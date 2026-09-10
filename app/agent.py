"""The Week 7 claims agent: a hand-built think/act/observe loop, two tools.

Unlike ``app.claims.ClaimSummaryGenerator`` (one retrieve, one LLM call), this
agent decides its own next step each turn -- search the policy corpus again,
run the deterministic assertion checks, or finalize -- and reacts to what that
step returns. Every call site still goes through the same
``LLMProvider.complete(system, user_message, json_schema, ...)`` interface
(``app/llm.py``) that the rest of this app already uses: one JSON-schema turn
per loop iteration, not native SDK tool-use. That keeps the loop identical
across Anthropic/OpenAI/stub and testable with zero API keys.

Stopping safely is not optional here the way it is for a single call: the loop
enforces ``search_policy`` and ``check_assertions`` each at least once before a
``finalize`` is accepted (rejecting an early attempt rather than trusting the
prompt), and is bounded by ``AGENT_MAX_STEPS`` and ``AGENT_TIMEOUT_SECONDS`` --
hitting either ends the loop with a ``needs_review`` result and a
``stopped_reason``, never an exception or an infinite loop.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.assertions import run_assertions
from app.claims import ClaimSummary, resolve_citations
from app.config import Settings
from app.llm import LLMProvider, LLMUnavailableError, build_llm_provider
from app.models import RetrievedChunk

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_STEP_SCHEMA",
    "AgentClaimResult",
    "AgentStep",
    "ClaimsAgent",
    "ClaimsAgentError",
    "PolicySearcher",
]

SYSTEM_PROMPT = """\
You are a claims-processing agent working from adjuster notes for an insurance \
claims desk. You have two tools:

- search_policy: search the policy/endorsement corpus for a phrase or topic. \
Returns numbered passages you can cite. Search again with a more specific query \
if the first results look off-topic or don't settle the question.
- check_assertions: check your current draft claim summary against four \
deterministic rules (claim number format, date of loss parses, excess is \
numeric, a denial cites an exclusion id). Run it before finalizing, and again \
after you fix anything it flagged.

Reply with exactly one JSON object each turn: a one-sentence thought, the \
action you are taking, and your current best draft of the claim summary fields \
(leave a field null/empty if you have nothing new to say about it this turn).

Rules:
1. You must call search_policy at least once, and check_assertions at least \
once, before you may finalize. An early finalize attempt is rejected.
2. Ground every coverage decision in a passage number from a search_policy \
result and list it in draft_citations. A decision with no resolvable citation \
is downgraded to needs_review -- do not guess.
3. If check_assertions reports a failure you can fix from the notes or a \
passage, fix it and check again. If you cannot fix it (for example the notes \
never state a well-formed claim number), say so in draft_summary and finalize \
with needs_review rather than inventing a value.
4. Reproduce amounts, dates and clause numbers exactly as written in the source.
"""

AGENT_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "thought": {"type": "string"},
        "action": {"type": "string", "enum": ["search_policy", "check_assertions", "finalize"]},
        "search_query": {"type": ["string", "null"], "description": "Required for search_policy."},
        "draft_claim_number": {"type": ["string", "null"]},
        "draft_date_of_loss": {"type": ["string", "null"]},
        "draft_coverage_decision": {"type": ["string", "null"]},
        "draft_cited_exclusion_id": {"type": ["string", "null"]},
        "draft_excess_amount": {"type": ["number", "null"]},
        "draft_summary": {"type": ["string", "null"]},
        "draft_citations": {"type": "array", "items": {"type": "integer"}},
    },
    "required": [
        "thought",
        "action",
        "search_query",
        "draft_claim_number",
        "draft_date_of_loss",
        "draft_coverage_decision",
        "draft_cited_exclusion_id",
        "draft_excess_amount",
        "draft_summary",
        "draft_citations",
    ],
    "additionalProperties": False,
}


class PolicySearcher(Protocol):
    """The one capability the agent needs from ``RagService`` -- kept narrow so
    this module never imports ``app.rag`` (which itself composes this module)."""

    def retrieve(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]: ...


class ClaimsAgentError(RuntimeError):
    """Raised when the configured LLM backend cannot be reached or is not configured."""


@dataclass(frozen=True, slots=True)
class AgentStep:
    """One visible turn of the loop."""

    step_number: int
    thought: str
    action: str
    action_input: dict[str, Any]
    observation: str
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class AgentClaimResult:
    """What the loop produced, plus the full trace of how it got there."""

    summary: ClaimSummary
    steps: list[AgentStep]
    stopped_reason: str
    llm_calls: int
    elapsed_seconds: float
    retrieved: list[RetrievedChunk] = field(default_factory=list)


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


def _blank_draft() -> dict[str, Any]:
    return {
        "claim_number": "UNKNOWN",
        "date_of_loss": "",
        "coverage_decision": "needs_review",
        "cited_exclusion_id": None,
        "excess_amount": None,
        "summary": "",
        "citations": [],
    }


def _merge_draft(draft: dict[str, Any], turn: dict[str, Any]) -> None:
    """Fold this turn's non-empty draft_* fields into the running draft."""
    if turn.get("draft_claim_number"):
        draft["claim_number"] = str(turn["draft_claim_number"]).strip()
    if turn.get("draft_date_of_loss"):
        draft["date_of_loss"] = str(turn["draft_date_of_loss"]).strip()
    if turn.get("draft_coverage_decision"):
        draft["coverage_decision"] = str(turn["draft_coverage_decision"])
    if turn.get("draft_cited_exclusion_id"):
        draft["cited_exclusion_id"] = str(turn["draft_cited_exclusion_id"])
    if turn.get("draft_excess_amount") is not None:
        draft["excess_amount"] = turn["draft_excess_amount"]
    if turn.get("draft_summary"):
        draft["summary"] = str(turn["draft_summary"]).strip()
    if turn.get("draft_citations"):
        draft["citations"] = turn["draft_citations"]


def _describe_hits(assigned: Sequence[tuple[int, RetrievedChunk]]) -> str:
    if not assigned:
        return "no passages retrieved"
    lines: list[str] = []
    for index, chunk in assigned:
        same_page = chunk.page == chunk.page_end
        pages = f"{chunk.page}" if same_page else f"{chunk.page}-{chunk.page_end}"
        section = chunk.section or "(unlabelled)"
        header = f"[{index}] filename: {chunk.filename} | page: {pages} | section: {section}"
        lines.append(f"{header}\n{chunk.text}")
    return "\n\n".join(lines)


def _finalize_summary(
    draft: dict[str, Any], numbered: Sequence[RetrievedChunk], model: str | None, notes: list[str]
) -> ClaimSummary:
    citations = resolve_citations(draft.get("citations"), numbered, notes)
    coverage_decision = str(draft.get("coverage_decision") or "needs_review")
    cited_exclusion_id = draft.get("cited_exclusion_id")
    excess_amount = draft.get("excess_amount")

    if not citations and coverage_decision != "needs_review":
        # Same principle as ClaimSummaryGenerator._verify: a decision that cites
        # nothing resolvable is not shippable, however plausible it reads.
        notes.append(f"decision {coverage_decision!r} had no resolvable citation, downgraded")
        coverage_decision = "needs_review"
        cited_exclusion_id = None

    return ClaimSummary(
        claim_number=str(draft.get("claim_number") or "UNKNOWN").strip() or "UNKNOWN",
        date_of_loss=str(draft.get("date_of_loss") or "").strip(),
        coverage_decision=coverage_decision,
        cited_exclusion_id=str(cited_exclusion_id) if cited_exclusion_id else None,
        excess_amount=float(excess_amount) if isinstance(excess_amount, (int, float)) else None,
        summary=str(draft.get("summary") or "").strip(),
        citations=citations,
        chunks_used=len(numbered),
        model=model,
        notes=notes,
    )


def _build_turn_message(adjuster_notes: str, steps: Sequence[AgentStep]) -> str:
    if steps:
        scratchpad = "\n".join(
            f'<step number="{s.step_number}" action="{s.action}">\n'
            f"thought: {s.thought}\n"
            f"input: {s.action_input}\n"
            f"<observation>\n{s.observation}\n</observation>\n"
            "</step>"
            for s in steps
        )
    else:
        scratchpad = "(no steps taken yet -- your first action should be search_policy)"
    return (
        "<adjuster_notes>\n"
        f"{adjuster_notes}\n"
        "</adjuster_notes>\n\n"
        "<scratchpad>\n"
        f"{scratchpad}\n"
        "</scratchpad>"
    )


@dataclass
class _LoopState:
    """Mutable state threaded through one :meth:`ClaimsAgent.process` call."""

    start: float
    steps: list[AgentStep] = field(default_factory=list)
    numbered: list[RetrievedChunk] = field(default_factory=list)
    draft: dict[str, Any] = field(default_factory=_blank_draft)
    did_search: bool = False
    did_check: bool = False
    last_model: str | None = None
    _index_by_chunk_id: dict[str, int] = field(default_factory=dict)

    def assign_numbers(self, hits: Sequence[RetrievedChunk]) -> list[tuple[int, RetrievedChunk]]:
        """Give each hit a citation number stable across the whole episode --
        the same chunk keeps its number whether this is the first search or a
        later, more targeted one."""
        assigned: list[tuple[int, RetrievedChunk]] = []
        for chunk in hits:
            index = self._index_by_chunk_id.get(chunk.chunk_id)
            if index is None:
                self.numbered.append(chunk)
                index = len(self.numbered)
                self._index_by_chunk_id[chunk.chunk_id] = index
            assigned.append((index, chunk))
        return assigned


@dataclass(frozen=True, slots=True)
class _TurnContext:
    """One parsed LLM turn, before it is dispatched to an action handler."""

    step_number: int
    thought: str
    payload: dict[str, Any]
    started: float

    def as_step(self, action: str, action_input: dict[str, Any], observation: str) -> AgentStep:
        elapsed = time.monotonic() - self.started
        return AgentStep(self.step_number, self.thought, action, action_input, observation, elapsed)


class ClaimsAgent:
    """Hand-built ReAct loop: think, act (search or check), observe, repeat."""

    def __init__(
        self, settings: Settings, *, searcher: PolicySearcher, provider: LLMProvider | None = None
    ) -> None:
        self._settings = settings
        self._searcher = searcher
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

    def _next_turn(
        self, adjuster_notes: str, steps: Sequence[AgentStep]
    ) -> tuple[dict[str, Any], str | None]:
        try:
            response = self._provider.complete(
                system=SYSTEM_PROMPT,
                user_message=_build_turn_message(adjuster_notes, steps),
                json_schema=AGENT_STEP_SCHEMA,
                max_tokens=self._settings.answer_max_tokens,
                effort=self._settings.answer_effort,
            )
        except LLMUnavailableError as exc:
            raise ClaimsAgentError(str(exc)) from exc

        if response.stop_reason == "refusal":
            return {"thought": "the model declined this turn", "action": None}, response.model
        payload = _extract_json(response.text)
        return (payload or {"thought": "", "action": None}), response.model

    def process(self, adjuster_notes: str, top_k: int | None = None) -> AgentClaimResult:
        adjuster_notes = adjuster_notes.strip()
        if not adjuster_notes:
            raise ValueError("adjuster notes must not be empty")
        if not self.is_configured:
            raise ClaimsAgentError(
                self._provider.status()["error"]
                or "the LLM backend is not configured for this deployment"
            )

        state = _LoopState(start=time.monotonic())

        for step_number in range(1, self._settings.agent_max_steps + 1):
            if time.monotonic() - state.start >= self._settings.agent_timeout_seconds:
                timeout = self._settings.agent_timeout_seconds
                return self._stopped(state, reason=f"stopped: timeout after {timeout:.0f}s")

            payload, state.last_model = self._next_turn(adjuster_notes, state.steps)
            turn = _TurnContext(
                step_number=step_number,
                thought=str(payload.get("thought", "")).strip(),
                payload=payload,
                started=time.monotonic(),
            )
            action = payload.get("action")

            if action == "finalize" and not (state.did_search and state.did_check):
                self._reject_early_finalize(state, turn)
                continue
            if action == "search_policy":
                self._do_search(state, turn, adjuster_notes, top_k)
                continue
            if action == "check_assertions":
                self._do_check(state, turn)
                continue
            if action == "finalize":
                return self._do_finalize(state, turn)

            self._record_unrecognized(state, turn, action)

        budget = self._settings.agent_max_steps
        return self._stopped(state, reason=f"stopped: step budget ({budget}) exhausted")

    def _reject_early_finalize(self, state: _LoopState, turn: _TurnContext) -> None:
        missing = "search_policy" if not state.did_search else "check_assertions"
        observation = f"rejected: you must call {missing} before finalizing"
        state.steps.append(turn.as_step("finalize", turn.payload, observation))

    def _do_search(
        self, state: _LoopState, turn: _TurnContext, adjuster_notes: str, top_k: int | None
    ) -> None:
        state.did_search = True
        query = str(turn.payload.get("search_query") or "").strip() or adjuster_notes
        hits = self._searcher.retrieve(query, top_k or self._settings.top_k)
        assigned = state.assign_numbers(hits)
        _merge_draft(state.draft, turn.payload)
        action_input = {"search_query": query}
        state.steps.append(turn.as_step("search_policy", action_input, _describe_hits(assigned)))

    def _do_check(self, state: _LoopState, turn: _TurnContext) -> None:
        state.did_check = True
        _merge_draft(state.draft, turn.payload)
        results = run_assertions(state.draft)
        observation = "; ".join(
            f"{r.name}={'ok' if r.passed else 'FAIL'} ({r.detail})" for r in results
        )
        action_input = dict(state.draft)
        state.steps.append(turn.as_step("check_assertions", action_input, observation))

    def _do_finalize(self, state: _LoopState, turn: _TurnContext) -> AgentClaimResult:
        _merge_draft(state.draft, turn.payload)
        notes: list[str] = []
        summary = _finalize_summary(state.draft, state.numbered, state.last_model, notes)
        state.steps.append(turn.as_step("finalize", dict(state.draft), "finalized"))
        return AgentClaimResult(
            summary=summary,
            steps=state.steps,
            stopped_reason="finalized",
            llm_calls=len(state.steps),
            elapsed_seconds=time.monotonic() - state.start,
            retrieved=state.numbered,
        )

    def _record_unrecognized(self, state: _LoopState, turn: _TurnContext, action: Any) -> None:
        observation = (
            f"unrecognized action {action!r}; choose search_policy, check_assertions, or finalize"
        )
        state.steps.append(turn.as_step(str(action), turn.payload, observation))

    def _stopped(self, state: _LoopState, *, reason: str) -> AgentClaimResult:
        notes = [reason]
        draft = dict(state.draft, coverage_decision="needs_review")
        summary = _finalize_summary(draft, state.numbered, state.last_model, notes)
        return AgentClaimResult(
            summary=summary,
            steps=state.steps,
            stopped_reason=reason,
            llm_calls=len(state.steps),
            elapsed_seconds=time.monotonic() - state.start,
            retrieved=state.numbered,
        )
