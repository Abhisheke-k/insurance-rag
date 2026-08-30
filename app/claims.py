"""Claim summarisation: adjuster notes -> a structured, cited claim summary.

This is the artifact Weeks 5 and 6 are built around. It is a second
generation surface alongside `/ask` (`app/generation.py`), reusing the same
retrieval stack and the same `LLMProvider` abstraction (`app/llm.py`) but a
different prompt and a different output shape: instead of answering a
question, it reads free-text adjuster notes, retrieves the policy/endorsement
wording those notes describe, and produces a coverage decision an adjuster
can act on -- claim number, date of loss, covered/denied/needs-review, the
exclusion clause it turned on (if any), and a citation back to the passage
that decision came from.

The citation discipline is the same as `/ask`: a coverage decision with no
citation that survives verification is not shippable, so it is downgraded to
`needs_review` rather than shipped as a confident but ungrounded denial or
approval -- the exact mistake ("a summary that invents coverage") the Week 6
brief opens with.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import Settings
from app.generation import build_context_block
from app.llm import LLMProvider, LLMUnavailableError, build_llm_provider
from app.models import Citation, RetrievedChunk

logger = logging.getLogger(__name__)

__all__ = [
    "CLAIM_NUMBER_RE",
    "CLAIM_SUMMARY_SCHEMA",
    "ClaimSummary",
    "ClaimSummaryGenerationError",
    "ClaimSummaryGenerator",
    "build_claim_user_message",
]

#: The canonical claim-number shape this whole app assumes -- also the
#: deterministic assertion in Week 6 (see app/assertions.py).
CLAIM_NUMBER_RE = re.compile(r"CLM-\d{4}-\d{5}")

CoverageDecision = str  # "covered" | "denied" | "partially_covered" | "needs_review"

SYSTEM_PROMPT = """\
You write claim summaries for a claims operations team from adjuster notes.

You have access ONLY to the adjuster notes and the numbered policy/endorsement \
passages supplied in the user message.

Rules:
1. Extract the claim number and date of loss exactly as they appear in the notes. \
If either is missing or malformed, say so rather than inventing one.
2. Decide coverage ("covered", "denied", "partially_covered", or "needs_review") \
strictly from whether the retrieved passages describe an exclusion that matches the \
circumstances in the notes, or a peril/limit that covers them. If no retrieved \
passage clearly applies, use "needs_review" -- do not guess.
3. Every coverage decision must cite the passage number(s) it rests on in "citations". \
A decision with no citation is not usable -- if you cannot ground the decision in a \
passage, use "needs_review" and say why in "summary".
4. If you cite an exclusion, name its clause id in "cited_exclusion_id" (e.g. "4.2(d)" \
or "4.3"). Leave it null when the decision is not a denial.
5. Reproduce amounts, dates and clause numbers exactly as written in the source text.
6. "summary" is 1-3 sentences a claims handler can act on without re-reading the notes.
"""

CLAIM_SUMMARY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "claim_number": {"type": "string", "description": "As written in the notes, or 'UNKNOWN'."},
        "date_of_loss": {"type": "string", "description": "As written in the notes, or empty string."},
        "coverage_decision": {
            "type": "string",
            "enum": ["covered", "denied", "partially_covered", "needs_review"],
        },
        "cited_exclusion_id": {"type": ["string", "null"]},
        "excess_amount": {"type": ["number", "null"]},
        "summary": {"type": "string"},
        "citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "claim_number",
        "date_of_loss",
        "coverage_decision",
        "cited_exclusion_id",
        "excess_amount",
        "summary",
        "citations",
    ],
    "additionalProperties": False,
}


class ClaimSummaryGenerationError(RuntimeError):
    """Raised when the configured LLM backend cannot be reached or is not configured."""


@dataclass(frozen=True, slots=True)
class ClaimSummary:
    """A verified claim summary -- what survives citation resolution."""

    claim_number: str
    date_of_loss: str
    coverage_decision: CoverageDecision
    cited_exclusion_id: str | None
    excess_amount: float | None
    summary: str
    citations: list[Citation]
    chunks_used: int
    model: str | None = None
    notes: list[str] = field(default_factory=list)


def build_claim_user_message(adjuster_notes: str, retrieved: Sequence[RetrievedChunk]) -> str:
    return (
        "<adjuster_notes>\n"
        f"{adjuster_notes.strip()}\n"
        "</adjuster_notes>\n\n"
        "<policy_context>\n"
        f"{build_context_block(retrieved)}\n"
        "</policy_context>"
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    text = text.strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    start, end = candidate.find("{"), candidate.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(candidate[start : end + 1])
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


_SNIPPET_CHARS = 320


def _snippet(text: str) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    return compact if len(compact) <= _SNIPPET_CHARS else compact[: _SNIPPET_CHARS - 1].rstrip() + "…"


def _to_citation(chunk: RetrievedChunk) -> Citation:
    return Citation(
        filename=chunk.filename,
        page=chunk.page,
        section=chunk.section,
        chunk_id=chunk.chunk_id,
        score=round(chunk.score, 4),
        snippet=_snippet(chunk.text),
    )


class ClaimSummaryGenerator:
    """Wraps a single LLM call plus citation verification, mirroring AnswerGenerator."""

    def __init__(self, settings: Settings, *, provider: LLMProvider | None = None) -> None:
        self._settings = settings
        self._provider = provider or build_llm_provider(settings)

    @property
    def is_configured(self) -> bool:
        return self._provider.is_configured()

    def status(self) -> dict[str, Any]:
        provider_status = self._provider.status()
        return {
            "configured": provider_status["configured"],
            "model": provider_status["model"],
            "error": provider_status["error"],
        }

    def summarize(self, adjuster_notes: str, retrieved: Sequence[RetrievedChunk]) -> ClaimSummary:
        if not self.is_configured:
            raise ClaimSummaryGenerationError(
                self._provider.status()["error"] or "the LLM backend is not configured for this deployment"
            )

        try:
            response = self._provider.complete(
                system=SYSTEM_PROMPT,
                user_message=build_claim_user_message(adjuster_notes, retrieved),
                json_schema=CLAIM_SUMMARY_SCHEMA,
                max_tokens=self._settings.answer_max_tokens,
                effort=self._settings.answer_effort,
            )
        except LLMUnavailableError as exc:
            raise ClaimSummaryGenerationError(str(exc)) from exc

        notes: list[str] = []
        if response.stop_reason == "refusal":
            return ClaimSummary(
                claim_number="UNKNOWN",
                date_of_loss="",
                coverage_decision="needs_review",
                cited_exclusion_id=None,
                excess_amount=None,
                summary="The model declined to summarise this claim.",
                citations=[],
                chunks_used=len(retrieved),
                model=response.model,
                notes=[f"refusal category: {response.refusal_category}"] if response.refusal_category else [],
            )

        payload = _extract_json(response.text)
        if payload is None:
            if response.stop_reason == "max_tokens":
                notes.append("response hit max_tokens before completing")
            return ClaimSummary(
                claim_number="UNKNOWN",
                date_of_loss="",
                coverage_decision="needs_review",
                cited_exclusion_id=None,
                excess_amount=None,
                summary="Unable to parse a claim summary from the model response.",
                citations=[],
                chunks_used=len(retrieved),
                model=response.model,
                notes=notes + ["unparseable model response"],
            )

        return self._verify(payload, retrieved, response.model, notes)

    def _verify(
        self,
        payload: dict[str, Any],
        retrieved: Sequence[RetrievedChunk],
        model: str | None,
        notes: list[str],
    ) -> ClaimSummary:
        citations: list[Citation] = []
        seen: set[str] = set()
        for raw in payload.get("citations") or []:
            try:
                index = int(raw)
            except (TypeError, ValueError):
                notes.append(f"discarded non-numeric citation {raw!r}")
                continue
            if not 1 <= index <= len(retrieved):
                notes.append(f"discarded out-of-range citation [{index}]")
                continue
            chunk = retrieved[index - 1]
            if chunk.chunk_id in seen:
                continue
            seen.add(chunk.chunk_id)
            citations.append(_to_citation(chunk))

        coverage_decision = str(payload.get("coverage_decision", "needs_review"))
        cited_exclusion_id = payload.get("cited_exclusion_id")
        excess_amount = payload.get("excess_amount")

        if not citations and coverage_decision != "needs_review":
            # Same principle as /ask: a coverage decision that cites nothing
            # resolvable is not shippable, however plausible it reads.
            notes.append(f"decision {coverage_decision!r} had no resolvable citation and was downgraded")
            logger.warning("downgrading an uncited coverage decision to needs_review")
            coverage_decision = "needs_review"
            cited_exclusion_id = None

        return ClaimSummary(
            claim_number=str(payload.get("claim_number", "UNKNOWN")).strip() or "UNKNOWN",
            date_of_loss=str(payload.get("date_of_loss", "")).strip(),
            coverage_decision=coverage_decision,
            cited_exclusion_id=str(cited_exclusion_id) if cited_exclusion_id else None,
            excess_amount=float(excess_amount) if isinstance(excess_amount, (int, float)) else None,
            summary=str(payload.get("summary", "")).strip(),
            citations=citations,
            chunks_used=len(retrieved),
            model=model,
            notes=notes,
        )
