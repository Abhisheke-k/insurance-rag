"""Week 11: turn production traces into permanent eval cases -- the failure -> test loop's first step.

    python -m scripts.w11_make_case --log .drill/traces.jsonl 279c1932 aeb32b61 eabf329a

A case is built **from the log alone**: the redacted input stored on the trace (the only copy of the
adjuster's text a log reader is allowed to have), with the claim-number pseudonym swapped for a synthetic,
well-formed claim number so the shared assertions can run (the same substitution Week 6 made for the regression
cases it took from the Week 5 traces). Building from the log is the point: if a failure cannot be reproduced from
what the log keeps, the log is missing something, and finding that out is cheaper now than at 3am.

Each case asserts its own expectation before any judge runs: ``must_not_decide: ["covered"]``, because the
failure is a coverage verdict on a loss the policy excludes. ``judged: false`` -- these cases are about one
fact, not about the judge's reading of the rationale.

The case is checked to *fail* on the current system before it is written (``--require-red``, the default): a test
that has only ever been green is a test you have not tested.
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path

from app.logquery import load_records
from scripts.w11_traffic import build_service

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "coursework" / "w11" / "regression_cases.jsonl"
_PSEUDONYM_RE = re.compile(r"\bclm_[0-9a-f]{8}\b")


def synthetic_claim_number(trace_id: str) -> str:
    return f"CLM-2026-{int(trace_id[:6], 16) % 90000 + 10000:05d}"


def build_case(record: dict, why: str) -> dict:
    trace_id = record["trace_id"]
    notes = _PSEUDONYM_RE.sub(synthetic_claim_number(trace_id), record["input"]["redacted"])
    return {
        "id": f"prod_{trace_id[:8]}",
        "notes": notes,
        "mode": "exclusion_missed",
        "source": f"production_failure:{trace_id}",
        "must_not_decide": ["covered"],
        "judged": False,
        "why": why,
        "observed": {
            "decision": record["output"]["decision"],
            "summary": record["output"]["summary"][:200],
            "prompt_versions": record["prompt_versions"],
            "context_ids": record["context_ids"],
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("trace_ids", nargs="+", help="trace id prefixes")
    parser.add_argument("--log", type=Path, default=ROOT / ".drill" / "traces.jsonl")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--why", default="Production failure: answered 'covered' although the loss falls inside an exclusion in the policy wording.")
    parser.add_argument("--no-require-red", action="store_true")
    arguments = parser.parse_args()

    records = [r for r in load_records([arguments.log]) if r.get("schema") == "trace/1"]
    existing = {json.loads(l)["id"] for l in arguments.out.read_text(encoding="utf-8").splitlines() if l.strip()} if arguments.out.exists() else set()
    cases = []
    for prefix in arguments.trace_ids:
        matches = [r for r in records if r["trace_id"].startswith(prefix)]
        if len(matches) != 1:
            raise SystemExit(f"{prefix!r} matches {len(matches)} traces")
        case = build_case(matches[0], arguments.why)
        if case["id"] not in existing:
            cases.append(case)

    if not arguments.no_require_red:
        with tempfile.TemporaryDirectory() as tmp:
            service = build_service(Path(tmp), None)
            for case in cases:
                decision = service.summarize_claim(case["notes"]).summary.coverage_decision
                if decision in case["must_not_decide"]:
                    print(f"RED   {case['id']}: decision={decision!r}  (reproduces from the redacted log text)")
                else:
                    raise SystemExit(f"{case['id']} does not reproduce the failure (decision={decision!r}); not a usable case")

    arguments.out.parent.mkdir(parents=True, exist_ok=True)
    with arguments.out.open("a", encoding="utf-8") as handle:
        for case in cases:
            handle.write(json.dumps(case, ensure_ascii=False) + "\n")
    print(f"wrote {len(cases)} case(s) to {arguments.out}")


if __name__ == "__main__":
    main()
