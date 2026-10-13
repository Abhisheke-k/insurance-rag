"""Week 11: cost per query by stage, what the fix costs, and what breaks first at 10x -- all from trace logs.

    python -m scripts.w11_cost_report            # writes coursework/w11/cost_by_stage.md and tenx.md

Three questions, each answered from measurements rather than recollection:

1. **Where does one request's cost go?** The found trace (``coursework/w11/trace.json``) and the 330-request drill log
   are broken down by stage -- retrieval / generation / tools -- from their spans. Attribution comes *first*: an
   optimisation made before knowing which stage the money is in is the brief's first common mistake.
2. **What did the fix add?** The same 33 notes (Weeks 6 and 7) are run with ``carry_clause_stem`` off and on, tracing to
   a log each time, and the per-request token and cost difference is read off the spans.
3. **At 10x, what breaks first -- cost, latency or a rate limit?** Demand is computed from the measured tokens per
   request and an explicit volume assumption; local capacity is measured (requests/second through the real pipeline).

The model is the offline stub, so *tokens are estimated* (heuristic over the real prompts) and *cost is a projection* at
the configured model's list price (see ``app/metering.py``); latency is the real pipeline's wall-clock minus any model
time, because the stub's model call takes microseconds. Every one of those caveats is repeated in the output files.
"""

from __future__ import annotations

import json
import statistics
import tempfile
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.logquery import load_records
from scripts.w11_traffic import build_service

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w11"
DRILL_LOG = ROOT / ".drill" / "traces.jsonl"
FOUND = OUT / "trace.json"
NOTES_FILES = [ROOT / "coursework" / "w7" / "claim_scenarios.jsonl", ROOT / "coursework" / "w6" / "eval_cases.jsonl"]

#: Volume assumptions, stated rather than hidden. 1,800 FNOLs/month is the Week 12 Variant D brief's own figure.
TODAY_PER_MONTH = 1_800
GROWTH = 10
BUSINESS_DAYS = 22
HOURS_PER_DAY = 8
PEAK_FACTOR = 3.0  # the busiest hour carries 3x the average hour


def stage_totals(record: dict[str, Any]) -> dict[str, dict[str, float]]:
    stages: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0, "ms": 0.0, "usd": 0.0, "tin": 0, "tout": 0})
    for s in record["spans"]:
        kind = s["kind"] if s["kind"] in ("retrieval", "generation", "tool", "verify") else "other"
        stage = stages[kind]
        stage["n"] += 1
        stage["ms"] += s["duration_ms"]
        stage["usd"] += float(s["attrs"].get("cost_usd") or 0.0)
        stage["tin"] += int(s["attrs"].get("input_tokens") or 0)
        stage["tout"] += int(s["attrs"].get("output_tokens") or 0)
    accounted = sum(v["ms"] for v in stages.values())
    stages["unattributed"]["ms"] = max(0.0, record["duration_ms"] - accounted)
    return stages


def stage_table(record: dict[str, Any]) -> list[str]:
    stages = stage_totals(record)
    total_usd = sum(v["usd"] for v in stages.values()) or 1e-12
    total_ms = record["duration_ms"] or 1e-12
    rows = ["| stage | spans | latency (ms) | % of latency | input tok | output tok | cost (USD) | % of cost |", "|---|---|---|---|---|---|---|---|"]
    for name in ("retrieval", "generation", "tool", "verify", "other", "unattributed"):
        if name not in stages and name not in ("tool",):
            continue
        v = stages.get(name, {"n": 0, "ms": 0.0, "usd": 0.0, "tin": 0, "tout": 0})
        label = {"tool": "tools", "other": "other spans", "unattributed": "unattributed (orchestration)"}.get(name, name)
        rows.append(
            f"| {label} | {int(v['n'])} | {v['ms']:.2f} | {v['ms'] / total_ms:.0%} | {int(v['tin']):,} | {int(v['tout']):,} | ${v['usd']:.5f} | {v['usd'] / total_usd:.0%} |"
        )
    rows.append(f"| **request total** | | **{record['duration_ms']:.2f}** | 100% | **{record['cost']['input_tokens']:,}** | **{record['cost']['output_tokens']:,}** | **${record['cost']['usd']:.5f}** | 100% |")
    return rows


def span_table(record: dict[str, Any]) -> list[str]:
    rows = ["| t+ (ms) | span | kind | duration (ms) | detail |", "|---|---|---|---|---|"]
    for s in record["spans"]:
        a = s["attrs"]
        if s["kind"] == "generation":
            detail = f"model `{a.get('model')}`, prompt `{a.get('prompt_version')}`, {a.get('input_tokens')} in / {a.get('output_tokens')} out tokens, ${a.get('cost_usd')} ({a.get('cost_basis')})"
        elif s["kind"] == "retrieval":
            detail = f"mode `{a.get('mode')}`, top_k {a.get('top_k')}, n={a.get('n')}, query_digest `{a.get('query_digest')}`"
        else:
            detail = ", ".join(f"{k}={v}" for k, v in a.items())
        rows.append(f"| {s['start_ms']:.2f} | {s['name']} | {s['kind']} | {s['duration_ms']:.2f} | {detail} |")
    return rows


def per_feature(records: list[dict[str, Any]]) -> list[str]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in records:
        if r.get("schema") == "trace/1":
            groups[r["feature"]].append(r)
    rows = [
        "| feature | n | gated (no model call) | mean cost | p95 cost | mean tokens in/out | mean latency (ms) | retrieval ms | generation ms (stub) | tools ms |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for feature, group in sorted(groups.items()):
        costs = sorted(g["cost"]["usd"] for g in group)
        stages = [stage_totals(g) for g in group]
        mean = lambda xs: statistics.fmean(xs)  # noqa: E731
        rows.append(
            f"| {feature} | {len(group)} | {sum(1 for g in group if (g['output'] or {}).get('gated'))} | ${mean(costs):.4f} | ${costs[int(len(costs) * 0.95) - 1]:.4f} | "
            f"{mean(g['cost']['input_tokens'] for g in group):,.0f} / {mean(g['cost']['output_tokens'] for g in group):,.0f} | "
            f"{mean(g['duration_ms'] for g in group):.2f} | {mean(s['retrieval']['ms'] if 'retrieval' in s else 0 for s in stages):.2f} | "
            f"{mean(s['generation']['ms'] if 'generation' in s else 0 for s in stages):.2f} | {mean(s['tool']['ms'] if 'tool' in s else 0 for s in stages):.2f} |"
        )
    return rows


def run_notes(carry: bool, notes: list[str], **overrides: Any) -> tuple[list[dict[str, Any]], float]:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "t.jsonl"
        service = build_service(Path(tmp), log, carry_clause_stem=carry, **overrides)
        started = time.perf_counter()
        for n in notes:
            service.summarize_claim(n, user="cost-report", input_type="fnol_note")
        elapsed = time.perf_counter() - started
        return list(load_records([log])), elapsed


def main() -> None:
    notes: list[str] = []
    for path in NOTES_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                notes.append(row.get("adjuster_notes") or row["notes"])

    found = json.loads(FOUND.read_text(encoding="utf-8"))
    drill = [r for r in load_records([DRILL_LOG]) if r.get("schema") == "trace/1"]
    off, off_s = run_notes(False, notes)
    on, on_s = run_notes(True, notes)
    shipped, _ = run_notes(True, notes, claim_top_k=4)  # the system as it now ships: lead-in fix + 4 passages

    def mean_cost(records: list[dict[str, Any]], key: str) -> float:
        return statistics.fmean(r["cost"][key] for r in records)

    extra_in = mean_cost(on, "input_tokens") - mean_cost(off, "input_tokens")
    extra_usd = mean_cost(on, "usd") - mean_cost(off, "usd")
    stems = [s["attrs"].get("stems_carried", 0) for r in on for s in r["spans"] if s["kind"] == "retrieval"]
    carried_requests = sum(1 for n in stems if n)

    fixed_cost = statistics.fmean(r["cost"]["usd"] for r in drill if r["feature"] == "claim_summary")
    agent_cost = statistics.fmean(r["cost"]["usd"] for r in drill if r["feature"] == "claims_agent")
    fixed_tok = statistics.fmean(r["cost"]["input_tokens"] + r["cost"]["output_tokens"] for r in drill if r["feature"] == "claim_summary")
    agent_tok = statistics.fmean(r["cost"]["input_tokens"] + r["cost"]["output_tokens"] for r in drill if r["feature"] == "claims_agent")
    trace_bytes = statistics.fmean(len(json.dumps(r)) for r in drill)

    # local capacity: requests/second through the real pipeline (stub model), tracing on
    throughput = len(notes) / on_s

    volume = TODAY_PER_MONTH * GROWTH
    per_hour = volume / (BUSINESS_DAYS * HOURS_PER_DAY)
    peak_per_min = per_hour * PEAK_FACTOR / 60
    tpm_fixed, tpm_agent = peak_per_min * fixed_tok, peak_per_min * agent_tok

    cost_md = [
        "# Week 11 -- cost per query, by stage",
        "",
        "Generated by `python -m scripts.w11_cost_report` from trace logs. **Caveats that apply to every number below:** the model is the "
        "offline stub, so tokens are *estimated* (a heuristic over the real prompts and replies) and cost is a *projection* at the configured "
        "model's list price (`claude-opus-5`, $5/$25 per million tokens in/out); the stub's model call takes microseconds, so "
        "latency is dominated by retrieval and orchestration, not by a model. The *shape* (which stage the money is in) is robust; the absolute "
        "latency of a live model call is not measured.",
        "",
        "## 1. The found request (round-1 trace, `trace.json`)",
        "",
        f"Trace `{found['trace_id']}`, feature `{found['feature']}`, prompt version(s) `{', '.join(found['prompt_versions'])}`, "
        f"{len(found['context_ids'])} retrieved context ids: `{', '.join(c.split('::')[1] for c in found['context_ids'])}`.",
        "",
        *stage_table(found),
        "",
        "Per span:",
        "",
        *span_table(found),
        "",
        "**Reading it.** Generation is the whole of the cost. Retrieval costs nothing per token here -- the embedder is the local hashing "
        "model, so a retrieval span is CPU time only (a hosted embedder would add a per-input-token charge to this row). The fixed workflow calls "
        "no tools, so the tools row is empty; the agent's tools row appears in the next table. 'Unattributed' is request overhead outside any "
        "span (building the prompt, parsing the reply, citation verification, writing the trace).",
        "",
        f"## 2. Across all {len(drill)} requests in the drill log",
        "",
        *per_feature(drill),
        "",
        "The agent costs ~3x the fixed workflow per request because it makes three model calls and re-sends the retrieved passages on turns 2 and 3 "
        "(Week 7 measured the same 3x in calls; here it is in tokens and dollars).",
        "",
        "**`ask` costs $0 because every one of its requests was gated -- a bug this table exposed, not a saving.** The drill log was generated "
        "in hybrid retrieval mode, which reports fused RRF scores (~0.033 at most) while the relevance gate compares against a cosine floor of 0.12, "
        "so no question ever reached the model and `output.found` was false for all of them. Fixed afterwards with a separate hybrid floor "
        "(`MIN_RELEVANCE_HYBRID`, `tests/test_hybrid_gate.py`); this log predates the fix, which is why the row still reads zero.",
        "",
        "## 3. What the retrieval fix adds",
        "",
        f"The same {len(notes)} notes (Week 7 scenarios + Week 6 cases) through `summarize_claim` with `carry_clause_stem` off and on, tracing each:",
        "",
        "| | fix off | fix on | difference |",
        "|---|---|---|---|",
        f"| mean input tokens / request | {mean_cost(off, 'input_tokens'):,.0f} | {mean_cost(on, 'input_tokens'):,.0f} | **+{extra_in:,.0f} ({extra_in / mean_cost(off, 'input_tokens'):.1%})** |",
        f"| mean cost / request (projected) | ${mean_cost(off, 'usd'):.5f} | ${mean_cost(on, 'usd'):.5f} | **+${extra_usd:.5f}** (+${extra_usd * 1000:.2f} per 1,000 requests) |",
        f"| requests that had a lead-in carried | | {carried_requests} of {len(notes)} | |",
        "",
        "The fix is cheap because it only touches continuation chunks of list clauses (3 of the corpus's 9 chunks) and adds one sentence to each; "
        "it is paid for only on requests that retrieve one.",
        "",
    ]
    (OUT / "cost_by_stage.md").write_text("\n".join(cost_md), encoding="utf-8")

    shipped_ratio = mean_cost(shipped, "input_tokens") / mean_cost(off, "input_tokens") - 1
    month_fixed, month_agent = volume * fixed_cost, volume * agent_cost
    tenx = [
        f"**At 10x ({volume:,} claims/month, about {peak_per_min:.1f} a minute in the busiest hour) the provider's rate limit breaks first, not cost or latency: "
        f"peak demand is ~{tpm_fixed / 1000:.0f}k tokens/min on the fixed workflow and ~{tpm_agent / 1000:.0f}k on the agent, against a projected "
        f"${month_fixed:,.0f}-${month_agent:,.0f}/month and a pipeline that handles ~{throughput:,.0f} requests/second locally; "
        f"I did not verify the account's actual limit, so ~{tpm_agent / 1000:.0f}k tokens/min is the number to check it against.**",
        "",
        "# Week 11 -- what breaks first at 10x",
        "",
        "## The one-line answer and why it is *by elimination*",
        "",
        "Cost and latency can be bounded from measurements; a rate limit cannot, because it is a number in a contract I cannot see. "
        "So the claim is: cost and latency are demonstrably not binding at 10x, which leaves the rate limit as the first constraint that could be -- "
        "and the table says what number it would have to clear.",
        "",
        f"**Which system these numbers describe:** the drill-era system (5 passages per claim, no lead-in fix), i.e. the traffic in "
        f"`.drill/traces.jsonl`. The system as it now ships (lead-in fix + 4 passages, `cost_improvement.md`) sends **{shipped_ratio:+.1%}** input tokens per "
        "claim on the same notes, so every demand figure below is, if anything, slightly conservative.",
        "",
        "## Working (all inputs stated)",
        "",
        "| | value | source |",
        "|---|---|---|",
        f"| volume today | {TODAY_PER_MONTH:,} claims/month | the Week 12 Variant D brief (assumed to be this app's volume) |",
        f"| volume at 10x | {volume:,} claims/month | |",
        f"| average hour | {per_hour:.0f} claims ({BUSINESS_DAYS} business days x {HOURS_PER_DAY} h) | assumption |",
        f"| busiest hour | {per_hour * PEAK_FACTOR:.0f} claims = **{peak_per_min:.1f}/min** ({PEAK_FACTOR:.0f}x the average hour) | assumption |",
        f"| tokens per request, fixed workflow | {fixed_tok:,.0f} (in+out, estimated) | mean of {sum(1 for r in drill if r['feature'] == 'claim_summary')} traced requests |",
        f"| tokens per request, agent | {agent_tok:,.0f} | mean of {sum(1 for r in drill if r['feature'] == 'claims_agent')} traced requests |",
        f"| **peak tokens/min, fixed workflow** | **{tpm_fixed:,.0f}** | {peak_per_min:.1f}/min x {fixed_tok:,.0f} |",
        f"| **peak tokens/min, agent** | **{tpm_agent:,.0f}** | {peak_per_min:.1f}/min x {agent_tok:,.0f} |",
        f"| monthly cost, fixed workflow | ${month_fixed:,.0f} | {volume:,} x ${fixed_cost:.4f} (projected) |",
        f"| monthly cost, agent | ${month_agent:,.0f} | {volume:,} x ${agent_cost:.4f} (projected) |",
        f"| local pipeline throughput | {throughput:,.0f} requests/s | {len(notes)} requests, tracing on, stub model |",
        f"| trace log growth | {trace_bytes / 1024:.1f} KB/request -> {volume * trace_bytes / 1e6:,.0f} MB/month | mean serialized trace |",
        "",
        "## Why cost does not break first",
        "",
        f"${month_agent:,.0f}/month at the agent's 3x price is the worst case measured. Cost scales linearly with volume, so 10x volume is 10x the bill, "
        "not a cliff -- and it is a number a budget owner can approve.",
        "",
        "## Why latency does not break first",
        "",
        "Requests are independent: one claim's latency is its own sequential model calls (1 for the fixed workflow, 3 for the agent) and does not depend on "
        f"how many other claims are in flight. At {peak_per_min:.1f} claims/min, even a 30-second end-to-end claim keeps only ~{peak_per_min * 30 / 60:.1f} in flight. "
        f"The local code is not the limit either: {throughput:,.0f} requests/s against a need of {peak_per_min / 60:.2f}/s. (A live model's per-call latency is the unmeasured term; "
        "it multiplies per-request latency, but not with volume.)",
        "",
        "## Why the rate limit is the one to check",
        "",
        "It is the only constraint with a hard external ceiling that paying more does not move on its own, and it binds on *concurrent token throughput*, which is exactly "
        f"what 10x multiplies: ~{tpm_fixed / 1000:.0f}k tokens/min (fixed) or ~{tpm_agent / 1000:.0f}k (agent) at the busiest hour. Hailstorm-style bursts "
        "(Variant D's 18,000 FNOLs in a week) are a separate, larger case handled in the Week 12 plan.",
        "",
        "**Not verified:** the provider's actual tokens-per-minute and requests-per-minute limits for this account; real model latency; the 3x peak factor and the "
        "volume itself (both assumed above). Treat the conclusion as 'cost and latency are not the problem at 10x; confirm the rate limit covers the number in the table.'",
        "",
    ]
    (OUT / "tenx.md").write_text("\n".join(tenx), encoding="utf-8")
    print("\n".join(cost_md))
    print("-" * 80)
    print("\n".join(tenx[:1]))


if __name__ == "__main__":
    main()
