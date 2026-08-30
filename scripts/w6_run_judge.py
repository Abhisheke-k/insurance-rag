"""Week 6: run the judge over the frozen 25, compute agreement against labels_25.json.

    python -m scripts.w6_run_judge --prompt coursework/w6/judge_v1.txt
    python -m scripts.w6_run_judge --prompt coursework/w6/judge_v2.txt
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.config import Settings
from app.judge import ClaimSummaryJudge
from app.models import RetrievedChunk

ROOT = Path(__file__).resolve().parent.parent
FROZEN_PATH = ROOT / "coursework" / "w6" / "frozen_summaries_25.json"
LABELS_PATH = ROOT / "coursework" / "w6" / "labels_25.json"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--prompt", type=Path, default=ROOT / "coursework" / "w6" / "judge_v1.txt")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    frozen = json.loads(FROZEN_PATH.read_text(encoding="utf-8"))
    labels = {row["id"]: row for row in json.loads(LABELS_PATH.read_text(encoding="utf-8"))["labels"]}
    system_prompt = args.prompt.read_text(encoding="utf-8")

    judge = ClaimSummaryJudge(Settings(llm_provider="stub"), system_prompt)

    results = []
    agree = 0
    for case in frozen:
        retrieved = [
            RetrievedChunk(
                chunk_id=c["chunk_id"], doc_id=c["doc_id"], filename=c["filename"], text=c["text"],
                page=c["page"], page_end=c["page_end"], section_path=c["section_path"],
                clause_label=c["clause_label"], score=c["score"],
            )
            for c in case["retrieved"]
        ]
        verdict = judge.judge(case["notes"], case["summary"], retrieved)
        human = labels[case["id"]]["pass_criterion"]
        matches = verdict.pass_criterion == human
        agree += matches
        results.append(
            {
                "id": case["id"],
                "mode": case["mode"],
                "human": human,
                "judge": verdict.pass_criterion,
                "agree": matches,
                "judge_rationale": verdict.rationale,
                "human_reason": labels[case["id"]]["reason"],
            }
        )

    agreement = agree / len(frozen)
    print(f"prompt: {args.prompt}")
    print(f"agreement: {agree}/{len(frozen)} = {agreement:.0%}")
    print()
    for r in results:
        marker = "  " if r["agree"] else "**"
        print(f"{marker} {r['id']:32} human={str(r['human']):5} judge={str(r['judge']):5} mode={r['mode']}")

    if args.out:
        args.out.write_text(json.dumps({"prompt": str(args.prompt), "agreement": agreement, "results": results}, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
