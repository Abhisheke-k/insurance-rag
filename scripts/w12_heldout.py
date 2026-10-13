"""Week 12: ten held-out notices for the judge -- written after the judge was iterated, labelled before it is run on them.

The 28 labelled outputs were used to *tune* the integrated judge's prompt (v1 -> v2), so agreement on them is not evidence the judge
generalises. These ten were written afterwards, run through the shipped system, and are labelled by hand from the outputs before
``python -m scripts.w12_judge --heldout`` is ever invoked. Several are deliberately phrased to dodge the rule the iteration added
("third flood in 90 days", "last flooded on 1 July") because that is where I expect a pattern-reading judge to fail.

    python -m scripts.w12_heldout         # run them through the system, write coursework/w12/heldout_outputs.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from scripts.w12_run_suite import SHIPPED, run_config

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w12"

HELDOUT: list[tuple[str, str]] = [
    ("H01", "CLM-2024-00418. Date of loss: 3 September 2024. Water came up through the floor drains after a flood, the second this year, about 45 days after the earlier flood. The yard is in a flood zone."),
    ("H02", "CLM-2024-00502. Date of loss: 12 July 2024. The river burst its banks and flooded the loading bay. The site is not in a flood zone."),
    ("H03", "CLM-2024-00417. Date of loss: 2 November 2024. The stockroom flooded again after the October storms. The yard is in a flood zone."),
    ("H04", "CLM-2025-00290. Date of loss: 15 October 2024. Business interruption: production stopped for 80 hours after a power failure. Does the waiting period apply?"),
    ("H05", "CLM-2024-00418. Date of loss: 5 December 2024. A fire in the packing hall destroyed the conveyor."),
    ("H06", "CLM-2024-00417. Date of loss: 9 December 2024. Ransomware encrypted the order system and the attackers demanded a payment."),
    ("H07", "CLM-2024-00417. Date of loss: 20 August 2024. Flood at the depot; the site last flooded on 1 July 2024. The yard is in a flood zone."),
    ("H08", "CLM-2025-00031. Date of loss: 14 February 2025. Flood damage to the cold store; it also flooded in January. The site is not in a flood zone."),
    ("H09", "CLM-2024-00502. Date of loss: 18 August 2024. Cracks appeared in the north wall after ground heave beneath the warehouse."),
    ("H10", "CLM-2024-00611. Date of loss: 13 September 2024. A burst water main flooded the basement stock room; this is the third flood in 90 days. The yard is in a flood zone."),
]


def main() -> None:
    logging.disable(logging.CRITICAL)
    cases: list[dict[str, Any]] = [{"id": cid, "fnol": text, "flags": {}, "truth": {}, "ideal_tools": []} for cid, text in HELDOUT]
    runs, _ = run_config(SHIPPED, cases, ROOT / "logs" / "w12_heldout")
    rows = []
    for run in runs:
        card = run.card
        if card is None or not card["wording"]:
            rows.append({"id": run.case["id"], "notice": run.case["fnol"], "cited": [], "signals": [], "reason_codes": [], "note": run.error or "nothing cited"})
            continue
        rows.append(
            {
                "id": run.case["id"],
                "notice": card["request"]["fnol_redacted"],
                "cited": [{"version": w["version"], "section": w["section"], "excerpt": w["excerpt"]} for w in card["wording"]],
                "signals": card["signals"],
                "reason_codes": card["referral"]["reason_codes"],
            }
        )
    (OUT / "heldout_outputs.json").write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in rows:
        print("###", row["id"], "|", " ".join(row["notice"].split())[:150])
        for cited in row["cited"]:
            print("   CITED:", cited["version"][:60], "|", cited["section"][:50], "\n         ", " ".join(cited["excerpt"].split())[:200])
        print("   codes:", row["reason_codes"])


if __name__ == "__main__":
    main()
