"""Week 9: the claims desk's capabilities, exposed as an MCP server.

Until now every tool the claims agent could use was wired into
``app/agent.py`` by hand -- ``search_policy`` and ``check_assertions`` exist
only as branches inside one class, and no other application can use them. This
module publishes the same two capabilities (plus one exact-match lookup) over
the Model Context Protocol, so any MCP-speaking host -- this app's own agent,
another team's, Claude Code, Claude Desktop -- can discover and call them
without a line of integration code.

**Where the AI runs: not here.** This process has no language model. A tool
handler receives arguments, runs ordinary code (a vector search, four regexes,
a string scan) and returns data. It never sees the adjuster's notes unless a
caller chooses to put them in a query, never sees the agent's reasoning or
scratchpad, and cannot tell which model -- or whether any model -- is on the
other end; all it learns about its caller is the client name in the handshake.
The model lives in the *host* (``app/mcp_agent.py``), which decides which tool
to call and what to make of the answer. ``tests/test_mcp.py`` pins this: every
tool is exercised with every LLM backend patched to raise.

Everything exposed here is read-only on purpose. ``POST /ingest`` is **not** a
tool: Week 8 traced the prompt-injection attack to "anyone can add a document
to the corpus", and publishing ingestion to other people's agents would hand
them that capability directly.

Run it::

    python -m app.mcp_server                                   # stdio, for a host to spawn
    python -m app.mcp_server --transport http --port 8765      # needs MCP_AUTH_TOKENS
    python -m app.mcp_server --tools search_policy,check_assertions,lookup_exclusion_code
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections.abc import Callable, Collection, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp_types import ToolAnnotations
from pydantic import BaseModel, Field

from app import compliance
from app.assertions import run_assertions
from app.injection_guard import quarantine
from app.limits import LimitFigure, LimitsTable
from app.observability import JsonlSink
from app.mcp_access import CALL_PRINCIPAL, CALL_TRACE, AccessPolicy, BearerAuthMiddleware, ToolScopeMiddleware
from app.models import RetrievedChunk
from app.policy_registry import PolicyRegistry
from app.referrals import REASON_CODES, ReferralQueue

logger = logging.getLogger(__name__)

__all__ = [
    "ALL_TOOLS",
    "DEFAULT_TOOLS",
    "EVERY_TOOL",
    "DeskServices",
    "Passage",
    "PassageResult",
    "PolicyCorpus",
    "TRIAGE_TOOLS",
    "build_server",
    "build_server_for_service",
]

#: Enabled unless the operator says otherwise: the two capabilities the Week 7 agent had.
DEFAULT_TOOLS: tuple[str, ...] = ("search_policy", "check_assertions")
#: Week 12: the claims-triage desk. ``search_wording`` is the *dated* search -- it cannot be called without a policy number
#: and a loss date -- and ``refer_to_adjuster`` is the one tool that writes.
TRIAGE_TOOLS: tuple[str, ...] = ("lookup_policy", "coverage_in_force", "search_wording", "get_limits", "refer_to_adjuster")
#: The Week 9 set. ``lookup_exclusion_code`` is the "second tool": enabling it changes the *server's*
#: configuration and nothing in any agent.
ALL_TOOLS: tuple[str, ...] = (*DEFAULT_TOOLS, "lookup_exclusion_code")
#: Everything this module can serve, Week 9 and Week 12.
EVERY_TOOL: tuple[str, ...] = (*ALL_TOOLS, *TRIAGE_TOOLS)

_READ_ONLY = ToolAnnotations(
    read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
#: A write that is safe to retry: the referral id is derived from what is referred, so a second call is a no-op.
_WRITE_IDEMPOTENT = ToolAnnotations(
    read_only_hint=False, destructive_hint=False, idempotent_hint=True, open_world_hint=False
)
_POLICY_NUMBER_PATTERN = r"^[A-Z]{3}-\d{4}-\d{5}$"
_CLAIM_NUMBER_PATTERN = r"^CLM-\d{4}-\d{5}$"
_REFERENCE_PATTERN = r"^(?:CLM-\d{4}-\d{5}|[A-Z]{3}-\d{4}-\d{5})$"
_ISO_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
#: Enforced by the tool's own input schema, so a caller sees the constraint in `tools/list`.
_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,31}$"
_MAX_LOOKUP_RESULTS = 10


class Passage(BaseModel):
    """One retrieved policy passage -- everything a caller needs to cite it."""

    chunk_id: str
    doc_id: str
    filename: str
    page: int
    page_end: int
    section_path: str
    clause_label: str | None = None
    score: float
    text: str


class Withheld(BaseModel):
    """A passage the server declined to return. Provenance and reason only, never its text."""

    chunk_id: str
    filename: str
    page: int
    reason: str


class PassageResult(BaseModel):
    passages: list[Passage]
    withheld: list[Withheld] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PolicyCorpus:
    """The only two things the server needs from the app. No LLM, no generator, no agent."""

    retrieve: Callable[[str, int | None], list[RetrievedChunk]]
    all_chunks: Callable[[], list[RetrievedChunk]]
    #: ``(query, top_k, as_of, documents) -> chunks in force on as_of``. Only the Week 12 ``search_wording`` tool uses it.
    retrieve_as_of: Callable[[str, int | None, date | None, Collection[str] | None], list[RetrievedChunk]] | None = None


@dataclass(frozen=True, slots=True)
class DeskServices:
    """What the Week 12 tools read and write besides the wording: the registry, the limits table, the referral queue."""

    registry: PolicyRegistry
    limits: LimitsTable
    referrals: ReferralQueue


def _to_result(chunks: Sequence[RetrievedChunk], *, guard: bool) -> PassageResult:
    """Shape retrieved chunks for the wire, withholding any that read as instructions.

    Defence in depth: a caller should not have to trust that *its* host
    screens tool output. Flagged passages are reported by provenance and
    reason only (the suspect text is not repeated back).
    """
    withheld: list[Withheld] = []
    kept = list(chunks)
    if guard:
        kept, flags = quarantine(chunks)
        withheld = [Withheld(chunk_id=f.chunk_id, filename=f.filename, page=f.page, reason=f.reason) for f in flags]
    passages = [
        Passage(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            filename=c.filename,
            page=c.page,
            page_end=c.page_end,
            section_path=c.section_path,
            clause_label=c.clause_label,
            score=round(c.score, 4),
            text=c.text,
        )
        for c in kept
    ]
    return PassageResult(passages=passages, withheld=withheld)


def _register_search_policy(server: MCPServer, corpus: PolicyCorpus, *, guard: bool) -> None:
    @server.tool(
        name="search_policy",
        title="Search policy wording",
        description=(
            "Search the policy and endorsement corpus for passages about a phrase or topic. "
            "Returns up to top_k passages, best match first, each with its filename, page, "
            "section path and full text so it can be cited. If the results look off-topic or "
            "do not settle the question, search again with a more specific query. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def search_policy(
        query: Annotated[str, Field(min_length=1, max_length=2000, description="What to look for.")],
        top_k: Annotated[int, Field(ge=1, le=20, description="How many passages to return.")] = 5,
    ) -> PassageResult:
        return _to_result(corpus.retrieve(query, top_k), guard=guard)


def _register_check_assertions(server: MCPServer) -> None:
    @server.tool(
        name="check_assertions",
        title="Check a draft claim summary",
        description=(
            "Run four deterministic checks on a draft claim summary: claim_number matches "
            "CLM-YYYY-NNNNN; date_of_loss is present and parses; excess_amount, if given, is a "
            "non-negative number; and a 'denied' coverage_decision names a cited_exclusion_id. "
            "Arguments are the draft's fields. Returns one `name=ok|FAIL (detail)` entry per "
            "check, separated by '; '. Read-only; changes nothing."
        ),
        annotations=_READ_ONLY,
        structured_output=False,
    )
    def check_assertions(
        claim_number: Annotated[str, Field(max_length=64)] = "UNKNOWN",
        date_of_loss: Annotated[str, Field(max_length=64)] = "",
        coverage_decision: Annotated[str, Field(max_length=32)] = "needs_review",
        cited_exclusion_id: Annotated[str | None, Field(max_length=64)] = None,
        excess_amount: float | None = None,
    ) -> str:
        results = run_assertions(
            {
                "claim_number": claim_number,
                "date_of_loss": date_of_loss,
                "coverage_decision": coverage_decision,
                "cited_exclusion_id": cited_exclusion_id,
                "excess_amount": excess_amount,
            }
        )
        return "; ".join(f"{r.name}={'ok' if r.passed else 'FAIL'} ({r.detail})" for r in results)


def _register_lookup_exclusion_code(server: MCPServer, corpus: PolicyCorpus, *, guard: bool) -> None:
    @server.tool(
        name="lookup_exclusion_code",
        title="Look up an exact identifier",
        description=(
            "Find every passage that mentions an exact identifier -- an exclusion code such as "
            "'E-17', a form number such as 'HO-0304', or an edition string. Exact, case-insensitive "
            "match on the whole identifier. Use it when a topical search keeps returning "
            "near-miss editions of the same clause. Same result shape as search_policy. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def lookup_exclusion_code(
        code: Annotated[
            str,
            Field(
                pattern=_IDENTIFIER_PATTERN,
                description="The identifier, e.g. E-17: 1-32 letters, digits, '.' or '-'.",
            ),
        ],
    ) -> PassageResult:
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(code)}(?![A-Za-z0-9])", re.IGNORECASE)
        hits = [chunk for chunk in corpus.all_chunks() if pattern.search(f"{chunk.section_path}\n{chunk.text}")]
        exact = [
            RetrievedChunk(
                chunk_id=c.chunk_id,
                doc_id=c.doc_id,
                filename=c.filename,
                text=c.text,
                page=c.page,
                page_end=c.page_end,
                section_path=c.section_path,
                clause_label=c.clause_label,
                score=1.0,
            )
            for c in hits[:_MAX_LOOKUP_RESULTS]
        ]
        return _to_result(exact, guard=guard)


# --------------------------------------------------------------------------- #
# Week 12: the claims-triage desk
# --------------------------------------------------------------------------- #
class PolicyLookupOut(BaseModel):
    found: bool
    kind: Literal["claim", "policy", "unknown"]
    claim_number: str | None = None
    policy_number: str | None = None
    product: str | None = None
    period_start: str | None = None
    period_end: str | None = None
    cancelled_from: str | None = None
    policyholder_ref: str | None = None
    wording_documents: list[str] = Field(default_factory=list)
    wording_on_file: bool = False


class InForceOut(BaseModel):
    policy_number: str
    loss_date: str
    #: ``None`` = could not be determined (unknown policy) -- never read it as "not in force".
    in_force: bool | None
    reason_code: str
    detail: str
    period_start: str | None = None
    period_end: str | None = None
    cancelled_from: str | None = None


class FigureOut(BaseModel):
    amount: int | None
    currency: str = "GBP"
    basis: str | None = None
    source: str | None = None
    stated: str = "not_stated"
    effective_from: str | None = None
    effective_to: str | None = None
    note: str = ""
    candidates: list[FigureOut] = Field(default_factory=list)


class LimitsOut(BaseModel):
    """Three different numbers under three different names -- never merged into one 'limit'."""

    product: str
    loss_date: str
    peril: str
    deductible: FigureOut
    sub_limit: FigureOut
    policy_limit: FigureOut
    unresolved: list[str] = Field(default_factory=list)


class VersionOut(BaseModel):
    document: str
    edition: str | None = None
    effective_from: str | None = None
    effective_to: str | None = None


class WordingPassage(Passage):
    #: Which edition / endorsement this text is, and the dates it applied -- what a citation must say.
    versions: list[VersionOut] = Field(default_factory=list)


class WordingResult(BaseModel):
    passages: list[WordingPassage]
    withheld: list[Withheld] = Field(default_factory=list)
    policy_number: str
    #: The loss date the wording was resolved against.
    loss_date: str
    editions_in_force: list[str] = Field(default_factory=list)
    note: str = ""


class ReferralOut(BaseModel):
    referral_id: str
    status: Literal["queued"] = "queued"
    duplicate: bool
    queue: str


FigureOut.model_rebuild()


def _parse_day(value: str, field_name: str = "loss_date") -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{field_name} {value!r} is not a real calendar date (use YYYY-MM-DD)") from exc


def _figure_out(figure: LimitFigure) -> FigureOut:
    return FigureOut(
        amount=figure.amount,
        currency=figure.currency,
        basis=figure.basis,
        source=figure.source,
        stated=figure.stated,
        effective_from=figure.effective_from.isoformat() if figure.effective_from else None,
        effective_to=figure.effective_to.isoformat() if figure.effective_to else None,
        note=figure.note,
        candidates=[_figure_out(c) for c in figure.candidates],
    )


def _register_lookup_policy(server: MCPServer, corpus: PolicyCorpus, desk: DeskServices) -> None:
    @server.tool(
        name="lookup_policy",
        title="Find the policy for a claim",
        description=(
            "Given a claim number (CLM-YYYY-NNNNN) or a policy number (AAA-YYYY-NNNNN), return the policy it sits on: "
            "product, period of insurance, any cancellation date, and the wording documents it was issued on (and whether "
            "that wording is on file). Contains no personal data. An unknown reference returns found=false -- do not guess "
            "a policy. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def lookup_policy(
        reference: Annotated[str, Field(pattern=_REFERENCE_PATTERN, description="A claim number or a policy number.")],
    ) -> PolicyLookupOut:
        found = desk.registry.lookup(reference)
        if found.policy is None:
            return PolicyLookupOut(found=False, kind="unknown")
        policy = found.policy
        on_file = {chunk.filename for chunk in corpus.all_chunks()}
        return PolicyLookupOut(
            found=True,
            kind=found.kind,
            claim_number=found.claim_number,
            policy_number=policy.policy_number,
            product=policy.product,
            period_start=policy.period_start.isoformat(),
            period_end=policy.period_end.isoformat(),
            cancelled_from=policy.cancelled_from.isoformat() if policy.cancelled_from else None,
            policyholder_ref=policy.policyholder_ref,
            wording_documents=list(policy.wording_documents),
            wording_on_file=bool(policy.wording_documents) and all(name in on_file for name in policy.wording_documents),
        )


def _register_coverage_in_force(server: MCPServer, desk: DeskServices) -> None:
    @server.tool(
        name="coverage_in_force",
        title="Was the policy in force on the loss date?",
        description=(
            "Check whether a policy was in force on a given loss date (YYYY-MM-DD): inside the period of insurance and not "
            "cancelled by then. Returns in_force true/false with a reason_code (in_force, before_inception, after_expiry, "
            "cancelled), or in_force=null for an unknown policy -- null means 'could not be determined', not 'no'. This is a "
            "date check on the policy record only; it says nothing about whether any loss is covered. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def coverage_in_force(
        policy_number: Annotated[str, Field(pattern=_POLICY_NUMBER_PATTERN)],
        loss_date: Annotated[str, Field(pattern=_ISO_DATE_PATTERN, description="Date of loss, YYYY-MM-DD.")],
    ) -> InForceOut:
        result = desk.registry.coverage_in_force(policy_number, _parse_day(loss_date))
        return InForceOut(
            policy_number=result.policy_number,
            loss_date=result.loss_date.isoformat(),
            in_force=result.in_force,
            reason_code=result.reason_code,
            detail=result.detail,
            period_start=result.period_start.isoformat() if result.period_start else None,
            period_end=result.period_end.isoformat() if result.period_end else None,
            cancelled_from=result.cancelled_from.isoformat() if result.cancelled_from else None,
        )


def _register_search_wording(server: MCPServer, corpus: PolicyCorpus, desk: DeskServices, *, guard: bool) -> None:
    @server.tool(
        name="search_wording",
        title="Search the wording in force on the loss date",
        description=(
            "Search a policy's wording for passages about a topic, returning ONLY wording that was in force on the loss "
            "date (YYYY-MM-DD): superseded editions and endorsements not yet effective are removed by the server. Each "
            "passage lists the edition or endorsement it belongs to and the dates it applied, so a citation can name them. "
            "Query with the circumstances of the loss in general terms (the peril, the clause topic, an exclusion code) -- "
            "never names, addresses or injuries. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def search_wording(
        query: Annotated[str, Field(min_length=1, max_length=2000, description="What to look for, in general terms.")],
        policy_number: Annotated[str, Field(pattern=_POLICY_NUMBER_PATTERN)],
        loss_date: Annotated[str, Field(pattern=_ISO_DATE_PATTERN, description="Date of loss, YYYY-MM-DD.")],
        top_k: Annotated[int, Field(ge=1, le=10)] = 4,
    ) -> WordingResult:
        day = _parse_day(loss_date)
        policy = desk.registry.policy(policy_number)
        base = {"policy_number": policy_number, "loss_date": day.isoformat()}
        if policy is None:
            return WordingResult(passages=[], note="no such policy in the registry; nothing was searched", **base)
        if not policy.wording_documents:
            return WordingResult(passages=[], note="no wording is on file for this policy; nothing was searched", **base)
        if corpus.retrieve_as_of is None:
            raise RuntimeError("this server was built without loss-date retrieval")
        hits = corpus.retrieve_as_of(query, top_k, day, policy.wording_documents)
        kept, flags = quarantine(hits) if guard else (list(hits), [])
        passages = [
            WordingPassage(
                chunk_id=c.chunk_id, doc_id=c.doc_id, filename=c.filename, page=c.page, page_end=c.page_end,
                section_path=c.section_path, clause_label=c.clause_label, score=round(c.score, 4), text=c.text,
                versions=[VersionOut(**tag.to_dict()) for tag in c.versions],
            )
            for c in kept
        ]
        editions = sorted({tag.edition for c in kept for tag in c.versions if tag.edition})
        withheld = [Withheld(chunk_id=f.chunk_id, filename=f.filename, page=f.page, reason=f.reason) for f in flags]
        return WordingResult(passages=passages, withheld=withheld, editions_in_force=editions, **base)


def _register_get_limits(server: MCPServer, desk: DeskServices) -> None:
    @server.tool(
        name="get_limits",
        title="Deductible, sub-limit and policy limit",
        description=(
            "Return the three money figures that apply to a loss on a given date, as three separate fields: deductible "
            "(what the insured bears each and every loss), sub_limit (the cap for that peril, usually an annual aggregate) and "
            "policy_limit (the overall cap per occurrence). They are different numbers and must never be merged or "
            "substituted for one another. Each carries its basis, source clause and the dates it applied. If the deductible "
            "depends on a fact you do not have (in_flood_zone), it is returned null with both candidates and the missing "
            "fact listed in 'unresolved' -- do not pick one. Read-only."
        ),
        annotations=_READ_ONLY,
        structured_output=True,
    )
    def get_limits(
        policy_number: Annotated[str, Field(pattern=_POLICY_NUMBER_PATTERN)],
        loss_date: Annotated[str, Field(pattern=_ISO_DATE_PATTERN, description="Date of loss, YYYY-MM-DD.")],
        peril: Annotated[Literal["flood", "other"], Field(description="'flood' for flood / escape of water from a watercourse, else 'other'.")],
        in_flood_zone: Annotated[bool | None, Field(description="Is the insured location in a Flood Zone? Omit if unknown.")] = None,
    ) -> LimitsOut:
        day = _parse_day(loss_date)
        policy = desk.registry.policy(policy_number)
        if policy is None:
            raise ToolError("no such policy in the registry")
        if not policy.wording_documents:
            # The table is transcribed from an issued wording; with none on file for this policy there is nothing to verify it against.
            none = FigureOut(amount=None, note="no wording is on file for this policy, so no figure can be verified against it")
            return LimitsOut(
                product=policy.product, loss_date=day.isoformat(), peril=peril,
                deductible=none, sub_limit=none, policy_limit=none,
            )
        result = desk.limits.resolve(policy.product, day, peril, in_flood_zone, documents=policy.wording_documents)
        return LimitsOut(
            product=result.product,
            loss_date=day.isoformat(),
            peril=result.peril,
            deductible=_figure_out(result.deductible),
            sub_limit=_figure_out(result.sub_limit),
            policy_limit=_figure_out(result.policy_limit),
            unresolved=list(result.unresolved),
        )


def _register_refer_to_adjuster(server: MCPServer, desk: DeskServices) -> None:
    @server.tool(
        name="refer_to_adjuster",
        title="Refer the claim to an adjuster",
        description=(
            "Queue the claim for a human adjuster, who makes every coverage decision. Call it once, last, with the claim "
            "number, the reason codes that tell the adjuster where to look first, and a short factual note (dates, "
            "figures, which wording applies). The note must be neutral facts: it is refused if it states or implies a "
            "coverage decision (accepted, declined, payable, excluded, or the word 'covered'). Do not put names, addresses "
            "or injuries in it. Safe to retry: a repeat of the same referral returns the same id. Writes to the referral queue."
        ),
        annotations=_WRITE_IDEMPOTENT,
        structured_output=True,
    )
    def refer_to_adjuster(
        claim_number: Annotated[str, Field(pattern=_CLAIM_NUMBER_PATTERN)],
        reason_codes: Annotated[list[Literal[REASON_CODES]], Field(min_length=1, max_length=6, description="Why, in order of importance.")],  # type: ignore[valid-type]
        policy_number: Annotated[str | None, Field(pattern=_POLICY_NUMBER_PATTERN)] = None,
        loss_date: Annotated[str | None, Field(pattern=_ISO_DATE_PATTERN)] = None,
        priority: Literal["routine", "urgent"] = "routine",
        facts: Annotated[str, Field(max_length=1500, description="Neutral facts for the adjuster. No decision, no personal details.")] = "",
        wording_refs: Annotated[list[Annotated[str, Field(max_length=200)]], Field(max_length=10, description="Edition / clause references the adjuster should read.")] = [],  # noqa: B006
    ) -> ReferralOut:
        if loss_date is not None:
            _parse_day(loss_date)
        for label, text in [("facts", facts), *[("wording_refs", ref) for ref in wording_refs]]:
            problems = compliance.violations(text)
            if problems:
                raise ToolError(
                    f"{label} may not state a coverage decision ({problems[0].why}); the adjuster decides. Rewrite as neutral facts."
                )
        inherited = CALL_TRACE.get()
        receipt = desk.referrals.add(
            claim_number=claim_number,
            policy_number=policy_number,
            loss_date=loss_date,
            reason_codes=list(reason_codes),
            priority=priority,
            facts=facts,
            wording_refs=list(wording_refs),
            trace_id=inherited[0] if inherited else None,
            principal=CALL_PRINCIPAL.get() or "unknown",
        )
        return ReferralOut(referral_id=receipt.referral_id, duplicate=receipt.duplicate, queue=receipt.queue)


def build_server(
    corpus: PolicyCorpus,
    *,
    enabled: Sequence[str] = DEFAULT_TOOLS,
    access: AccessPolicy | None = None,
    transport: str = "stdio",
    guard_outputs: bool = True,
    trace_sink: JsonlSink | None = None,
    desk: DeskServices | None = None,
) -> MCPServer:
    """Assemble the MCP server for ``corpus``.

    ``enabled`` is the operator's list of tools to publish -- the single place a
    new capability is switched on. ``access`` is required for the HTTP
    transport and ignored for stdio, where the launching process is the only
    possible caller. ``desk`` carries the registry, limits table and referral
    queue the Week 12 triage tools need; enabling one of them without it is an error.
    """
    unknown = [name for name in enabled if name not in EVERY_TOOL]
    if unknown:
        raise ValueError(f"unknown tool(s) {unknown}; this server can publish {list(EVERY_TOOL)}")
    needs_desk = [name for name in enabled if name in TRIAGE_TOOLS]
    if needs_desk and desk is None:
        raise ValueError(f"tool(s) {needs_desk} need a DeskServices (policy registry, limits table, referral queue)")

    server = MCPServer(
        "claims-desk-policy",
        title="Claims desk policy tools",
        instructions=(
            "Tools over an insurance policy and endorsement corpus: topical search, an exact-identifier lookup, "
            "deterministic checks on a draft claim summary and, for claims triage, policy lookup, an in-force check, "
            "loss-date-scoped wording search, the deductible / sub-limit / policy-limit figures and a referral queue. "
            "Nothing here calls a language model or modifies the corpus; the only tool that writes is "
            "refer_to_adjuster, which appends to the referral queue. No tool decides coverage."
        ),
        version="1.1.0",
        middleware=[ToolScopeMiddleware(access, transport=transport, trace_sink=trace_sink)],
    )
    if "search_policy" in enabled:
        _register_search_policy(server, corpus, guard=guard_outputs)
    if "check_assertions" in enabled:
        _register_check_assertions(server)
    if "lookup_exclusion_code" in enabled:
        _register_lookup_exclusion_code(server, corpus, guard=guard_outputs)
    if desk is not None:
        if "lookup_policy" in enabled:
            _register_lookup_policy(server, corpus, desk)
        if "coverage_in_force" in enabled:
            _register_coverage_in_force(server, desk)
        if "search_wording" in enabled:
            _register_search_wording(server, corpus, desk, guard=guard_outputs)
        if "get_limits" in enabled:
            _register_get_limits(server, desk)
        if "refer_to_adjuster" in enabled:
            _register_refer_to_adjuster(server, desk)
    return server


def build_desk(referral_path: Path | None = None) -> DeskServices:
    """The registry and limits table shipped in ``data/`` and a referral queue writing to ``referral_path``."""
    return DeskServices(PolicyRegistry.from_file(), LimitsTable.from_file(), ReferralQueue(referral_path))


def build_server_for_service(service: Any, *, desk: DeskServices | None = None, **kwargs: Any) -> MCPServer:
    """Bind the server to a :class:`~app.rag.RagService` -- retrieval only; nothing else is passed in."""
    corpus = PolicyCorpus(
        retrieve=service.retrieve, all_chunks=service.store.all_chunks, retrieve_as_of=service.retrieve_as_of
    )
    return build_server(corpus, desk=desk, **kwargs)


# --------------------------------------------------------------------------- #
# Command line
# --------------------------------------------------------------------------- #
def _is_loopback(host: str) -> bool:
    return host in {"127.0.0.1", "localhost", "::1"}


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--tools",
        default=os.environ.get("MCP_TOOLS", ",".join(DEFAULT_TOOLS)),
        help=f"comma-separated tools to publish (env MCP_TOOLS); available: {', '.join(EVERY_TOOL)}",
    )
    parser.add_argument(
        "--ingest",
        action="append",
        type=Path,
        default=[],
        metavar="PDF",
        help="ingest this PDF at startup (repeatable); operator-side only, never exposed as a tool",
    )
    parser.add_argument(
        "--trace-log",
        type=Path,
        default=Path(os.environ["MCP_TRACE_LOG"]) if os.environ.get("MCP_TRACE_LOG") else None,
        help="append this server's span records (trace id, tool, duration; never arguments) here (env MCP_TRACE_LOG)",
    )
    parser.add_argument(
        "--referral-log",
        type=Path,
        default=Path(os.environ["REFERRAL_LOG"]) if os.environ.get("REFERRAL_LOG") else Path("./logs/referrals.jsonl"),
        help="where refer_to_adjuster appends its queue (env REFERRAL_LOG); only used when that tool is enabled",
    )
    parser.add_argument(
        "--no-output-guard",
        action="store_true",
        help="DEMONSTRATION ONLY: do not withhold injection-shaped passages (reproduces an unscreened third-party server)",
    )
    arguments = parser.parse_args(argv)

    # stdout belongs to the protocol on stdio: every log line goes to stderr.
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)-8s %(name)s: %(message)s"
    )

    from app.config import get_settings  # noqa: PLC0415 -- keep `import app.mcp_server` cheap
    from app.rag import RagService  # noqa: PLC0415

    # The server never calls a model, so it never holds model credentials: scrub any
    # key inherited from the environment and pin the (unused) LLM backend to the
    # offline stub, so no real client is even constructed in this process.
    settings = get_settings().model_copy(
        update={"llm_provider": "stub", "anthropic_api_key": None, "openai_api_key": None}
    )
    service = RagService(settings)
    for pdf in arguments.ingest:
        result = service.ingest(pdf.read_bytes(), pdf.name)
        logger.info("ingested %s: %d chunks", pdf.name, result.chunks)

    enabled = [name.strip() for name in arguments.tools.split(",") if name.strip()]
    trace_sink = JsonlSink(arguments.trace_log) if arguments.trace_log else None
    desk = build_desk(arguments.referral_log) if any(name in TRIAGE_TOOLS for name in enabled) else None

    if arguments.transport == "stdio":
        build_server_for_service(
            service,
            desk=desk,
            enabled=enabled,
            transport="stdio",
            guard_outputs=not arguments.no_output_guard,
            trace_sink=trace_sink,
        ).run("stdio")
        return

    raw_tokens = os.environ.get("MCP_AUTH_TOKENS")
    if not raw_tokens:
        parser.error("--transport http requires MCP_AUTH_TOKENS (a JSON object of bearer token -> principal)")
    policy = AccessPolicy.from_json(raw_tokens)
    if not _is_loopback(arguments.host):
        logger.warning("binding %s: terminate TLS in front of this server; bearer tokens are sent in clear", arguments.host)

    import uvicorn  # noqa: PLC0415

    server = build_server_for_service(
        service,
        desk=desk,
        enabled=enabled,
        access=policy,
        transport="http",
        guard_outputs=not arguments.no_output_guard,
        trace_sink=trace_sink,
    )
    app = BearerAuthMiddleware(server.streamable_http_app(host=arguments.host), policy)
    uvicorn.run(app, host=arguments.host, port=arguments.port, log_level="warning")


if __name__ == "__main__":
    main()
