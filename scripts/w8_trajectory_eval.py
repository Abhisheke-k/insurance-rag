"""Week 8: trajectory evaluation for the claims agent (app/agent.py).

Week 7 raced the agent against the fixed workflow on *outcome* (decision
accuracy) alone, then found by hand, reading race_raw.json, that one scenario
-- sequential_flood_e17 -- got the right decision for the wrong reason (cited
clause 4.1, not the exclusion code, 4.3, that actually governs a second flood
event). This script turns that hand-read finding into a repeatable, scored
check across every scenario: for each one it grades the *outcome*
(coverage_decision correct?) and the *trajectory* (did the agent's tools
surface and ground the decision in the passage/assertion that actually
decides it?) independently, and reports where they disagree.

    python -m scripts.w8_trajectory_eval

Trajectory grading is scenario-specific (coursework/w8/trajectory_scenarios.jsonl,
field "grounding"), because "the right path" means something different per
scenario:
  - clause_keyword   -- at least one citation's section names the clause that
                        actually controls the decision (not just any clause).
  - assertion_failure -- check_assertions actually flagged the named check as
                        FAIL somewhere in its observation.
  - no_citation      -- the agent honestly finalized with no citation, rather
                        than confidently citing an unrelated clause.

Tool-choice accuracy is graded separately and more narrowly: whether the
agent issued at least "expected_min_searches" search_policy calls. The loop
(app/agent.py) enforces *order* (search, then check, then finalize) but never
a search *count* -- so this check is squarely about the deterministic stub
persona's behaviour, not the loop's, and is reported as such.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agent import AgentClaimResult
from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "data" / "sample_endorsement_pack.pdf"
DEFAULT_SCENARIOS = ROOT / "coursework" / "w8" / "trajectory_scenarios.jsonl"
DEFAULT_RESULTS_MD = ROOT / "coursework" / "w8" / "trajectory_results.md"
DEFAULT_RAW_JSON = ROOT / "coursework" / "w8" / "trajectory_raw.json"


def _search_count(result: AgentClaimResult) -> int:
    return sum(1 for s in result.steps if s.action == "search_policy")


def _grade_grounding(result: AgentClaimResult, grounding: dict[str, Any]) -> tuple[bool, str]:
    kind = grounding["type"]
    if kind == "clause_keyword":
        # Graded against the STATED RATIONALE (summary.summary), not the whole
        # cited chunk's text -- the same principle Week 6's judge v2 learned
        # the hard way (app/llm.py, _judge): this corpus packs several clauses
        # into one chunk (e.g. Endorsement 1's clauses 1-4.1 together, or the
        # whole cyber exclusion plus its write-back), so "the right keyword
        # appears somewhere in the cited chunk" is true almost by construction
        # and proves nothing about what the decision actually rests on. The
        # rationale sentence is what the stub (or a real model) actually
        # quoted as its reason, which is the thing worth checking.
        rationale = result.summary.summary.upper()
        wanted = [w.upper() for w in grounding["any_of"]]
        hit = next((w for w in wanted if w in rationale), None)
        if hit:
            return True, f"stated rationale grounded in {hit!r}"
        return False, f"expected one of {grounding['any_of']!r} in the stated rationale, got: {result.summary.summary!r}"

    if kind == "assertion_failure":
        name = grounding["name"]
        for step in result.steps:
            if step.action == "check_assertions" and f"{name}=FAIL" in step.observation:
                return True, f"{name}=FAIL correctly surfaced by check_assertions"
        return False, f"{name}=FAIL was never surfaced by check_assertions"

    if kind == "no_citation":
        if not result.summary.citations:
            return True, "no citation offered -- honest about finding nothing"
        cited = "; ".join(c.section for c in result.summary.citations)
        return False, f"confidently cited an unrelated clause instead of admitting no match: {cited}"

    raise ValueError(f"unknown grounding type {kind!r}")  # pragma: no cover - fixture bug, not a runtime case


@dataclass(frozen=True, slots=True)
class Grade:
    scenario_id: str
    expected_decision: str
    actual_decision: str
    outcome_correct: bool
    searches: int
    expected_min_searches: int
    tool_choice_ok: bool
    path_correct: bool
    path_reason: str
    category: str  # both_ok | right_outcome_wrong_path | wrong_outcome_right_path | both_wrong
    cited_sections: list[str]


def _category(outcome_correct: bool, path_correct: bool) -> str:
    if outcome_correct and path_correct:
        return "both_ok"
    if outcome_correct and not path_correct:
        return "right_outcome_wrong_path"
    if not outcome_correct and path_correct:
        return "wrong_outcome_right_path"
    return "both_wrong"


def grade_scenario(result: AgentClaimResult, scenario: dict[str, Any]) -> Grade:
    outcome_correct = result.summary.coverage_decision == scenario["expected_coverage_decision"]
    searches = _search_count(result)
    tool_choice_ok = searches >= scenario["expected_min_searches"]
    path_correct, path_reason = _grade_grounding(result, scenario["grounding"])
    return Grade(
        scenario_id=scenario["id"],
        expected_decision=scenario["expected_coverage_decision"],
        actual_decision=result.summary.coverage_decision,
        outcome_correct=outcome_correct,
        searches=searches,
        expected_min_searches=scenario["expected_min_searches"],
        tool_choice_ok=tool_choice_ok,
        path_correct=path_correct,
        path_reason=path_reason,
        category=_category(outcome_correct, path_correct),
        cited_sections=[c.section for c in result.summary.citations],
    )


def build_service(pdf_bytes: bytes, *, workdir: Path) -> RagService:
    settings = Settings(
        embedding_provider="hashing",
        force_in_memory_store=True,
        chroma_dir=workdir,
        llm_provider="stub",
        top_k=5,
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(pdf_bytes, "sample_endorsement_pack.pdf")
    return service


def _row(*cells: Any) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def render_markdown(grades: list[Grade]) -> str:
    lines = [
        "# Week 8 -- Trajectory evaluation of the claims agent",
        "",
        "Scores `app.agent.ClaimsAgent` (Week 7) on two independent axes per "
        "scenario: **outcome** (is `coverage_decision` right?) and "
        "**trajectory** (is the decision actually grounded in the passage or "
        "assertion that controls it?). Scenarios: "
        "[`trajectory_scenarios.jsonl`](trajectory_scenarios.jsonl). LLM "
        "backend: `stub` (deterministic, no API key -- see `app/llm.py`).",
        "",
        "```bash",
        "python -m scripts.w8_trajectory_eval",
        "```",
        "",
        "## Per-scenario",
        "",
        _row(
            "Scenario", "Expected", "Actual", "Outcome", "Searches (actual/min)",
            "Tool-choice", "Path", "Category", "Path reason",
        ),
        "|" + "---|" * 9,
    ]
    for g in grades:
        lines.append(
            _row(
                g.scenario_id,
                g.expected_decision,
                g.actual_decision,
                "yes" if g.outcome_correct else "no",
                f"{g.searches}/{g.expected_min_searches}",
                "yes" if g.tool_choice_ok else "no",
                "yes" if g.path_correct else "no",
                g.category,
                g.path_reason,
            )
        )

    n = len(grades)
    outcome_acc = sum(1 for g in grades if g.outcome_correct) / n
    path_acc = sum(1 for g in grades if g.path_correct) / n
    tool_acc = sum(1 for g in grades if g.tool_choice_ok) / n
    gap_cases = [g for g in grades if g.category == "right_outcome_wrong_path"]

    lines += [
        "",
        "## Aggregate",
        "",
        _row("Metric", "Value"),
        "|---|---|",
        _row("Outcome accuracy", f"{sum(1 for g in grades if g.outcome_correct)}/{n} ({outcome_acc:.0%})"),
        _row("Trajectory (path) accuracy", f"{sum(1 for g in grades if g.path_correct)}/{n} ({path_acc:.0%})"),
        _row("Tool-choice accuracy (search count)", f"{sum(1 for g in grades if g.tool_choice_ok)}/{n} ({tool_acc:.0%})"),
        _row("Outcome vs. trajectory gap", f"{outcome_acc - path_acc:+.0%}"),
        _row("Correct-answer-wrong-path cases", str(len(gap_cases))),
        "",
    ]
    if gap_cases:
        lines.append(
            "**Correct-answer-wrong-path scenario(s):** "
            + ", ".join(f"`{g.scenario_id}`" for g in gap_cases)
            + " -- the decision label matches the expected one, but the "
            "grounding does not, which is exactly the failure mode this "
            "script exists to catch (a right answer for the wrong reason "
            "will not survive the next case that needs the reasoning to be "
            "right, not just the label)."
        )
        lines.append("")

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument("--out-md", type=Path, default=DEFAULT_RESULTS_MD)
    parser.add_argument("--out-json", type=Path, default=DEFAULT_RAW_JSON)
    arguments = parser.parse_args()

    pdf_path = arguments.pdf
    if not pdf_path.exists():
        print(f"{pdf_path} not found; generating the sample pack")
        build_sample_pdf(pdf_path)
    pdf_bytes = pdf_path.read_bytes()

    scenario_lines = arguments.scenarios.read_text(encoding="utf-8").splitlines()
    scenarios = [json.loads(line) for line in scenario_lines if line.strip()]

    workdir = ROOT / ".w8-trajectory"
    service = build_service(pdf_bytes, workdir=workdir)

    grades: list[Grade] = []
    raw_results: list[dict[str, Any]] = []
    for scenario in scenarios:
        result = service.process_claim_with_agent(scenario["adjuster_notes"])
        grade = grade_scenario(result, scenario)
        grades.append(grade)
        raw_results.append(
            {
                "scenario": scenario,
                "grade": {
                    "outcome_correct": grade.outcome_correct,
                    "actual_decision": grade.actual_decision,
                    "searches": grade.searches,
                    "tool_choice_ok": grade.tool_choice_ok,
                    "path_correct": grade.path_correct,
                    "path_reason": grade.path_reason,
                    "category": grade.category,
                    "cited_sections": grade.cited_sections,
                },
                "steps": [
                    {
                        "step_number": s.step_number,
                        "action": s.action,
                        "thought": s.thought,
                        "observation": s.observation[:2000],
                    }
                    for s in result.steps
                ],
            }
        )

    table = render_markdown(grades)
    print(table)

    arguments.out_md.parent.mkdir(parents=True, exist_ok=True)
    arguments.out_md.write_text(table, encoding="utf-8")
    print(f"\nwritten to {arguments.out_md}")

    arguments.out_json.parent.mkdir(parents=True, exist_ok=True)
    arguments.out_json.write_text(json.dumps(raw_results, indent=2), encoding="utf-8")
    print(f"written to {arguments.out_json}")


if __name__ == "__main__":
    main()
