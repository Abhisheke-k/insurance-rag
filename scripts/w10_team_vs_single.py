"""Week 10: race a manager + two specialists against the single agent, on the same claims.

    python -m scripts.w10_team_vs_single                          # offline stub model, reproducible
    python -m scripts.w10_team_vs_single --repeats 5              # steadier wall-clock medians
    python -m scripts.w10_team_vs_single --llm-provider anthropic --yes --repeats 1   # LIVE: spends money

The four contestants all use the same MCP tool server and the same corpus:

* ``fixed``        -- the Week 7 ship decision: one retrieve, one LLM call (reference rung only).
* ``single``       -- ``McpClaimsAgent``: the one agent, both tools, think/act/observe.
* ``team-lean``    -- ``ClaimsTeam``: a manager and an analyst + clerk specialists; hand-offs carry findings.
* ``team-full``    -- the same team, but every hand-off also carries the specialist's whole transcript.

The verdict is between ``single`` and ``team-*``; ``fixed`` shows where on the cost ladder both sit.

The same test set for everyone: the five labelled Week 7/8 scenarios
(``coursework/w8/trajectory_scenarios.jsonl``: expected decision *and* the clause that should ground it) plus
the 28 Week 6 notes (no expected decision, so they feed the label-free measures: assertions, judge v2,
agreement between pipelines, and tokens/cost/speed at a larger n).

What is and is not measured offline -- stated here so the numbers cannot be over-read:

* tokens are *estimated* from the real prompts and replies (see ``LLMUsage``), the same heuristic for every
  contestant; with a live provider they are the API's own counts;
* cost is tokens x list price; with the stub it is a projection ("what these exact prompts would cost");
* wall-clock includes tool calls, MCP, retrieval and orchestration but not model latency (the stub has none).
  ``critical path`` -- sequential model calls -- is the structural number that predicts live latency;
* with the stub, *quality* is equal by construction: every role bottoms out in the same simulated reading
  comprehension. The quality columns are therefore a plumbing check, not evidence about real models.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from mcp import StdioServerParameters

from app.assertions import run_assertions
from app.claims import ClaimSummaryGenerator
from app.config import Settings
from app.judge import ClaimSummaryJudge
from app.llm import build_llm_provider
from app.mcp_agent import McpClaimsAgent
from app.mcp_client import McpToolClient
from app.metering import PRICE_SOURCE, PRICES_USD_PER_MTOK, Meter, MeteredProvider, Totals, cost_usd, summarise
from app.rag import RagService
from app.registry import DocumentRegistry
from app.team import ClaimsTeam
from scripts.make_sample_pdf import build_sample_pdf
from scripts.w8_trajectory_eval import grade_scenario

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "data" / "sample_endorsement_pack.pdf"
LABELLED = ROOT / "coursework" / "w8" / "trajectory_scenarios.jsonl"
W6_CASES = ROOT / "coursework" / "w6" / "eval_cases.jsonl"
JUDGE_PROMPT = ROOT / "coursework" / "w6" / "judge_v2.txt"
OUT_DIR = ROOT / "coursework" / "w10"

PIPELINES = ("fixed", "single", "team-lean", "team-full")


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@dataclass
class Run:
    pipeline: str
    claim_id: str
    repeat: int
    seconds: float
    calls: int
    critical_path: int
    totals: Totals
    by_role: dict[str, Totals]
    round_trips: int
    decision: str
    summary: dict[str, Any]
    assertions_ok: bool
    judge_ok: bool | None = None
    expected: str | None = None
    outcome_ok: bool | None = None
    path_ok: bool | None = None
    path_reason: str = ""
    result: Any = field(default=None, repr=False)


@dataclass
class Contestant:
    name: str
    run: Callable[[str], Any]
    meter: Meter


def build_contestants(settings: Settings, client: McpToolClient, local: RagService, base_provider: Any) -> list[Contestant]:
    contestants: list[Contestant] = []

    meter = Meter()
    metered = MeteredProvider(base_provider, meter, role="fixed")
    generator = ClaimSummaryGenerator(settings, provider=metered)

    def run_fixed(notes: str) -> Any:
        retrieved = local.retrieve(notes, settings.top_k)
        summary = generator.summarize(notes, retrieved)
        return SimpleNamespace(summary=summary, steps=[], retrieved=retrieved, llm_calls=1, critical_path_calls=1, calls_by_role={"fixed": 1})

    contestants.append(Contestant("fixed", run_fixed, meter))

    meter = Meter()
    agent = McpClaimsAgent(settings, tools=client, provider=MeteredProvider(base_provider, meter, role="agent"))
    contestants.append(Contestant("single", agent.process, meter))

    for handoff in ("lean", "full"):
        meter = Meter()
        metered = MeteredProvider(base_provider, meter)
        team = ClaimsTeam(settings, tools=client, provider=lambda role, m=metered: m.with_role(role), handoff=handoff)
        contestants.append(Contestant(f"team-{handoff}", team.process, meter))
    return contestants


def _summary_dict(result: Any) -> dict[str, Any]:
    s = result.summary
    return {
        "claim_number": s.claim_number,
        "date_of_loss": s.date_of_loss,
        "coverage_decision": s.coverage_decision,
        "cited_exclusion_id": s.cited_exclusion_id,
        "excess_amount": s.excess_amount,
        "summary": s.summary,
        "citations": [c.chunk_id for c in s.citations],
    }


def measure(contestant: Contestant, client: McpToolClient, claim: dict[str, Any], repeat: int, judge: ClaimSummaryJudge | None) -> Run:
    mark = contestant.meter.mark()
    trips_before = sum(client.round_trips.values())
    started = time.perf_counter()
    result = contestant.run(claim["notes"])
    seconds = time.perf_counter() - started
    records = contestant.meter.since(mark)
    round_trips = sum(client.round_trips.values()) - trips_before

    summary = _summary_dict(result)
    run = Run(
        pipeline=contestant.name,
        claim_id=claim["id"],
        repeat=repeat,
        seconds=seconds,
        calls=len(records),
        critical_path=getattr(result, "critical_path_calls", None) or result.llm_calls,
        totals=summarise(records),
        by_role=contestant.meter.by_role(records),
        round_trips=round_trips,
        decision=result.summary.coverage_decision,
        summary=summary,
        assertions_ok=all(a.passed for a in run_assertions(summary)),
        result=result,
    )
    if judge is not None:
        run.judge_ok = judge.judge(claim["notes"], summary, result.retrieved).pass_criterion
    if claim.get("expected_coverage_decision"):
        run.expected = claim["expected_coverage_decision"]
        run.outcome_ok = run.decision == run.expected
        if contestant.name != "fixed":  # the fixed workflow has no steps to grade a trajectory on
            grade = grade_scenario(result, claim)
            run.path_ok, run.path_reason = grade.path_correct, grade.path_reason
    return run


# --------------------------------------------------------------------------- #
# Aggregation and rendering
# --------------------------------------------------------------------------- #
def _pct(numerator: int, denominator: int) -> str:
    return "n/a" if denominator == 0 else f"{numerator}/{denominator} ({numerator / denominator:.0%})"


def _row(*cells: Any) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def aggregate(runs: list[Run], pipeline: str, price_model: str) -> dict[str, Any]:
    mine = [r for r in runs if r.pipeline == pipeline]
    labelled = [r for r in mine if r.expected]
    judged = [r for r in mine if r.judge_ok is not None]
    total = sum((r.totals for r in mine), Totals())
    n = len(mine)
    return {
        "n": n,
        "outcome": (sum(bool(r.outcome_ok) for r in labelled), len(labelled)),
        "path": (sum(bool(r.path_ok) for r in labelled if r.path_ok is not None), sum(1 for r in labelled if r.path_ok is not None)),
        "assertions": (sum(r.assertions_ok for r in mine), n),
        "judge": (sum(bool(r.judge_ok) for r in judged), len(judged)),
        "median_seconds": statistics.median(r.seconds for r in mine),
        "median_model_seconds": statistics.median(r.totals.llm_seconds for r in mine),
        "round_trips": statistics.fmean(r.round_trips for r in mine),
        "mean_seconds": statistics.fmean(r.seconds for r in mine),
        "calls": total.calls / n,
        "critical_path": statistics.fmean(r.critical_path for r in mine),
        "input_tokens": total.input_tokens / n,
        "output_tokens": total.output_tokens / n,
        "total_tokens": total.total_tokens / n,
        "cost_per_claim": cost_usd(total, price_model) / n,
        "estimated": total.estimated_calls == total.calls,
    }


def agreement(runs: list[Run], pipeline: str, reference: str = "single") -> tuple[int, int]:
    ref = {(r.claim_id, r.repeat): r.decision for r in runs if r.pipeline == reference}
    mine = [r for r in runs if r.pipeline == pipeline]
    return sum(ref[(r.claim_id, r.repeat)] == r.decision for r in mine), len(mine)


def render(runs: list[Run], *, price_model: str, provider_name: str, repeats: int, claims: int) -> str:
    agg = {p: aggregate(runs, p, price_model) for p in PIPELINES}
    single = agg["single"]
    ratio = lambda p, key: f"{agg[p][key] / single[key]:.2f}x" if single[key] else "n/a"  # noqa: E731

    lines = [
        "# Week 10 -- manager + two specialists vs. the single agent: race tables",
        "",
        f"Generated by `python -m scripts.w10_team_vs_single`. LLM backend: `{provider_name}`. {claims} claims "
        f"(5 labelled Week 7/8 scenarios + 28 Week 6 notes) x {repeats} repeat(s), every contestant on the same claims, "
        "interleaved, after one discarded warm-up each. Tool server: `app/mcp_server.py` over stdio. "
        f"Cost = tokens x list price of `{price_model}` ({PRICE_SOURCE}).",
        "",
        "## The four numbers",
        "",
        _row("", "fixed (ref.)", "**single agent**", "team (lean hand-offs)", "team (full-context hand-offs)"),
        "|---|---|---|---|---|",
        _row("**Quality** -- decision correct (5 labelled)", *[_pct(*agg[p]["outcome"]) for p in PIPELINES]),
        _row("Quality -- right clause cited, 'path' (5 labelled)", *[_pct(*agg[p]["path"]) if agg[p]["path"][1] else "n/a" for p in PIPELINES]),
        _row("Quality -- all 4 deterministic assertions pass (all)", *[_pct(*agg[p]["assertions"]) for p in PIPELINES]),
        _row("Quality -- Week 6 judge v2 passes (all; noisy instrument)", *[_pct(*agg[p]["judge"]) for p in PIPELINES]),
        _row("Quality -- same decision as the single agent (all)", *[_pct(*agreement(runs, p)) for p in PIPELINES]),
        _row("**Speed** -- wall-clock per claim, median (ms)", *[f"{agg[p]['median_seconds'] * 1000:.1f}" for p in PIPELINES]),
        _row("Speed -- ... of which inside the model call (ms; the stub's CPU, not latency)", *[f"{agg[p]['median_model_seconds'] * 1000:.1f}" for p in PIPELINES]),
        _row("Speed -- ... outside it: tools, MCP, retrieval, orchestration (ms)", *[f"{(agg[p]['median_seconds'] - agg[p]['median_model_seconds']) * 1000:.1f}" for p in PIPELINES]),
        _row("Speed -- tool-server round trips per claim (list + call)", *[f"{agg[p]['round_trips']:.1f}" if p != "fixed" else "0 (in-process)" for p in PIPELINES]),
        _row("Speed -- sequential model calls on the critical path", *[f"{agg[p]['critical_path']:.1f}" for p in PIPELINES]),
        _row("**Tokens** -- model calls per claim", *[f"{agg[p]['calls']:.1f}" for p in PIPELINES]),
        _row("Tokens -- input per claim", *[f"{agg[p]['input_tokens']:,.0f}" for p in PIPELINES]),
        _row("Tokens -- output per claim", *[f"{agg[p]['output_tokens']:,.0f}" for p in PIPELINES]),
        _row("Tokens -- total per claim", *[f"{agg[p]['total_tokens']:,.0f} ({ratio(p, 'total_tokens')})" for p in PIPELINES]),
        _row(f"**Cost** -- per claim at `{price_model}`", *[f"${agg[p]['cost_per_claim']:.4f}" for p in PIPELINES]),
        _row("Cost -- per 1,000 claims", *[f"${agg[p]['cost_per_claim'] * 1000:,.2f} ({ratio(p, 'cost_per_claim')})" for p in PIPELINES]),
        "",
        "Ratios are against the single agent. "
        + ("Token counts are **estimated** (heuristic over the real prompts and replies), identically for every contestant. " if single["estimated"] else "Token counts are the API's own. ")
        + "Wall-clock excludes model latency when the backend is the offline stub.",
        "",
        "## Where the team's tokens go",
        "",
        _row("pipeline", "role", "calls / claim", "input tok / claim", "output tok / claim"),
        "|---|---|---|---|---|",
    ]
    for pipeline in ("single", "team-lean", "team-full"):
        mine = [r for r in runs if r.pipeline == pipeline]
        roles: dict[str, Totals] = defaultdict(Totals)
        for r in mine:
            for role, totals in r.by_role.items():
                roles[role] += totals
        for role in sorted(roles):
            t = roles[role]
            lines.append(_row(pipeline, role, f"{t.calls / len(mine):.1f}", f"{t.input_tokens / len(mine):,.0f}", f"{t.output_tokens / len(mine):,.0f}"))

    lines += ["", "## The five labelled claims, per pipeline (repeat 1)", "",
              _row("claim", "expected", "pipeline", "decision", "decision ok", "path ok", "calls", "total tokens", "path reason"), "|---|---|---|---|---|---|---|---|---|"]
    for r in runs:
        if r.repeat == 0 and r.expected:
            lines.append(_row(r.claim_id, r.expected, r.pipeline, r.decision, "yes" if r.outcome_ok else "no",
                              "n/a" if r.path_ok is None else ("yes" if r.path_ok else "no"), r.calls, f"{r.totals.total_tokens:,}", (r.path_reason or "")[:90]))

    # cost what-if: only meaningful because per-role tokens were measured
    lines += ["", "## What-if: who runs on which model (cost only)", "",
              "Re-prices the *measured* per-role tokens at other list prices. **Quality at a cheaper model was not measured** -- "
              "the stub says nothing about whether a small model can do a specialist's job. This table says only what the "
              "assignment would cost *if* it held.", "",
              _row("assignment", "cost / claim", "vs single @ " + price_model), "|---|---|---|"]
    base = agg["single"]["cost_per_claim"]

    def priced(pipeline: str, role_models: dict[str, str], default: str) -> float:
        mine = [r for r in runs if r.pipeline == pipeline]
        total = 0.0
        for r in mine:
            for role, totals in r.by_role.items():
                total += cost_usd(totals, role_models.get(role, default))
        return total / len(mine)

    options = [
        (f"single, all on {price_model}", priced("single", {}, price_model)),
        ("single, all on claude-sonnet-5-5", priced("single", {}, "claude-sonnet-5-5")),
        ("single, all on claude-haiku-5-5", priced("single", {}, "claude-haiku-5-5")),
        (f"team-lean, all on {price_model}", priced("team-lean", {}, price_model)),
        ("team-lean, all on claude-sonnet-5-5", priced("team-lean", {}, "claude-sonnet-5-5")),
        (f"team-lean, manager on {price_model}, specialists on claude-haiku-5-5", priced("team-lean", {"analyst": "claude-haiku-5-5", "clerk": "claude-haiku-5-5"}, price_model)),
        ("team-lean, manager on claude-sonnet-5-5, specialists on claude-haiku-5-5", priced("team-lean", {"analyst": "claude-haiku-5-5", "clerk": "claude-haiku-5-5"}, "claude-sonnet-5-5")),
    ]
    for label, value in options:
        lines.append(_row(label, f"${value:.4f}", f"{value / base:.2f}x"))

    single_by_key = {(s.claim_id, s.repeat): s.decision for s in runs if s.pipeline == "single"}

    def disagreements(pipeline: str) -> list[str]:
        return sorted({r.claim_id for r in runs if r.pipeline == pipeline and r.decision != single_by_key[(r.claim_id, r.repeat)]})

    lines += [
        "",
        f"Claims on which a team pipeline disagreed with the single agent: team-lean {disagreements('team-lean') or 'none'}, "
        f"team-full {disagreements('team-full') or 'none'}.",
        f"For reference, the fixed workflow disagreed with the single agent on {len(disagreements('fixed'))} of {len({r.claim_id for r in runs})} claims.",
        "",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--llm-provider", default="stub", choices=["stub", "anthropic", "openai", "auto"])
    parser.add_argument("--repeats", type=int, default=None, help="default 3 for the stub, 1 for a live provider")
    parser.add_argument("--price-model", default=None, help="price list to cost against (default: the configured ANSWER_MODEL)")
    parser.add_argument("--yes", action="store_true", help="required for a live provider: acknowledges this spends real money")
    parser.add_argument("--out-dir", type=Path, default=OUT_DIR)
    arguments = parser.parse_args()

    live = arguments.llm_provider != "stub"
    repeats = arguments.repeats or (1 if live else 3)
    labelled = [{**s, "notes": s["adjuster_notes"]} for s in _jsonl(LABELLED)]
    unlabelled = _jsonl(W6_CASES)
    claims = labelled + unlabelled
    if live:
        estimated_calls = len(claims) * repeats * (1 + 3 + 6 + 6)
        if not arguments.yes:
            sys.exit(f"refusing to run live without --yes: about {estimated_calls} model calls (plus judge calls) would be made")

    if not arguments.pdf.exists():
        build_sample_pdf(arguments.pdf)

    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        settings = Settings(
            embedding_provider="hashing", force_in_memory_store=True, chroma_dir=workdir / "host",
            llm_provider=arguments.llm_provider, top_k=5,  # type: ignore[arg-type]
        )
        base_provider = build_llm_provider(settings)
        if not base_provider.is_configured():
            sys.exit(f"LLM backend {arguments.llm_provider!r} is not configured: {base_provider.status().get('error')}")
        price_model = arguments.price_model or settings.answer_model
        if price_model not in PRICES_USD_PER_MTOK:
            sys.exit(f"no price on file for {price_model!r}; known: {sorted(PRICES_USD_PER_MTOK)}")

        local = RagService(settings, registry=DocumentRegistry(settings.registry_path))
        local.ingest(arguments.pdf.read_bytes(), arguments.pdf.name)
        judge = ClaimSummaryJudge(settings, JUDGE_PROMPT.read_text(encoding="utf-8"))
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "app.mcp_server", "--ingest", str(arguments.pdf)],
            env={"EMBEDDING_PROVIDER": "hashing", "FORCE_IN_MEMORY_STORE": "1", "CHROMA_DIR": str(workdir / "server")},
            cwd=str(ROOT),
        )

        runs: list[Run] = []
        with McpToolClient(params) as client:
            contestants = build_contestants(settings, client, local, base_provider)
            for contestant in contestants:  # one discarded warm-up each, so no contestant pays the cold-start
                measure(contestant, client, claims[0], -1, None)
            for repeat in range(repeats):
                for claim in claims:
                    for contestant in contestants:  # interleaved: every contestant sees the same moment's machine state
                        runs.append(measure(contestant, client, claim, repeat, judge))
                print(f"repeat {repeat + 1}/{repeats} done", flush=True)

    table = render(runs, price_model=price_model, provider_name=base_provider.name, repeats=repeats, claims=len(claims))
    print(table)
    arguments.out_dir.mkdir(parents=True, exist_ok=True)
    (arguments.out_dir / "race_tables.md").write_text(table, encoding="utf-8")
    raw = [
        {
            "pipeline": r.pipeline, "claim_id": r.claim_id, "repeat": r.repeat, "seconds": round(r.seconds, 6), "calls": r.calls,
            "critical_path": r.critical_path, "input_tokens": r.totals.input_tokens, "output_tokens": r.totals.output_tokens,
            "estimated": r.totals.estimated_calls == r.totals.calls, "decision": r.decision, "expected": r.expected,
            "outcome_ok": r.outcome_ok, "path_ok": r.path_ok, "assertions_ok": r.assertions_ok, "judge_ok": r.judge_ok,
            "by_role": {k: {"calls": v.calls, "input": v.input_tokens, "output": v.output_tokens} for k, v in r.by_role.items()},
        }
        for r in runs
    ]
    (arguments.out_dir / "race_raw.json").write_text(
        json.dumps({"llm_provider": base_provider.name, "price_model": price_model, "price_source": PRICE_SOURCE, "repeats": repeats, "runs": raw}, indent=2),
        encoding="utf-8",
    )
    print(f"\nwritten to {arguments.out_dir / 'race_tables.md'} and race_raw.json")


if __name__ == "__main__":
    main()
