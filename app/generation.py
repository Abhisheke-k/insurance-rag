"""Grounded answer generation with Claude.

The contract this module enforces, in order of importance:

1. **Nothing is answered from model knowledge.** The system prompt restricts
   Claude to the supplied passages and gives it an explicit escape hatch.
2. **Every answer is traceable.** Claude returns the passage numbers it used
   (structured output, so the shape is guaranteed); we resolve those numbers
   back to real chunk ids here and *discard anything that does not resolve*. A
   citation the model invented cannot survive this step.
3. **No citations means no answer.** If Claude claims it found something but
   cites nothing that resolves, the response is downgraded to the not-found
   message rather than shipped unsourced.

Retrieval quality is handled upstream; this module is deliberately strict and
boring.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Sequence

from app.config import Settings
from app.llm import LLMProvider, LLMUnavailableError, build_llm_provider
from app.models import Citation, GeneratedAnswer, RetrievedChunk

logger = logging.getLogger(__name__)

__all__ = [
    "NOT_FOUND_MESSAGE",
    "AnswerGenerator",
    "GenerationUnavailableError",
    "build_context_block",
    "not_found_answer",
]

NOT_FOUND_MESSAGE = "I couldn't find this in the documents."

_SNIPPET_CHARS = 320

SYSTEM_PROMPT = f"""\
You answer questions about insurance endorsement documents for an operations team.

You have access ONLY to the numbered passages supplied in the user message. They \
were retrieved from the customer's own documents.

Rules:
1. Answer only from the passages. Do not use general insurance knowledge, and do \
not infer, estimate, or fill in gaps.
2. If the passages do not contain the answer, set "found" to false and set \
"answer" to exactly: "{NOT_FOUND_MESSAGE}"
3. When you do answer, list in "citations" the numbers of every passage you relied \
on. Each factual statement in your answer must be supported by at least one cited \
passage.
4. Cite only passage numbers that actually appear in the context. Never invent one.
5. Reproduce defined terms, limits, dates, percentages and monetary amounts exactly \
as written.
6. Lead with the answer, then the conditions, exclusions or provisos that qualify \
it. No preamble, no restating the question.
7. A partial answer is fine when the passages establish part of it -- say plainly \
what they do and do not establish. Only use the not-found response when the \
passages are genuinely silent on the question.
8. If passages conflict (for example an endorsement amending a base clause), say so \
and cite both.
"""

#: Structured-output schema. Guarantees the response parses, so the citation
#: check below never has to cope with prose in place of JSON.
ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {
            "type": "boolean",
            "description": "True only when the passages answer the question.",
        },
        "answer": {
            "type": "string",
            "description": "The grounded answer, or the exact not-found sentence.",
        },
        "citations": {
            "type": "array",
            "description": "Passage numbers relied on, e.g. [1, 3].",
            "items": {"type": "integer"},
        },
    },
    "required": ["found", "answer", "citations"],
    "additionalProperties": False,
}


class GenerationUnavailableError(RuntimeError):
    """Raised when the configured LLM backend cannot be reached or is not configured."""


def build_context_block(retrieved: Sequence[RetrievedChunk]) -> str:
    """Render retrieved chunks as numbered, citation-labelled passages."""
    passages: list[str] = []
    for index, chunk in enumerate(retrieved, start=1):
        pages = (
            f"{chunk.page}" if chunk.page == chunk.page_end else f"{chunk.page}-{chunk.page_end}"
        )
        header = (
            f"[{index}] filename: {chunk.filename} | page: {pages} | "
            f"section: {chunk.section or '(unlabelled)'} | chunk_id: {chunk.chunk_id}"
        )
        passages.append(f"{header}\n{chunk.text}")
    return "\n\n".join(passages)


def _build_user_message(question: str, retrieved: Sequence[RetrievedChunk]) -> str:
    return (
        "<context>\n"
        f"{build_context_block(retrieved)}\n"
        "</context>\n\n"
        f"Question: {question.strip()}"
    )


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


def not_found_answer(*, chunks_used: int = 0, notes: Sequence[str] = ()) -> GeneratedAnswer:
    """The canonical 'not in the documents' response."""
    return GeneratedAnswer(
        answer=NOT_FOUND_MESSAGE,
        found=False,
        citations=[],
        chunks_used=chunks_used,
        notes=list(notes),
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort JSON recovery for the non-structured fallback path."""
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


class AnswerGenerator:
    """Wraps a single LLM call plus the citation-resolution guard rails.

    The call itself is delegated to an injected :class:`~app.llm.LLMProvider`
    (Anthropic, OpenAI, or the deterministic stub -- see :mod:`app.llm`); this
    class owns only the parts that do not vary by backend: prompt assembly and
    citation verification.
    """

    def __init__(self, settings: Settings, *, provider: LLMProvider | None = None) -> None:
        self._settings = settings
        self._provider = provider or build_llm_provider(settings)

    # ------------------------------------------------------------------ #
    @property
    def is_configured(self) -> bool:
        return self._provider.is_configured()

    def status(self) -> dict[str, Any]:
        provider_status = self._provider.status()
        return {
            "configured": provider_status["configured"],
            "model": provider_status["model"],
            "effort": self._settings.answer_effort,
            "error": provider_status["error"],
        }

    # ------------------------------------------------------------------ #
    def generate(self, question: str, retrieved: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        """Ask the model the question against ``retrieved``, then verify citations."""
        if not retrieved:
            return not_found_answer(notes=["no passages were retrieved"])

        if not self.is_configured:
            raise GenerationUnavailableError(
                self._provider.status()["error"] or "the LLM backend is not configured for this deployment"
            )

        try:
            response = self._provider.complete(
                system=SYSTEM_PROMPT,
                user_message=_build_user_message(question, retrieved),
                json_schema=ANSWER_SCHEMA,
                max_tokens=self._settings.answer_max_tokens,
                effort=self._settings.answer_effort,
            )
        except LLMUnavailableError as exc:
            raise GenerationUnavailableError(str(exc)) from exc

        if response.stop_reason == "refusal":
            logger.warning("the model declined the request (category=%s)", response.refusal_category)
            return GeneratedAnswer(
                answer=(
                    "The model declined to answer this request. Rephrase the question, "
                    "or review the retrieved passages directly."
                ),
                found=False,
                citations=[],
                chunks_used=len(retrieved),
                model=response.model,
                stop_reason="refusal",
                notes=[f"refusal category: {response.refusal_category}"] if response.refusal_category else [],
            )

        payload = _extract_json(response.text)
        notes: list[str] = []

        if payload is None:
            if response.stop_reason == "max_tokens":
                notes.append("response hit max_tokens before completing")
            logger.error("could not parse a JSON answer from the model response")
            return not_found_answer(chunks_used=len(retrieved), notes=notes + ["unparseable model response"])

        return self._verify(payload, retrieved, response, notes)

    # ------------------------------------------------------------------ #
    def _verify(
        self,
        payload: dict[str, Any],
        retrieved: Sequence[RetrievedChunk],
        response: Any,
        notes: list[str],
    ) -> GeneratedAnswer:
        """Resolve claimed passage numbers to real chunks; drop what does not resolve."""
        model = getattr(response, "model", None)
        stop_reason = getattr(response, "stop_reason", None)
        found = bool(payload.get("found", False))
        answer = str(payload.get("answer", "")).strip()

        if not found:
            return GeneratedAnswer(
                answer=NOT_FOUND_MESSAGE,
                found=False,
                citations=[],
                chunks_used=len(retrieved),
                model=model,
                stop_reason=stop_reason,
                notes=notes,
            )

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

        if not citations:
            # Traceability beats helpfulness: an uncited answer is not shippable.
            notes.append("answer had no resolvable citation and was withheld")
            logger.warning("withholding an uncited answer for traceability")
            return GeneratedAnswer(
                answer=NOT_FOUND_MESSAGE,
                found=False,
                citations=[],
                chunks_used=len(retrieved),
                model=model,
                stop_reason=stop_reason,
                notes=notes,
            )

        if not answer:
            answer = NOT_FOUND_MESSAGE
            return GeneratedAnswer(
                answer=answer,
                found=False,
                citations=[],
                chunks_used=len(retrieved),
                model=model,
                stop_reason=stop_reason,
                notes=notes + ["model returned an empty answer"],
            )

        return GeneratedAnswer(
            answer=answer,
            found=True,
            citations=citations,
            chunks_used=len(retrieved),
            model=model,
            stop_reason=stop_reason,
            notes=notes,
        )
