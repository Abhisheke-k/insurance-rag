"""The Week 7 claims agent: multi-step completion, safe stopping, tool dispatch.

``ingested_service`` (from conftest) exercises the agent against the real
deterministic StubProvider persona (Persona 3 in app/llm.py) -- the same
zero-key path the live app and the race script use. The edge cases that need
a specific, forced sequence of turns (never finalizing, finalizing too early)
use ``ScriptedStepProvider`` below, a queue-based fake in the same spirit as
conftest's ``StubGenerator``.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict
from typing import Any

import pytest

from app.agent import ClaimsAgent
from app.config import Settings
from app.llm import LLMProvider, LLMResponse
from app.rag import RagService


class ScriptedStepProvider(LLMProvider):
    """Returns a fixed queue of agent-turn JSON payloads, one per ``complete()`` call."""

    name = "fake:scripted-steps"

    def __init__(self, turns: Sequence[dict[str, Any]], *, repeat_last: bool = False) -> None:
        self._turns = list(turns)
        self._repeat_last = repeat_last
        self.calls = 0

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"provider": "fake", "model": self.name, "configured": True, "error": None}

    def complete(
        self,
        *,
        system: str,
        user_message: str,
        json_schema: dict[str, Any],
        max_tokens: int,
        effort: str = "medium",
    ) -> LLMResponse:
        if self.calls < len(self._turns):
            payload = self._turns[self.calls]
        elif self._repeat_last and self._turns:
            payload = self._turns[-1]
        else:  # pragma: no cover - tests size their scripts to avoid this
            payload = {"thought": "", "action": None}
        self.calls += 1
        return LLMResponse(text=json.dumps(payload), model=self.name, stop_reason="end")


def _turn(
    action: str | None,
    *,
    thought: str = "",
    search_query: str | None = None,
    draft_claim_number: str | None = None,
    draft_date_of_loss: str | None = None,
    draft_coverage_decision: str | None = None,
    draft_cited_exclusion_id: str | None = None,
    draft_excess_amount: float | None = None,
    draft_summary: str | None = None,
    draft_citations: list[int] | None = None,
) -> dict[str, Any]:
    return {
        "thought": thought,
        "action": action,
        "search_query": search_query,
        "draft_claim_number": draft_claim_number,
        "draft_date_of_loss": draft_date_of_loss,
        "draft_coverage_decision": draft_coverage_decision,
        "draft_cited_exclusion_id": draft_cited_exclusion_id,
        "draft_excess_amount": draft_excess_amount,
        "draft_summary": draft_summary,
        "draft_citations": draft_citations or [],
    }


FLOOD_NOTES = (
    "Claim CLM-2024-00123: insured reports flood damage on 12 March 2024 to the "
    "warehouse roof, within the annual aggregate flood sub-limit area. Claimed "
    "amount GBP 15,000."
)


# --------------------------------------------------------------------------- #
def test_agent_completes_a_genuinely_multistep_task(ingested_service: RagService):
    result = ingested_service.process_claim_with_agent(FLOOD_NOTES)

    assert result.stopped_reason == "finalized"
    assert len(result.steps) >= 3
    actions = [step.action for step in result.steps]
    assert "search_policy" in actions
    assert "check_assertions" in actions
    assert actions[-1] == "finalize"
    assert result.llm_calls == len(result.steps)
    assert result.summary.claim_number == "CLM-2024-00123"
    valid_decisions = {"covered", "denied", "partially_covered", "needs_review"}
    assert result.summary.coverage_decision in valid_decisions


def test_agent_stops_safely_when_it_never_finalizes(
    settings: Settings, ingested_service: RagService
):
    budget = settings.model_copy(update={"agent_max_steps": 3})
    turns = [_turn("search_policy", search_query="flood")]
    provider = ScriptedStepProvider(turns, repeat_last=True)
    agent = ClaimsAgent(budget, searcher=ingested_service, provider=provider)

    result = agent.process(FLOOD_NOTES)

    assert result.stopped_reason == "stopped: step budget (3) exhausted"
    assert len(result.steps) == 3
    assert provider.calls == 3
    assert result.summary.coverage_decision == "needs_review"


def test_agent_rejects_a_premature_finalize(settings: Settings, ingested_service: RagService):
    turns = [
        _turn("finalize", draft_coverage_decision="covered", draft_citations=[1]),
        _turn("search_policy", search_query="flood sub-limit"),
        _turn(
            "check_assertions",
            draft_claim_number="CLM-2024-00123",
            draft_date_of_loss="12 March 2024",
            draft_coverage_decision="covered",
            draft_citations=[1],
        ),
        _turn("finalize"),
    ]
    provider = ScriptedStepProvider(turns)
    agent = ClaimsAgent(settings, searcher=ingested_service, provider=provider)

    result = agent.process(FLOOD_NOTES)

    actions = [step.action for step in result.steps]
    assert actions == ["finalize", "search_policy", "check_assertions", "finalize"]
    assert "rejected" in result.steps[0].observation
    assert "search_policy" in result.steps[0].observation
    assert result.stopped_reason == "finalized"


def test_search_policy_tool_respects_top_k(settings: Settings, ingested_service: RagService):
    budget = settings.model_copy(update={"agent_max_steps": 1})
    provider = ScriptedStepProvider([_turn("search_policy", search_query="flood sub-limit")])
    agent = ClaimsAgent(budget, searcher=ingested_service, provider=provider)

    result = agent.process(FLOOD_NOTES, top_k=2)

    assert 0 < len(result.retrieved) <= 2


def test_check_assertions_surfaces_a_malformed_claim_number(
    settings: Settings, ingested_service: RagService
):
    budget = settings.model_copy(update={"agent_max_steps": 2})
    turns = [
        _turn("search_policy", search_query="flood"),
        _turn(
            "check_assertions",
            draft_claim_number="not-a-real-claim-number",
            draft_date_of_loss="12 March 2024",
            draft_coverage_decision="needs_review",
        ),
    ]
    provider = ScriptedStepProvider(turns)
    agent = ClaimsAgent(budget, searcher=ingested_service, provider=provider)

    result = agent.process(FLOOD_NOTES)

    check_step = result.steps[1]
    assert check_step.action == "check_assertions"
    assert "claim_number_format=FAIL" in check_step.observation


def test_every_step_is_visible_and_json_serializable(ingested_service: RagService):
    result = ingested_service.process_claim_with_agent(FLOOD_NOTES)

    assert result.steps
    for step in result.steps:
        assert step.action
        json.dumps(asdict(step))  # raises if any field isn't JSON-safe


def test_agent_rejects_empty_notes(ingested_service: RagService):
    with pytest.raises(ValueError):
        ingested_service.process_claim_with_agent("   ")
