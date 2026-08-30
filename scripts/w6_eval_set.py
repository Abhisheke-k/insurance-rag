"""Week 6 eval set: 25+ cases tagged by a Week 5 taxonomy mode, plus 2 regression cases.

Cases are drawn from the same scenario generator Week 5's traffic came from
(`scripts/w5_scenarios.py`) -- reusing the corpus rather than hand-writing a
second one -- tagged with the mode each scenario type is expected to exercise
based on what Week 5 actually observed. Two regression cases are copied
verbatim (redacted notes text) from real entries in `coursework/w5/traces.log`
that Week 5 found broken.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.tracing import read_traces
from scripts.w5_scenarios import build_scenarios

ROOT = Path(__file__).resolve().parent.parent
EVAL_SET_PATH = ROOT / "coursework" / "w6" / "eval_cases.jsonl"
TRACE_LOG = ROOT / "coursework" / "w5" / "traces.log"

#: scenario key -> Week 5 taxonomy mode it is expected to exercise (see coursework/w5/taxonomy.md)
MODE_BY_SCENARIO_KEY = {
    "flood_in_zone_covered": "verdict_inversion",
    "exclusion_stock_floor": "verdict_inversion",
    "e17_within_window": "verdict_inversion",
    "e17_defence_carveout": "verdict_inversion",
    "cyber_ransomware": "verdict_inversion",
    "cyber_cloud_outage": "verdict_inversion",
    "cyber_fire_writeback": "verdict_inversion",
    "bi_exceeds_indemnity_period": "verdict_inversion",
    "bi_within_waiting_period": "verdict_inversion",
    "sublimit_erosion": "off_topic_citation",
    "bi_past_waiting_period": "off_topic_citation",
    "bi_late_notification": "off_topic_citation",
    "exclusion_subsidence": "mislabel",
    "exclusion_livestock": "mislabel",
    "exclusion_construction": "mislabel",
    "construction_under_threshold": "mislabel",
    "flood_outside_zone": "deductible_misapplied",
    "stock_raised_covered": "control",
    "exclusion_regulatory_fine": "control",
    "flood_defence_untested": "control",
    "out_of_corpus": "control",
}

#: keys given 2 examples instead of 1 -- the modes Week 5 found most frequent/severe
PRIORITY_KEYS = {
    "flood_in_zone_covered", "exclusion_stock_floor", "e17_within_window",
    "cyber_ransomware", "sublimit_erosion",
}


@dataclass(frozen=True, slots=True)
class EvalCase:
    id: str
    notes: str
    mode: str
    source: str  # "generated" | "regression"


def build_eval_cases() -> list[EvalCase]:
    scenarios = build_scenarios()
    by_key: dict[str, list[str]] = {}
    for s in scenarios:
        by_key.setdefault(s.key, []).append(s.notes)

    cases: list[EvalCase] = []
    for key, notes_list in by_key.items():
        mode = MODE_BY_SCENARIO_KEY.get(key, "control")
        take = 2 if key in PRIORITY_KEYS else 1
        for i, notes in enumerate(notes_list[:take]):
            cases.append(EvalCase(id=f"gen_{key}_{i}", notes=notes, mode=mode, source="generated"))

    # -- 2 regression cases, copied verbatim from real failed traces (Week 5) --
    # The trace log stores REDACTED notes (claimant names / claim numbers masked
    # before write -- see app/tracing.py); regression fixtures substitute a fresh
    # synthetic, well-formed claim number for the redaction placeholder so the
    # case is actually runnable end to end, and are otherwise verbatim.
    traces = {t.trace_id: t for t in read_traces(TRACE_LOG)}
    regressions = [
        ("trc_00034", "verdict_inversion", "CLM-2026-90001"),  # stock-on-floor: quotes (d) exclusion, says "covered"
        ("trc_00011", "off_topic_citation", "CLM-2026-90002"),  # sub-limit exhaustion denied on the cyber exclusion
    ]
    for trace_id, mode, synthetic_claim_number in regressions:
        record = traces[trace_id]
        notes = record.input_redacted.replace("[CLAIM NUMBER REDACTED]", synthetic_claim_number)
        cases.append(EvalCase(id=f"regression_{trace_id}", notes=notes, mode=mode, source="regression"))

    return cases


def main() -> None:
    cases = build_eval_cases()
    EVAL_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with EVAL_SET_PATH.open("w", encoding="utf-8") as fh:
        for case in cases:
            fh.write(json.dumps({"id": case.id, "notes": case.notes, "mode": case.mode, "source": case.source}) + "\n")

    from collections import Counter

    print(f"wrote {len(cases)} cases to {EVAL_SET_PATH}")
    print("by mode:", dict(Counter(c.mode for c in cases)))
    print("regression cases:", [c.id for c in cases if c.source == "regression"])


if __name__ == "__main__":
    main()
