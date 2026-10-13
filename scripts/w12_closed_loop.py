"""Week 12: the closed failure -> test loop, the taxonomy's "before" numbers, the bonus, and cost / 10x -- all measured.

    python -m scripts.w12_closed_loop

Each defence in the system exists because a case in the suite went red without it. This script proves that by switching off exactly one
defence at a time on the same 38 cases (a real stdio MCP server each time) and reading the mode detectors (``scripts/w12_run_suite.py``):

    configuration          what is switched off
    shipped                nothing
    no_version_filter      server-side VERSION_FILTER=false           (the loss-date retrieval filter)
    no_date_check          TriageService(enforce_loss_date=False)     (host checks the model's loss date against the notice)
    no_pii_defence         redact_input=False, redact_arguments=False (personal details removed before the model / the wire)
    no_compliance_guard    enforce_compliance=False                   (output scan; the server-side refusal in refer_to_adjuster stays)
    no_in_force_backstop   backstop_in_force=False                    (host runs the in-force check if the agent skipped it)
    all_off                every one of the above

Then: the bonus (what the date filter costs in hit-rate@3 on questions that do not straddle a boundary) and cost per claim / the
hailstorm-week arithmetic.
"""

from __future__ import annotations

import json
import logging
import math
import time
from pathlib import Path
from typing import Any

from app.config import Settings, get_settings
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf
from scripts.w12_cases import CASES
from scripts.w12_run_suite import SHIPPED, Config, evaluate, mode_table, run_config, summarise, _files_text, _read_jsonl

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w12"

CONFIGS = [
    SHIPPED,
    Config("no_version_filter", version_filter=False),
    Config("no_date_check", enforce_loss_date=False),
    Config("no_pii_defence", redact_input=False, redact_arguments=False),
    Config("no_compliance_guard", enforce_compliance=False),
    Config("no_in_force_backstop", backstop_in_force=False),
    Config("all_off", version_filter=False, redact_input=False, redact_arguments=False, enforce_loss_date=False, backstop_in_force=False, enforce_compliance=False),
]

#: which mode each ablation is the "before" for
BEFORE_FOR = {
    "no_version_filter": "wrong_edition_cited",
    "no_date_check": "loss_date_misread",
    "no_pii_defence": "pii_in_tool_args",
    "no_compliance_guard": "verdict_language",
    "no_in_force_backstop": "in_force_check_skipped",
}


def ablations(workdir: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for config in CONFIGS:
        runs, paths = run_config(config, CASES, workdir)
        summary = summarise(runs, paths)
        edition_cases = [r for r in summary["per_case"] if "edition_right" in r["assertions"]]
        result[config.name] = {
            "passed_all": f"{summary['passed_all']}/{summary['n_cases']}",
            "modes": {m["mode"]: m for m in summary["modes"]},
            "edition_right": f"{sum(r['assertions']['edition_right']['ok'] for r in edition_cases)}/{len(edition_cases)}",
            "failing_cases": [f["id"] for f in summary["failures"]],
            "per_case_edition": {r["id"]: r["assertions"]["edition_right"]["ok"] for r in edition_cases},
            "trace_ids": {r["id"]: r["trace_id"] for r in summary["per_case"]},
        }
    return result


def the_five_around_the_2025_endorsement(table: dict[str, Any]) -> dict[str, Any]:
    """The bonus: cases whose loss date is on either side of 1 Feb 2025, red (filter off) vs green (shipped)."""
    ids = ["S05_jan2025_day_before_03-24", "S06_feb2025_03-24_starts", "L05_flood_after_the_endorsement_took_effect", "X06_asked_to_tell_the_claimant_it_is_covered", "I02_last_day_of_period"]
    rows = []
    for case_id in ids:
        before = table["no_version_filter"]["per_case_edition"][case_id]
        after = table["shipped"]["per_case_edition"][case_id]
        rows.append({"id": case_id, "green_before": before, "green_after": after, "trace_before": table["no_version_filter"]["trace_ids"][case_id]})
    return {"cases": rows, "red_before": sum(not r["green_before"] for r in rows), "green_after": sum(r["green_after"] for r in rows), "n": len(rows)}


# --------------------------------------------------------------------------- #
# Bonus: what the date filter costs in hit-rate@3 where nothing straddles a boundary
# --------------------------------------------------------------------------- #
#: (question, loss date, text that must be in the top-3). Every date is >= 45 days from any edition or endorsement boundary and every
#: expected text is in force that day, so a miss is retrieval, not "the wording had not taken effect yet".
HIT_QUESTIONS: list[tuple[str, str, str]] = [
    ("What is the flood sub-limit in the annual aggregate?", "2024-08-11", "GBP 5,000,000"),
    ("What deductible applies to flood losses inside a flood zone?", "2024-08-11", "GBP 250,000"),
    ("What is the policy limit any one occurrence?", "2024-08-11", "GBP 40,000,000"),
    ("second flood within consecutive days exclusion E-17", "2024-08-11", "within 55 consecutive days"),
    ("second flood within consecutive days exclusion E-17", "2025-06-15", "within 60 consecutive days"),
    ("second flood within consecutive days exclusion E-17", "2023-10-15", "within 45 consecutive days"),
    ("second flood within consecutive days exclusion E-17", "2022-09-01", "within 35 consecutive days"),
    ("ransom payment extortion demand cyber exclusion", "2024-08-11", "extortion demand"),
    ("What is the waiting period for business interruption?", "2024-12-01", "72 consecutive hours"),
    ("What is the maximum indemnity period?", "2024-12-01", "18 months"),
    ("How soon must the insured give written notice of a business interruption claim?", "2024-12-01", "within 30 days"),
    ("How much notice is needed to cancel the policy?", "2024-08-11", "60 days written notice"),
    ("What counts as a flood?", "2024-08-11", "escape of water from the normal confines"),
    ("Is subsidence excluded?", "2024-08-11", "subsidence, ground heave or landslip"),
    ("Do payments erode the sub-limit?", "2024-08-11", "erodes the annual aggregate sub-limit"),
    ("What is a flood zone?", "2024-08-11", "Zone 2 or Zone 3"),
]


def hit_rate(pool_factor: int, filtered: bool, workdir: Path) -> tuple[int, list[str]]:
    from datetime import date  # noqa: PLC0415

    pack, archive = build_sample_pdf(workdir / "pack.pdf"), build_form_library_pdf(workdir / "archive.pdf")
    base = get_settings().model_copy(update={"force_in_memory_store": True, "embedding_provider": "hashing", "chroma_dir": workdir / f"hr-{pool_factor}-{filtered}",
                                              "trace_log_path": None, "version_pool_factor": pool_factor, "version_filter": filtered})
    rag = RagService(base, registry=DocumentRegistry(base.registry_path))
    rag.ingest(pack.read_bytes(), "sample_endorsement_pack.pdf")
    rag.ingest(archive.read_bytes(), "form_ho0304_archive.pdf")
    hits, misses = 0, []
    for question, when, expected in HIT_QUESTIONS:
        top = rag.retrieve_as_of(question, 3, date.fromisoformat(when)) if filtered else rag.retrieve(question, 3)
        if any(expected in " ".join(h.text.split()) for h in top):
            hits += 1
        else:
            misses.append(f"{question[:40]} @ {when}")
    return hits, misses


def bonus(workdir: Path) -> dict[str, Any]:
    out = {"n": len(HIT_QUESTIONS)}
    for label, (factor, filtered) in {"unfiltered_retrieve": (3, False), "filtered_pool_x3": (3, True), "filtered_pool_x1": (1, True)}.items():
        hits, misses = hit_rate(factor, filtered, workdir)
        out[label] = {"hit_at_3": f"{hits}/{len(HIT_QUESTIONS)}", "pct": round(100 * hits / len(HIT_QUESTIONS), 1), "misses": misses}
    return out


# --------------------------------------------------------------------------- #
# Cost per claim, and a hailstorm week
# --------------------------------------------------------------------------- #
def system_prompt_share(workdir: Path) -> dict[str, Any]:
    """What fraction of a claim's input tokens is the same system prompt re-sent each turn (the part prompt caching would discount)."""
    card = json.loads((workdir / "shipped" / "cards.jsonl").read_text(encoding="utf-8").splitlines()[6])
    cost = card["cost"]
    from app.tokenizer import estimate_tokens  # noqa: PLC0415
    from app.triage import TRIAGE_ROLE  # noqa: PLC0415
    from app.mcp_agent import build_system_prompt  # noqa: PLC0415

    return {"example_trace": card["trace_id"], "input_tokens": cost["input_tokens"], "llm_calls": cost["llm_calls"], "role_brief_tokens": estimate_tokens(TRIAGE_ROLE.brief)}


def cost_and_hailstorm(summary: dict[str, Any]) -> dict[str, Any]:
    per_claim = summary["cost"]
    tokens_in = per_claim["input_tokens_mean"]
    out_tokens = 1400  # mean output tokens per claim in the shipped run (see cards.jsonl cost.output_tokens)
    week_claims = 18_000
    peak_share_of_week = 0.60  # assumption: 60% of the week's FNOLs arrive in the three worst days
    peak_hours = 72
    busiest_hour_factor = 3.0  # assumption: same 3x as Week 11
    average_peak_hour = week_claims * peak_share_of_week / peak_hours
    busiest_hour = average_peak_hour * busiest_hour_factor
    per_min = busiest_hour / 60
    return {
        "usd_per_claim": per_claim["usd_per_claim_mean"],
        "llm_calls_per_claim": per_claim["llm_calls_mean"],
        "input_tokens_per_claim": tokens_in,
        "latency_ms_p50_p95_excluding_model": [per_claim["latency_ms_p50"], per_claim["latency_ms_p95"]],
        "hailstorm_week": {
            "claims": week_claims,
            "usd": round(week_claims * per_claim["usd_per_claim_mean"]),
            "busiest_hour_claims": round(busiest_hour),
            "busiest_minute_claims": round(per_min, 1),
            "peak_tokens_per_min": round(per_min * (tokens_in + out_tokens)),
            "peak_model_requests_per_min": round(per_min * per_claim["llm_calls_mean"]),
            "vs_week11_check_number_43k_tokens_per_min": round(per_min * (tokens_in + out_tokens) / 43_333, 1),
            "non_model_capacity_claims_per_second": round(1000 / max(per_claim["latency_ms_p50"], 1), 1),
            "referrals_created": week_claims,
        },
        "assumptions": ["60% of the week's FNOLs in the 3 worst days", "busiest hour = 3x the average peak hour (as Week 11)", "output ~1,400 tokens/claim", "price = claude-opus-5 list ($5/$25 per MTok), tokens estimated"],
    }


def main() -> None:
    logging.disable(logging.CRITICAL)
    workdir = ROOT / "logs" / "w12_loop"
    started = time.perf_counter()
    table = ablations(workdir)
    summary_shipped = json.loads((OUT / "suite_results.json").read_text(encoding="utf-8"))
    result = {
        "ablations": {k: {kk: vv for kk, vv in v.items() if kk not in ("per_case_edition", "trace_ids")} for k, v in table.items()},
        "the_five_around_the_2025_endorsement": the_five_around_the_2025_endorsement(table),
        "wrong_edition_by_config": {name: table[name]["modes"]["wrong_edition_cited"] for name in ("shipped", "no_version_filter", "all_off")},
        "bonus_hit_rate": bonus(workdir),
        "cost": cost_and_hailstorm(summary_shipped),
        "seconds": round(time.perf_counter() - started, 1),
    }
    (OUT / "closed_loop.json").write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")

    print("== one defence off at a time (same 38 cases) ==")
    for name, row in result["ablations"].items():
        print(f"{name:22} cases passing every assertion: {row['passed_all']:6} edition_right: {row['edition_right']}")
    print("\n== the mode each defence is the 'before' for ==")
    for config_name, mode in BEFORE_FOR.items():
        before, after = table[config_name]["modes"][mode], table["shipped"]["modes"][mode]
        print(f"{mode:26} n={before['n']:2}  without defence: {before['count']:2} ({before['pct']}%)   shipped: {after['count']} ({after['pct']}%)   example trace (defence off) {before['example_trace_id']}")
    print("\n== the five around the 2025 endorsement ==")
    print(json.dumps(result["the_five_around_the_2025_endorsement"], indent=1))
    print("\n== bonus: hit-rate@3 on 16 non-straddling questions ==")
    print(json.dumps(result["bonus_hit_rate"], indent=1))
    print("\n== cost ==")
    print(json.dumps(result["cost"], indent=1))


if __name__ == "__main__":
    main()
