"""The claim-summary LLM judge: a single binary criterion, loaded from a prompt file.

Deliberately thin. All the judgment logic lives in the prompt text
(``coursework/w6/judge_v1.txt`` / ``judge_v2.txt``), not in this module, so
that "iterate the judge" means literally editing that file and re-running --
the same object Week 6 diffs and commits.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Sequence

from app.config import Settings
from app.generation import build_context_block
from app.llm import LLMProvider, LLMUnavailableError, build_llm_provider
from app.models import RetrievedChunk

__all__ = ["JUDGE_SCHEMA", "JudgeUnavailableError", "JudgeVerdict", "ClaimSummaryJudge", "build_judge_user_message"]

JUDGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pass_criterion": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["pass_criterion", "rationale"],
    "additionalProperties": False,
}


class JudgeUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    pass_criterion: bool
    rationale: str
    model: str | None = None


def build_judge_user_message(
    adjuster_notes: str, claim_summary: dict[str, Any], retrieved: Sequence[RetrievedChunk]
) -> str:
    return (
        "<adjuster_notes>\n"
        f"{adjuster_notes.strip()}\n"
        "</adjuster_notes>\n\n"
        "<claim_summary>\n"
        f"{json.dumps(claim_summary)}\n"
        "</claim_summary>\n\n"
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


class ClaimSummaryJudge:
    def __init__(self, settings: Settings, system_prompt: str, *, provider: LLMProvider | None = None) -> None:
        self._settings = settings
        self._system_prompt = system_prompt
        self._provider = provider or build_llm_provider(settings)

    def judge(
        self, adjuster_notes: str, claim_summary: dict[str, Any], retrieved: Sequence[RetrievedChunk]
    ) -> JudgeVerdict:
        try:
            response = self._provider.complete(
                system=self._system_prompt,
                user_message=build_judge_user_message(adjuster_notes, claim_summary, retrieved),
                json_schema=JUDGE_SCHEMA,
                max_tokens=self._settings.answer_max_tokens,
                effort=self._settings.answer_effort,
            )
        except LLMUnavailableError as exc:
            raise JudgeUnavailableError(str(exc)) from exc

        payload = _extract_json(response.text) or {}
        return JudgeVerdict(
            pass_criterion=bool(payload.get("pass_criterion", False)),
            rationale=str(payload.get("rationale", "")),
            model=response.model,
        )
