"""Week 11: the one cost improvement, as an experiment with its evidence -- passages per claim, 5 -> 4.

    python -m scripts.w11_cost_improvement          # writes coursework/w11/cost_improvement.md

The attribution (``cost_by_stage.md``) says generation is ~100% of the cost, and the retrieved passages are about three
quarters of a claim summary's input tokens (five passages of ~500 tokens each, plus a ~600-token prompt). So the lever is
how many passages a claim needs. Week 4 measured hit-rate@3 = 100% for hybrid retrieval, which says the answer-bearing chunk is
usually near the top -- but "usually" is a number to measure per claim family, not assume.

Each k from 5 down to 2 is measured on three independent axes, because any one of them alone can mislead:

* **cost** -- mean input tokens and projected USD per request over the 33 Week 6/7 notes (read from the traces);
* **retrieval recall** -- is the passage that *governs* each claim family (a substring of the policy wording that decides it) still in
  the top k? This does not depend on any model, so it cannot be flattered by how the simulated model reads;
* **ground-truth decision errors** -- the wrong-decision rates from ``w11_family_eval`` at that k. Under the stub these barely move with k,
  because the stub effectively reads only its single best passage; that is why recall, not this, is the axis to trust for a real model.

``carry_clause_stem`` is on throughout (it is the shipped fix); only the passage count varies.
"""

from __future__ import annotations

import json
import random
import statistics
import tempfile
from collections import defaultdict
from pathlib import Path

from app.logquery import load_records
from scripts.w11_family_eval import COVERED_FAMILIES
from scripts.w11_traffic import EXCLUDED_FAMILIES, build_service
from scripts.w5_scenarios import _build

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w11" / "cost_improvement.md"
NOTES_FILES = [ROOT / "coursework" / "w7" / "claim_scenarios.jsonl", ROOT / "coursework" / "w6" / "eval_cases.jsonl"]
KS = (5, 4, 3, 2)

#: family -> a substring of the passage that GOVERNS that family's outcome (taken from the corpus text itself).
GOVERNING = {
    "exclusion_subsidence": "subsidence, ground heave",
    "exclusion_regulatory_fine": "any fine, penalty",
    "exclusion_livestock": "standing timber",
    "exclusion_stock_floor": "stock stored below ground level",
    "exclusion_construction": "course of construction",
    "cyber_cloud_outage": "failure, malfunction, degradation",
    "cyber_ransomware": "any extortion demand, ransom payment",
    "e17_within_window": "within 60 consecutive days",
    "flood_in_zone_covered": "deductible of GBP 250,000",
    "stock_raised_covered": "raised at least 150 millimetres",
    "flood_outside_zone": "standard property deductible of GBP 50,000",
    "cyber_fire_writeback": "fire or explosion which itself results from a Cyber Incident",
    "construction_under_threshold": "contract value exceeds GBP 1,000,000",
    "e17_defence_carveout": "does not apply where the Insured can demonstrate",
    "bi_past_waiting_period": "waiting period of 72 consecutive hours",
}


def load_notes() -> list[str]:
    notes: list[str] = []
    for path in NOTES_FILES:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                notes.append(row.get("adjuster_notes") or row["notes"])
    return notes


def measure(k: int, notes: list[str]) -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp) / "t.jsonl"
        service = build_service(Path(tmp), log, carry_clause_stem=True, claim_top_k=k)
        for n in notes:
            service.summarize_claim(n, user="cost-improvement", input_type="fnol_note")
        records = list(load_records([log]))

        recall: dict[str, list[bool]] = defaultdict(list)
        wrong_excluded = n_excluded = wrong_covered = n_covered = 0
        service.tracer.sink = None
        for seed in range(8):
            for scenario in _build(random.Random(seed)):
                needle = GOVERNING.get(scenario.key)
                if needle:
                    retrieved = service.retrieve(scenario.notes, k)
                    recall[scenario.key].append(any(needle in " ".join(c.text.split()) for c in retrieved))
                if scenario.key in EXCLUDED_FAMILIES:
                    n_excluded += 1
                    wrong_excluded += service.summarize_claim(scenario.notes).summary.coverage_decision == "covered"
                elif scenario.key in COVERED_FAMILIES:
                    n_covered += 1
                    wrong_covered += service.summarize_claim(scenario.notes).summary.coverage_decision == "denied"
    return {
        "k": k,
        "tokens_in": statistics.fmean(r["cost"]["input_tokens"] for r in records),
        "usd": statistics.fmean(r["cost"]["usd"] for r in records),
        "recall": {family: (sum(v), len(v)) for family, v in recall.items()},
        "wrong_excluded": (wrong_excluded, n_excluded),
        "wrong_covered": (wrong_covered, n_covered),
    }


def main() -> None:
    notes = load_notes()
    results = {k: measure(k, notes) for k in KS}
    base = results[5]
    lines = [
        "# Week 11 -- the one cost improvement: passages per claim, 5 -> 4",
        "",
        "Generated by `python -m scripts.w11_cost_improvement`. Model: the offline stub (tokens estimated, cost projected at `claude-opus-5` list price; see "
        "`cost_by_stage.md` for the caveats). `carry_clause_stem` on throughout.",
        "",
        "**Why this lever.** `cost_by_stage.md`: generation is ~100% of cost; retrieved passages are ~75% of a claim summary's input tokens. "
        "Attribution came first; this is the change it points at.",
        "",
        f"## Cost ({len(notes)} Week 6/7 notes, traced)",
        "",
        "| passages per claim (k) | mean input tokens | vs k=5 | mean cost / request | per 1,000 requests |",
        "|---|---|---|---|---|",
    ]
    for k, r in results.items():
        lines.append(
            f"| {k}{' (before)' if k == 5 else ' **(shipped)**' if k == 4 else ''} | {r['tokens_in']:,.0f} | "
            f"{(r['tokens_in'] / base['tokens_in'] - 1):+.1%} | ${r['usd']:.5f} | ${r['usd'] * 1000:,.2f} |"
        )

    total = {k: [0, 0] for k in KS}
    for r in results.values():
        for hit, n in r["recall"].values():
            total[r["k"]][0] += hit
            total[r["k"]][1] += n
    lines += [
        "",
        "## Does the governing passage survive? (model-independent -- the axis to trust)",
        "",
        "Recall = the passage whose wording decides the claim is still among the top k. 8 seeds of each Week 5 scenario family.",
        "",
        "| family | " + " | ".join(f"@{k}" for k in KS) + " |",
        "|---|" + "---|" * len(KS),
    ]
    for family in GOVERNING:
        cells = []
        for k in KS:
            hit, n = results[k]["recall"][family]
            cells.append(f"{hit}/{n}" + (" **<**" if hit < results[5]["recall"][family][0] else ""))
        lines.append(f"| {family} | " + " | ".join(cells) + " |")
    lines.append("| **all families** | " + " | ".join(f"**{total[k][0] / total[k][1]:.0%}** ({total[k][0]}/{total[k][1]})" for k in KS) + " |")

    lines += [
        "",
        "`<` marks a family that loses recall against k=5.",
        "",
        "## Decision errors against ground truth (stub; weak evidence about real models)",
        "",
        "| k | excluded losses answered `covered` | covered losses wrongly `denied` |",
        "|---|---|---|",
    ]
    for k, r in results.items():
        e, c = r["wrong_excluded"], r["wrong_covered"]
        lines.append(f"| {k} | {e[0]}/{e[1]} ({e[0] / e[1]:.0%}) | {c[0]}/{c[1]} ({c[0] / c[1]:.0%}) |")
    r3, r4 = results[3], results[4]
    reg = "exclusion_regulatory_fine"
    lines += [
        "",
        "These barely move because the simulated model effectively reads only its single best passage -- it is nearly blind to passages 4 and 5 "
        "by construction. That is *why recall above is the evidence to rely on*, and why a real model, which reads every passage it is given, "
        "could be affected in ways this table cannot show.",
        "",
        "## Decision: k = 4",
        "",
        f"- **k=4 shipped** (`CLAIM_TOP_K`, default 4): input tokens {(r4['tokens_in'] / base['tokens_in'] - 1):+.1%}, projected cost "
        f"{(r4['usd'] / base['usd'] - 1):+.1%} per request; recall {total[4][0] / total[4][1]:.0%} vs {total[5][0] / total[5][1]:.0%} at k=5. "
        f"One family gives anything up: `{reg}` {results[4]['recall'][reg][0]}/{results[4]['recall'][reg][1]} vs {results[5]['recall'][reg][0]}/{results[5]['recall'][reg][1]} (one case in {sum(n for _, n in results[5]['recall'].values())}).",
        f"- **k=3 rejected** despite {(r3['tokens_in'] / base['tokens_in'] - 1):+.1%} tokens: `{reg}` falls to "
        f"{r3['recall'][reg][0]}/{r3['recall'][reg][1]} -- the governing passage is missing for "
        f"{1 - r3['recall'][reg][0] / r3['recall'][reg][1]:.0%} of those claims. A saving bought by dropping the evidence is not a saving.",
        "- **Not touched:** `/ask` keeps `TOP_K=5` (this experiment measured claim summaries only).",
        "- **Not measured:** whether a real model's accuracy changes with 4 vs 5 passages. Recall says the right passage is there; it does not say "
        "the model uses a fifth passage for something recall cannot see (a carve-out two passages away). The first live check is the Week 6 suite at "
        "`CLAIM_TOP_K=4` vs `5`.",
        "",
    ]
    OUT.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
