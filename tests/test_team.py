"""Week 10: the metering layer and the manager + two specialists team.

The team is the thing being *measured*, so these tests pin its structure --
who sees which tools, what a hand-off carries, whether the specialists really
run concurrently -- rather than its answers. Whether it should replace the
single agent is decided by ``scripts/w10_team_vs_single.py``, not here.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Sequence
from typing import Any

import pytest

from app.config import Settings
from app.llm import LLMProvider, LLMResponse, LLMUsage, StubProvider
from app.mcp_agent import McpClaimsAgent
from app.mcp_client import McpToolClient
from app.mcp_server import ALL_TOOLS, build_server_for_service
from app.metering import (
    PRICES_USD_PER_MTOK,
    CallRecord,
    Meter,
    MeteredProvider,
    Totals,
    UnknownModelPrice,
    cost_usd,
    summarise,
)
from app.rag import RagService
from app.team import MANAGER_SCHEMA, ClaimsTeam

FLOOD_NOTES = (
    "Claim CLM-2024-00123: insured reports flood damage on 12 March 2024 to the "
    "warehouse roof, within the annual aggregate flood sub-limit area. Claimed amount GBP 15,000."
)


@pytest.fixture
def client(ingested_service: RagService):
    tool_client = McpToolClient(build_server_for_service(ingested_service, enabled=ALL_TOOLS)).connect()
    yield tool_client
    tool_client.close()


# --------------------------------------------------------------------------- #
# Metering
# --------------------------------------------------------------------------- #
def test_stub_reports_estimated_usage_from_the_real_prompt():
    response = StubProvider().complete(
        system="ROLE: x", user_message="<adjuster_notes>\nhello\n</adjuster_notes>\n\n<scratchpad>\n(none)\n</scratchpad>",
        json_schema={"properties": {"thought": {}, "action": {}}}, max_tokens=1000,
    )
    assert response.usage is not None and response.usage.estimated
    assert response.usage.input_tokens > 0 and response.usage.output_tokens > 0


def test_metered_provider_records_role_tokens_and_time_without_changing_the_response():
    class Fixed(LLMProvider):
        name = "fake"

        def is_configured(self) -> bool:
            return True

        def status(self) -> dict[str, Any]:
            return {"provider": "fake", "model": "m", "configured": True, "error": None}

        def complete(self, *, system, user_message, json_schema, max_tokens, effort="medium") -> LLMResponse:
            return LLMResponse(text="{}", model="claude-opus-5-5", stop_reason="end", usage=LLMUsage(100, 20))

    meter = Meter()
    base = MeteredProvider(Fixed(), meter, role="manager")
    response = base.complete(system="s", user_message="u", json_schema={}, max_tokens=10)
    base.with_role("analyst").complete(system="s", user_message="u", json_schema={}, max_tokens=10)

    assert response.text == "{}"
    records = meter.since(0)
    assert [r.role for r in records] == ["manager", "analyst"]
    assert (records[0].input_tokens, records[0].output_tokens, records[0].estimated) == (100, 20, False)
    assert meter.by_role(records)["analyst"].calls == 1


def test_a_response_without_usage_is_estimated_and_flagged():
    class NoUsage(StubProvider):
        def complete(self, **kwargs: Any) -> LLMResponse:
            real = super().complete(**kwargs)
            return LLMResponse(text=real.text, model=real.model, stop_reason="end", usage=None)

    meter = Meter()
    MeteredProvider(NoUsage(), meter).complete(
        system="ROLE: x", user_message="<adjuster_notes>\nhi\n</adjuster_notes>",
        json_schema={"properties": {"thought": {}, "action": {}}}, max_tokens=10,
    )
    (record,) = meter.since(0)
    assert record.estimated and record.input_tokens > 0


def test_meter_marks_isolate_one_claims_worth_of_calls():
    meter = Meter()
    rec = CallRecord("agent", "m", 10, 5, 0, 0, 0.1, False)
    meter.add(rec)
    mark = meter.mark()
    meter.add(rec)
    meter.add(rec)
    assert summarise(meter.since(mark)).calls == 2 and summarise(meter.since(0)).calls == 3


def test_cost_is_tokens_times_list_price():
    totals = Totals(calls=1, input_tokens=2_000_000, output_tokens=500_000)
    price_in, price_out = PRICES_USD_PER_MTOK["claude-opus-5-5"]
    assert cost_usd(totals, "claude-opus-5-5") == pytest.approx(2 * price_in + 0.5 * price_out)
    assert cost_usd(totals, "claude-opus-5-5") == pytest.approx(2 * 4.00 + 0.5 * 20.00)
    with pytest.raises(UnknownModelPrice):
        cost_usd(totals, "claude-imaginary-9")


def test_meter_is_safe_under_concurrent_writers():
    meter = Meter()

    def work() -> None:
        for _ in range(200):
            meter.add(CallRecord("r", "m", 1, 1, 0, 0, 0.0, False))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert summarise(meter.since(0)).calls == 1600


# --------------------------------------------------------------------------- #
# The team's structure
# --------------------------------------------------------------------------- #
def test_team_finalizes_with_the_expected_division_of_labour(client: McpToolClient, settings: Settings):
    result = ClaimsTeam(settings, tools=client).process(FLOOD_NOTES)

    assert result.stopped_reason == "finalized"
    assert result.calls_by_role == {"manager": 2, "analyst": 2, "clerk": 2}
    assert result.llm_calls == 6 and result.handoffs == 2
    assert result.critical_path_calls == 4  # manager, then the slower specialist (parallel), then manager
    assert result.summary.claim_number == "CLM-2024-00123"
    assert result.summary.citations  # the analyst's passage numbers resolve against the analyst's evidence


def test_team_and_single_agent_agree_on_these_claims(client: McpToolClient, settings: Settings):
    """Quality parity here is expected -- both bottom out in the same simulated reading comprehension."""
    single = McpClaimsAgent(settings, tools=client)
    team = ClaimsTeam(settings, tools=client)
    one, many = single.process(FLOOD_NOTES), team.process(FLOOD_NOTES)

    assert (one.summary.coverage_decision, one.summary.claim_number, one.summary.date_of_loss) == (
        many.summary.coverage_decision, many.summary.claim_number, many.summary.date_of_loss,
    )
    assert [c.chunk_id for c in one.summary.citations] == [c.chunk_id for c in many.summary.citations]


class CapturingProvider(LLMProvider):
    """Delegates to the stub but remembers who was sent what."""

    name = "fake:capturing"

    def __init__(self) -> None:
        self._stub = StubProvider()
        self.seen: list[tuple[str, str, str]] = []  # (role marker, system, user)

    def is_configured(self) -> bool:
        return True

    def status(self) -> dict[str, Any]:
        return {"provider": "fake", "model": self.name, "configured": True, "error": None}

    def complete(self, *, system, user_message, json_schema, max_tokens, effort="medium") -> LLMResponse:
        role = system.splitlines()[0]
        self.seen.append((role, system, user_message))
        return self._stub.complete(system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens)


def test_each_specialist_is_offered_only_the_tools_its_job_needs(client: McpToolClient, settings: Settings):
    provider = CapturingProvider()
    ClaimsTeam(settings, tools=client, provider=provider).process(FLOOD_NOTES)

    def tools_in(system: str) -> set[str]:
        block = system.split("<tools>\n")[1].split("\n</tools>")[0]
        return {tool["name"] for tool in json.loads(block)}

    analyst = next(system for role, system, _ in provider.seen if role == "ROLE: coverage_analyst")
    clerk = next(system for role, system, _ in provider.seen if role == "ROLE: claims_clerk")
    assert tools_in(analyst) == {"search_policy", "lookup_exclusion_code"}
    assert tools_in(clerk) == {"check_assertions"}
    assert not any("<tools>" in system for role, system, _ in provider.seen if role == "ROLE: manager")


def test_every_handoff_resends_the_notes(client: McpToolClient, settings: Settings):
    provider = CapturingProvider()
    ClaimsTeam(settings, tools=client, provider=provider).process(FLOOD_NOTES)

    by_role: dict[str, int] = {}
    for role, _system, user in provider.seen:
        assert FLOOD_NOTES in user  # no agent works from a summary of the notes
        by_role[role] = by_role.get(role, 0) + 1
    assert by_role == {"ROLE: manager": 2, "ROLE: coverage_analyst": 2, "ROLE: claims_clerk": 2}


def test_full_handoff_forwards_the_specialists_evidence_and_lean_does_not(client: McpToolClient, settings: Settings):
    def final_manager_prompt(handoff: str) -> str:
        provider = CapturingProvider()
        ClaimsTeam(settings, tools=client, provider=provider, handoff=handoff).process(FLOOD_NOTES)
        return [user for role, _s, user in provider.seen if role == "ROLE: manager"][-1]

    lean, full = final_manager_prompt("lean"), final_manager_prompt("full")

    assert "transcript" not in lean and "transcript" in full
    assert len(full) > len(lean) * 1.5
    assert "annual aggregate" in full.lower() or "sub-limit" in full.lower()  # a retrieved passage, re-sent
    with pytest.raises(ValueError):
        ClaimsTeam(settings, tools=client, handoff="everything")


def _rendezvous_provider(barrier: threading.Barrier) -> CapturingProvider:
    class Rendezvous(CapturingProvider):
        def complete(self, *, system, user_message, json_schema, max_tokens, effort="medium") -> LLMResponse:
            first_specialist_turn = "ROLE: manager" not in system and "action=" not in user_message
            if first_specialist_turn:
                barrier.wait()
            return super().complete(system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens)

    return Rendezvous()


def test_specialists_run_concurrently_when_parallel(client: McpToolClient, settings: Settings):
    """Both specialists must be inside ``complete()`` at the same moment, or the barrier breaks."""
    provider = _rendezvous_provider(threading.Barrier(2, timeout=10))
    result = ClaimsTeam(settings, tools=client, provider=provider, parallel=True).process(FLOOD_NOTES)
    assert result.stopped_reason == "finalized"


def test_specialists_run_one_at_a_time_when_not_parallel(client: McpToolClient, settings: Settings):
    provider = _rendezvous_provider(threading.Barrier(2, timeout=1.0))  # one at a time can never meet here
    with pytest.raises(threading.BrokenBarrierError):
        ClaimsTeam(settings, tools=client, provider=provider, parallel=False).process(FLOOD_NOTES)


def test_sequential_mode_lengthens_the_critical_path(client: McpToolClient, settings: Settings):
    parallel = ClaimsTeam(settings, tools=client, parallel=True).process(FLOOD_NOTES)
    sequential = ClaimsTeam(settings, tools=client, parallel=False).process(FLOOD_NOTES)
    assert parallel.llm_calls == sequential.llm_calls == 6
    assert parallel.critical_path_calls == 4 and sequential.critical_path_calls == 6


class ScriptedManager(CapturingProvider):
    """Stub specialists, but a manager that follows a script."""

    def __init__(self, turns: Sequence[dict[str, Any]]) -> None:
        super().__init__()
        self._turns = list(turns)
        self.manager_calls = 0

    def complete(self, *, system, user_message, json_schema, max_tokens, effort="medium") -> LLMResponse:
        if json_schema is MANAGER_SCHEMA:
            payload = self._turns[min(self.manager_calls, len(self._turns) - 1)]
            self.manager_calls += 1
            return LLMResponse(text=json.dumps(payload), model=self.name, stop_reason="end")
        return super().complete(system=system, user_message=user_message, json_schema=json_schema, max_tokens=max_tokens)


def _manager_turn(action: str, delegations: list[dict[str, str]] | None = None, **draft: Any) -> dict[str, Any]:
    return {
        "thought": "", "action": action, "delegations": delegations or [],
        "draft_claim_number": draft.get("claim_number"), "draft_date_of_loss": draft.get("date_of_loss"),
        "draft_coverage_decision": draft.get("decision"), "draft_cited_exclusion_id": None,
        "draft_excess_amount": None, "draft_summary": draft.get("summary"), "draft_citations": draft.get("citations", []),
    }


def test_manager_cannot_finalize_before_both_specialists_have_reported(client: McpToolClient, settings: Settings):
    turns = [
        _manager_turn("finalize", decision="covered"),                                      # nothing delegated yet
        _manager_turn("delegate", [{"specialist": "analyst", "task": "decide coverage"}]),   # only one of the two
        _manager_turn("finalize", decision="covered"),                                      # clerk still missing
        _manager_turn("delegate", [{"specialist": "clerk", "task": "validate"}]),
    ]
    result = ClaimsTeam(settings, tools=client, provider=ScriptedManager(turns)).process(FLOOD_NOTES)

    rejections = [s.observation for s in result.steps if s.action == "finalize"]
    assert rejections == [
        "rejected: no report yet from analyst, clerk; delegate first",
        "rejected: no report yet from clerk; delegate first",
    ]
    # the manager spent its budget being refused, so the safe outcome is needs_review, not a guess
    assert result.stopped_reason == "stopped: step budget (4) exhausted"
    assert result.summary.coverage_decision == "needs_review"


def test_manager_can_delegate_in_two_rounds_and_then_finalize(client: McpToolClient, settings: Settings):
    turns = [
        _manager_turn("delegate", [{"specialist": "analyst", "task": "decide coverage"}]),
        _manager_turn("delegate", [{"specialist": "clerk", "task": "validate"}]),
        _manager_turn("finalize", claim_number="CLM-2024-00123", date_of_loss="12 March 2024", decision="covered", citations=[1]),
    ]
    result = ClaimsTeam(settings, tools=client, provider=ScriptedManager(turns)).process(FLOOD_NOTES)

    assert result.stopped_reason == "finalized" and result.handoffs == 2
    assert result.critical_path_calls == 3 + 2 + 2  # three manager turns, plus each specialist's two calls, run one after the other
    assert result.summary.claim_number == "CLM-2024-00123"


def test_a_manager_that_never_finalizes_ends_in_needs_review(client: McpToolClient, settings: Settings):
    team = ClaimsTeam(settings, tools=client, provider=ScriptedManager([_manager_turn("delegate", [])]))
    result = team.process(FLOOD_NOTES)

    assert result.stopped_reason == "stopped: step budget (4) exhausted"
    assert result.summary.coverage_decision == "needs_review"


def test_team_rejects_empty_notes_and_survives_unknown_specialists(client: McpToolClient, settings: Settings):
    with pytest.raises(ValueError):
        ClaimsTeam(settings, tools=client).process("   ")

    turns = [_manager_turn("delegate", [{"specialist": "intern", "task": "do stuff"}])]
    result = ClaimsTeam(settings, tools=client, provider=ScriptedManager(turns)).process(FLOOD_NOTES)
    assert result.steps[0].observation.startswith("rejected: no valid delegation")
