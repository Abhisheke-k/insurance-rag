"""Week 6: the one command. Runs generation + assertions + judge over the full
eval set (25+ cases, mode-tagged, including the 2 regression cases) and prints
pass rate by mode.

    python -m scripts.w6_run_eval
    python -m scripts.w6_run_eval --judge coursework/w6/judge_v2.txt
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.assertions import run_assertions
from app.config import Settings
from app.judge import ClaimSummaryJudge
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf
from scripts.w6_eval_set import EVAL_SET_PATH

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--judge", type=Path, default=ROOT / "coursework" / "w6" / "judge_v2.txt")
    args = parser.parse_args()

    main_pdf = DATA_DIR / "sample_endorsement_pack.pdf"
    lib_pdf = DATA_DIR / "form_ho0304_archive.pdf"
    if not main_pdf.exists():
        build_sample_pdf(main_pdf)
    if not lib_pdf.exists():
        build_form_library_pdf(lib_pdf)

    settings = Settings(
        embedding_provider="sentence-transformers",
        force_in_memory_store=True,
        chroma_dir=ROOT / ".eval_scratch" / "w6_run_eval",
        llm_provider="stub",
        retrieval_mode="hybrid_bm25_rrf",
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(lib_pdf.read_bytes(), "form_ho0304_archive.pdf")
    judge = ClaimSummaryJudge(settings, args.judge.read_text(encoding="utf-8"))

    cases = [json.loads(line) for line in EVAL_SET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]

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
        assertion_results = run_assertions(summary_dict)
        assertions_pass = all(a.passed for a in assertion_results)
        verdict = judge.judge(case["notes"], summary_dict, outcome.retrieved)
        case_pass = assertions_pass and verdict.pass_criterion
        by_mode[case["mode"]].append(case_pass)
        rows.append(
            {
                "id": case["id"],
                "mode": case["mode"],
                "source": case["source"],
                "assertions_pass": assertions_pass,
                "failed_assertions": [a.name for a in assertion_results if not a.passed],
                "judge_pass": verdict.pass_criterion,
                "pass": case_pass,
            }
        )

    total_pass = sum(r["pass"] for r in rows)
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


if __name__ == "__main__":
    main()
