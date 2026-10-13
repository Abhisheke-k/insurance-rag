"""Week 12: the judge, re-checked on the integrated system's outputs -- agreement against hand labels, as a number.

Runs only on the outputs a human already labelled (``coursework/w12/labels.json``, hashed in ``labels_provenance.txt`` before this
file existed), so the comparison cannot have been shaped by the verdicts. Two judges are measured:

* **integrated judge** (``coursework/w12/judge_integrated.txt``): the Week 6 judge's *method* -- one binary criterion, loaded from a
  prompt file, deterministic assertions first -- re-targeted at what the triage system produces. The Week 6 criterion ("does the
  cited passage support the stated coverage decision?") has nothing to grade here: the system states no decision, by design.
* **Week 6 judge v2, unchanged** (``coursework/w6/judge_v2.txt``), shown for comparison with its inputs mapped as closely as the
  shapes allow (decision = ``needs_review``, rationale = the card's own summary). It is graded against the same FIT labels, which is
  not the question it was validated for -- that is the point of showing it.

The judge here is the offline stub (a deterministic rule set reading the prompt's text), so "agreement" measures how well one set
of rules I wrote matches one set of labels I wrote. See ``coursework/w12/eval_report.md`` for what that does and does not mean.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.judge import ClaimSummaryJudge
from app.llm import build_llm_provider
from app.models import RetrievedChunk

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "coursework" / "w12"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(100 * (centre - half), 1), round(100 * (centre + half), 1)


def user_message(row: dict[str, Any]) -> str:
    card = {
        "wording": [{"version": w["version"], "section": w["section"], "excerpt": w["excerpt"]} for w in row["cited"]],
        "reason_codes": row["reason_codes"],
        "signals": row["signals"],
    }
    return f"<fnol>\n{row['notice'].strip()}\n</fnol>\n\n<triage_card>\n{json.dumps(card)}\n</triage_card>"


def judge_integrated(rows: list[dict[str, Any]], prompt_path: Path) -> dict[str, bool]:
    settings = get_settings()
    provider = build_llm_provider(settings)
    system = prompt_path.read_text(encoding="utf-8")
    from app.judge import JUDGE_SCHEMA, _extract_json  # noqa: PLC0415

    verdicts = {}
    for row in rows:
        response = provider.complete(system=system, user_message=user_message(row), json_schema=JUDGE_SCHEMA, max_tokens=settings.answer_max_tokens)
        payload = _extract_json(response.text) or {}
        verdicts[row["id"]] = bool(payload.get("pass_criterion", False))
    return verdicts


def judge_week6_unchanged(rows: list[dict[str, Any]]) -> dict[str, bool]:
    settings = get_settings()
    judge = ClaimSummaryJudge(settings, (ROOT / "coursework" / "w6" / "judge_v2.txt").read_text(encoding="utf-8"))
    verdicts = {}
    for row in rows:
        retrieved = [
            RetrievedChunk(f"c{i}", "d", "x.pdf", w["excerpt"], 1, 1, w["section"], None, 1.0) for i, w in enumerate(row["cited"])
        ]
        summary = {
            "coverage_decision": "needs_review",
            "summary": " ".join(w["excerpt"] for w in row["cited"])[:600],
            "citations": [c.chunk_id for c in retrieved],
        }
        verdicts[row["id"]] = judge.judge(row["notice"], summary, retrieved).pass_criterion
    return verdicts


def agreement(verdicts: dict[str, bool], labels: dict[str, bool]) -> dict[str, Any]:
    ids = sorted(labels)
    agree = [i for i in ids if verdicts[i] == labels[i]]
    tp = sum(verdicts[i] and labels[i] for i in ids)
    tn = sum((not verdicts[i]) and (not labels[i]) for i in ids)
    fp = sum(verdicts[i] and not labels[i] for i in ids)  # judge says fit, human says not
    fn = sum((not verdicts[i]) and labels[i] for i in ids)
    low, high = wilson(len(agree), len(ids))
    return {
        "n": len(ids), "agree": len(agree), "agreement_pct": round(100 * len(agree) / len(ids), 1), "ci95_pct": [low, high],
        "confusion": {"judge_fit_human_fit": tp, "judge_unfit_human_unfit": tn, "judge_fit_human_unfit": fp, "judge_unfit_human_fit": fn},
        "disagreements": [i for i in ids if verdicts[i] != labels[i]],
        "bar_85": len(agree) / len(ids) >= 0.85,
    }


def run_judge(workdir: Path, *, prompt: Path = OUT / "judge_integrated.txt", rows_path: Path = OUT / "outputs_for_labelling.json",
              labels_path: Path = OUT / "labels.json", out_path: Path = OUT / "judge_results.json") -> dict[str, Any]:
    rows = json.loads(rows_path.read_text(encoding="utf-8"))
    labels_doc = json.loads(labels_path.read_text(encoding="utf-8"))
    labels = {item["id"]: item["fit"] for item in labels_doc["labels"]}
    rows = [r for r in rows if r["id"] in labels]
    missing = sorted(set(labels) - {r["id"] for r in rows})
    if missing:
        raise SystemExit(f"labelled but not in the outputs: {missing}")

    integrated = judge_integrated(rows, prompt)
    week6 = judge_week6_unchanged(rows)
    result = {
        "labels_sha256": hashlib.sha256(labels_path.read_bytes()).hexdigest()[:16],
        "outputs_sha256": hashlib.sha256(rows_path.read_bytes()).hexdigest()[:16],
        "judge_prompt_sha256": hashlib.sha256(prompt.read_bytes()).hexdigest()[:16],
        "integrated_judge": agreement(integrated, labels),
        "week6_judge_v2_unchanged": agreement(week6, labels),
        "verdicts_integrated": integrated,
        "labels": labels,
        "caveats": "stub judge (deterministic rules); single-rater labels by the system's author; n is small, see ci95",
    }
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    for name in ("integrated_judge", "week6_judge_v2_unchanged"):
        a = result[name]
        print(f"{name:28} agreement {a['agree']}/{a['n']} = {a['agreement_pct']}%  (95% CI {a['ci95_pct'][0]}-{a['ci95_pct'][1]})  "
              f"clears 85%: {a['bar_85']}  confusion {a['confusion']}")
        print(f"{'':28} disagreements: {a['disagreements']}")
    return result


SHIPPED_PROMPT = OUT / "judge_integrated.txt"
REJECTED_V2_PROMPT = OUT / "judge_integrated_v2_rejected.txt"


def _labels(path: Path) -> dict[str, bool]:
    return {item["id"]: item["fit"] for item in json.loads(path.read_text(encoding="utf-8"))["labels"]}


def run_all() -> dict[str, Any]:
    """Every judge x every labelled set, in one table. The tuned set is shown only to show what tuning does to it."""
    tuned_rows = json.loads((OUT / "outputs_for_labelling.json").read_text(encoding="utf-8"))
    tuned_labels = _labels(OUT / "labels.json")
    tuned_rows = [r for r in tuned_rows if r["id"] in tuned_labels]
    held_rows = json.loads((OUT / "heldout_outputs.json").read_text(encoding="utf-8"))
    held_labels = _labels(OUT / "labels_heldout.json")

    table: dict[str, dict[str, Any]] = {}
    for name, rows, labels, kind in (("tuning_set_28", tuned_rows, tuned_labels, "tuned"), ("heldout_10", held_rows, held_labels, "held-out")):
        table[name] = {
            "kind": kind,
            "shipped_v1": agreement(judge_integrated(rows, SHIPPED_PROMPT), labels),
            "rejected_v2_tuned_on_the_28": agreement(judge_integrated(rows, REJECTED_V2_PROMPT), labels),
            "week6_judge_v2_unchanged": agreement({**{i: False for i in labels}, **judge_week6_unchanged([r for r in rows if r["cited"]])}, labels),
        }
    combined = {}
    for key in ("shipped_v1", "week6_judge_v2_unchanged"):
        agree = table["tuning_set_28"][key]["agree"] + table["heldout_10"][key]["agree"]
        n = table["tuning_set_28"][key]["n"] + table["heldout_10"][key]["n"]
        low, high = wilson(agree, n)
        combined[key] = {"agree": agree, "n": n, "agreement_pct": round(100 * agree / n, 1), "ci95_pct": [low, high], "bar_85": agree / n >= 0.85}
    result = {
        "hashes": {
            "labels": hashlib.sha256((OUT / "labels.json").read_bytes()).hexdigest()[:16],
            "labels_heldout": hashlib.sha256((OUT / "labels_heldout.json").read_bytes()).hexdigest()[:16],
            "judge_shipped_prompt": hashlib.sha256(SHIPPED_PROMPT.read_bytes()).hexdigest()[:16],
            "judge_rejected_v2_prompt": hashlib.sha256(REJECTED_V2_PROMPT.read_bytes()).hexdigest()[:16],
        },
        "table": table,
        "combined_38_untuned_judges": combined,
        "caveats": "stub judge (deterministic rules); single rater who is also the judge's author; n = 28 + 10; the rejected v2 was tuned on the 28",
    }
    (OUT / "judge_results.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    for set_name, block in table.items():
        for judge_name in ("shipped_v1", "rejected_v2_tuned_on_the_28", "week6_judge_v2_unchanged"):
            a = block[judge_name]
            print(f"{set_name:14} {judge_name:30} {a['agree']:>2}/{a['n']:<2} = {a['agreement_pct']:>5}%  CI {a['ci95_pct'][0]:>5}-{a['ci95_pct'][1]:<5} 85% bar: {a['bar_85']!s:5}  disagreements {a['disagreements']}")
    for judge_name, a in combined.items():
        print(f"combined 38    {judge_name:30} {a['agree']}/{a['n']} = {a['agreement_pct']}%  CI {a['ci95_pct']}  85% bar: {a['bar_85']}")
    return result


if __name__ == "__main__":
    run_all()
