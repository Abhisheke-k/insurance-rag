"""Week 6: the one command. Runs generation + assertions + judge over the full
eval set (25+ cases, mode-tagged, including the 2 regression cases) and prints
pass rate by mode.

    python -m scripts.w6_run_eval
    python -m scripts.w6_run_eval --judge coursework/w6/judge_v2.txt

Week 11 extends it without changing what it did before:

* **More case files.** The suite is every file in ``SUITE_FILES`` -- the original 28 Week 6 cases plus
  ``coursework/w11/regression_cases.jsonl``, where each real production failure becomes a permanent case. A
  failure -> test loop only works if the test lives in the suite that runs on every change.
* **A case can assert its own expected outcome**, checked *before* the judge ("assertions before judges"):
  ``must_not_decide: ["covered"]`` fails a case whose decision is on the list; ``expected_decision`` fails any other
  decision. ``judged: false`` marks a case whose pass/fail is its assertions alone (a regression case is
  about one fact, not about the judge's read of the rationale).
* **Reproducible comparison.** ``--embedding hashing`` runs in seconds with no model download (the original
  sentence-transformers setup is still the default, and takes ~2 minutes to load on a cold start);
  ``--setting key=value`` overrides a config knob for a before/after run; ``--json-out`` writes every case's
  result so two runs can be diffed case by case, not just by total.

    python -m scripts.w6_run_eval --embedding hashing --label before --json-out before.json
    python -m scripts.w6_run_eval --embedding hashing --label after  --json-out after.json
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from app.assertions import run_assertions
from app.config import Settings
from app.judge import ClaimSummaryJudge
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf
from scripts.w6_eval_set import EVAL_SET_PATH

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
REGRESSION_CASES = ROOT / "coursework" / "w11" / "regression_cases.jsonl"
SUITE_FILES = [EVAL_SET_PATH, REGRESSION_CASES]


def load_cases(paths: list[Path]) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    for path in paths:
        if path.exists():
            cases += [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return cases


def case_assertions(case: dict[str, Any], summary: dict[str, Any]) -> list[tuple[str, bool, str]]:
    """The deterministic checks for one case: the four shared ones, then the case's own expectations."""
    results = [(a.name, a.passed, a.detail) for a in run_assertions(summary)]
    decision = summary["coverage_decision"]
    forbidden = case.get("must_not_decide") or []
    if forbidden:
        results.append(("must_not_decide", decision not in forbidden, f"coverage_decision={decision!r}, forbidden={forbidden}"))
    if case.get("expected_decision"):
        results.append(("expected_decision", decision == case["expected_decision"], f"coverage_decision={decision!r}, expected={case['expected_decision']!r}"))
    return results


def _coerce(value: str) -> Any:
    lowered = value.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        try:
            return float(value)
        except ValueError:
            return value


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", type=Path, default=ROOT / "coursework" / "w6" / "judge_v2.txt")
    parser.add_argument("--embedding", default="sentence-transformers", choices=["sentence-transformers", "hashing"])
    parser.add_argument("--retrieval-mode", default="hybrid_bm25_rrf", choices=["dense", "hybrid_bm25_rrf"])
    parser.add_argument("--cases", action="append", type=Path, help="case file(s); default: the whole suite (SUITE_FILES)")
    parser.add_argument("--setting", action="append", default=[], metavar="KEY=VALUE", help="override a Settings field, e.g. carry_clause_stem=false")
    parser.add_argument("--label", default="run")
    parser.add_argument("--json-out", type=Path)
    args = parser.parse_args()

    main_pdf = DATA_DIR / "sample_endorsement_pack.pdf"
    lib_pdf = DATA_DIR / "form_ho0304_archive.pdf"
    if not main_pdf.exists():
        build_sample_pdf(main_pdf)
    if not lib_pdf.exists():
        build_form_library_pdf(lib_pdf)

    overrides = dict(item.split("=", 1) for item in args.setting)
    settings = Settings(
        embedding_provider=args.embedding,
        force_in_memory_store=True,
        chroma_dir=ROOT / ".eval_scratch" / "w6_run_eval",
        llm_provider="stub",
        retrieval_mode=args.retrieval_mode,
        trace_log_path=None,
        **{key: _coerce(value) for key, value in overrides.items()},
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(lib_pdf.read_bytes(), "form_ho0304_archive.pdf")
    judge = ClaimSummaryJudge(settings, args.judge.read_text(encoding="utf-8"))

    cases = load_cases(args.cases or SUITE_FILES)

    by_mode: dict[str, list[bool]] = defaultdict(list)
    rows = []
    for case in cases:
        outcome = service.summarize_claim(case["notes"])
        summary_dict = {
            "claim_number": outcome.summary.claim_number,
            "date_of_loss": outcome.summary.date_of_loss,
            "coverage_decision": outcome.summary.coverage_decision,
            "cited_exclusion_id": outcome.summary.cited_exclusion_id,
            "excess_amount": outcome.summary.excess_amount,
            "summary": outcome.summary.summary,
            "citations": [c.chunk_id for c in outcome.summary.citations],
        }
        checks = case_assertions(case, summary_dict)
        assertions_pass = all(passed for _name, passed, _detail in checks)
        judged = case.get("judged", True)
        judge_pass = judge.judge(case["notes"], summary_dict, outcome.retrieved).pass_criterion if judged else None
        case_pass = assertions_pass and (judge_pass is not False)
        by_mode[case["mode"]].append(case_pass)
        rows.append(
            {
                "id": case["id"],
                "mode": case["mode"],
                "source": case["source"],
                "decision": summary_dict["coverage_decision"],
                "assertions_pass": assertions_pass,
                "failed_assertions": [name for name, passed, _d in checks if not passed],
                "judge_pass": judge_pass,
                "pass": case_pass,
            }
        )

    total_pass = sum(r["pass"] for r in rows)
    print(f"label: {args.label}    embedding={args.embedding} retrieval={args.retrieval_mode} overrides={overrides or '-'}")
    print(f"judge prompt: {args.judge}")
    print(f"OVERALL: {total_pass}/{len(rows)} = {total_pass / len(rows):.0%}\n")
    print(f"{'mode':22} {'pass':6} {'total':6} {'rate'}")
    for mode, results in sorted(by_mode.items()):
        p = sum(results)
        print(f"{mode:22} {p:<6} {len(results):<6} {p / len(results):.0%}")

    print(f"\n{'id':38} {'pass':6} {'assertions':12} {'judge':6}")
    for r in rows:
        assertion_note = "ok" if r["assertions_pass"] else ",".join(r["failed_assertions"])
        print(f"{r['id']:38} {str(r['pass']):6} {assertion_note:12} {str(r['judge_pass']):6}")

    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(
            json.dumps({"label": args.label, "overall": [total_pass, len(rows)], "overrides": overrides, "rows": rows}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwritten to {args.json_out}")


if __name__ == "__main__":
    main()
