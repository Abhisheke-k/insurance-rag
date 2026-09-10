"""Race the Week 7 claims agent against the existing fixed workflow.

Fixed workflow: ``RagService.summarize_claim()`` (``app/rag.py``) followed by
``run_assertions()`` (``app/assertions.py``) -- one retrieve, one LLM call,
already shipped by Weeks 5-6. Agent: ``RagService.process_claim_with_agent()``
(``app/agent.py``) -- a hand-built think/act/observe loop over two tools
(``search_policy``, ``check_assertions``), bounded by ``AGENT_MAX_STEPS`` /
``AGENT_TIMEOUT_SECONDS``.

Both run against the same ingested corpus and the same
``coursework/w7/claim_scenarios.jsonl`` cases, under the deterministic stub LLM
provider by default (no API key, no network, reproducible); pass
``--llm-provider anthropic`` for a live-model comparison.

    python -m scripts.w7_agent_vs_workflow
    python -m scripts.w7_agent_vs_workflow --llm-provider anthropic
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.assertions import run_assertions
from app.claims import ClaimSummary
from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PDF = ROOT / "data" / "sample_endorsement_pack.pdf"
DEFAULT_SCENARIOS = ROOT / "coursework" / "w7" / "claim_scenarios.jsonl"
DEFAULT_RESULTS_MD = ROOT / "coursework" / "w7" / "results.md"
DEFAULT_RAW_JSON = ROOT / "coursework" / "w7" / "race_raw.json"


def _summary_to_dict(summary: ClaimSummary) -> dict[str, Any]:
    return {
        "claim_number": summary.claim_number,
        "date_of_loss": summary.date_of_loss,
        "coverage_decision": summary.coverage_decision,
        "cited_exclusion_id": summary.cited_exclusion_id,
        "excess_amount": summary.excess_amount,
        "summary": summary.summary,
        "citations": [c.chunk_id for c in summary.citations],
        "chunks_used": summary.chunks_used,
        "notes": summary.notes,
    }


@dataclass(frozen=True, slots=True)
class Run:
    scenario_id: str
    pipeline: str  # "fixed" | "agent"
    elapsed_seconds: float
    llm_calls: int
    coverage_decision: str
    correct: bool
    assertions_passed: int
    assertions_total: int
    summary: dict[str, Any]
    steps: list[dict[str, Any]] | None = None


def _run_fixed(service: RagService, scenario: dict[str, Any]) -> Run:
    start = time.perf_counter()
    outcome = service.summarize_claim(scenario["adjuster_notes"])
    elapsed = time.perf_counter() - start

    summary_dict = _summary_to_dict(outcome.summary)
    assertion_results = run_assertions(summary_dict)
    return Run(
        scenario_id=scenario["id"],
        pipeline="fixed",
        elapsed_seconds=elapsed,
        llm_calls=1,
        coverage_decision=outcome.summary.coverage_decision,
        correct=outcome.summary.coverage_decision == scenario["expected_coverage_decision"],
        assertions_passed=sum(1 for r in assertion_results if r.passed),
        assertions_total=len(assertion_results),
        summary=summary_dict,
    )


def _run_agent(service: RagService, scenario: dict[str, Any]) -> Run:
    start = time.perf_counter()
    result = service.process_claim_with_agent(scenario["adjuster_notes"])
    elapsed = time.perf_counter() - start

    summary_dict = _summary_to_dict(result.summary)
    assertion_results = run_assertions(summary_dict)
    return Run(
        scenario_id=scenario["id"],
        pipeline="agent",
        elapsed_seconds=elapsed,
        llm_calls=result.llm_calls,
        coverage_decision=result.summary.coverage_decision,
        correct=result.summary.coverage_decision == scenario["expected_coverage_decision"],
        assertions_passed=sum(1 for r in assertion_results if r.passed),
        assertions_total=len(assertion_results),
        summary=summary_dict,
        steps=[
            {
                "step_number": s.step_number,
                "thought": s.thought,
                "action": s.action,
                "action_input": s.action_input,
                "observation": s.observation,
                "elapsed_seconds": round(s.elapsed_seconds, 4),
            }
            for s in result.steps
        ],
    )


def build_service(pdf_bytes: bytes, *, workdir: Path, llm_provider: str) -> RagService:
    settings = Settings(
        embedding_provider="hashing",
        force_in_memory_store=True,
        chroma_dir=workdir,
        llm_provider=llm_provider,  # type: ignore[arg-type]
        top_k=5,
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(pdf_bytes, "sample_endorsement_pack.pdf")
    return service


def _aggregate(runs: list[Run]) -> dict[str, float]:
    n = len(runs)
    return {
        "n": n,
        "correct": sum(1 for r in runs if r.correct),
        "mean_seconds": statistics.fmean(r.elapsed_seconds for r in runs),
        "total_seconds": sum(r.elapsed_seconds for r in runs),
        "mean_llm_calls": statistics.fmean(r.llm_calls for r in runs),
        "assertions_rate": (
            sum(r.assertions_passed for r in runs) / sum(r.assertions_total for r in runs)
            if runs
            else 0.0
        ),
    }


def _row(*cells: Any) -> str:
    return "| " + " | ".join(str(cell) for cell in cells) + " |"


def render_markdown(runs: list[Run], scenarios: list[dict[str, Any]], *, llm_provider: str) -> str:
    by_scenario: dict[str, dict[str, Run]] = {}
    for run in runs:
        by_scenario.setdefault(run.scenario_id, {})[run.pipeline] = run

    lines = [
        "# Week 7 -- Claims agent vs. fixed workflow: results",
        "",
        "Fixed workflow: `RagService.summarize_claim()` (`app/rag.py`) + "
        "`run_assertions()` (`app/assertions.py`) -- one retrieve, one LLM call, "
        "the feature Weeks 5-6 already tuned. Agent: "
        "`RagService.process_claim_with_agent()` (`app/agent.py`) -- a hand-built "
        "think/act/observe loop over `search_policy` and `check_assertions`, "
        f"bounded by `AGENT_MAX_STEPS`/`AGENT_TIMEOUT_SECONDS`. LLM backend: "
        f"`{llm_provider}`. Full step-by-step traces: [`race_raw.json`](race_raw.json).",
        "",
        "```bash",
        "python -m scripts.w7_agent_vs_workflow",
        "```",
        "",
        "## Per-scenario",
        "",
        _row(
            "Scenario", "Expected", "Fixed decision", "Fixed correct", "Fixed time (s)",
            "Fixed LLM calls", "Fixed assertions", "Agent decision", "Agent correct",
            "Agent time (s)", "Agent LLM calls", "Agent assertions",
        ),
        "|" + "---|" * 12,
    ]

    for scenario in scenarios:
        sid = scenario["id"]
        fixed = by_scenario[sid]["fixed"]
        agent = by_scenario[sid]["agent"]
        fixed_ok = "yes" if fixed.correct else "no"
        agent_ok = "yes" if agent.correct else "no"
        lines.append(
            _row(
                sid, scenario["expected_coverage_decision"],
                fixed.coverage_decision, fixed_ok, f"{fixed.elapsed_seconds:.4f}",
                fixed.llm_calls, f"{fixed.assertions_passed}/{fixed.assertions_total}",
                agent.coverage_decision, agent_ok, f"{agent.elapsed_seconds:.4f}",
                agent.llm_calls, f"{agent.assertions_passed}/{agent.assertions_total}",
            )
        )

    fixed_runs = [r for r in runs if r.pipeline == "fixed"]
    agent_runs = [r for r in runs if r.pipeline == "agent"]
    fixed_agg = _aggregate(fixed_runs)
    agent_agg = _aggregate(agent_runs)

    def _pct(agg: dict[str, float]) -> str:
        return f"{agg['correct']}/{agg['n']} ({agg['correct'] / agg['n']:.0%})"

    lines += [
        "",
        "## Aggregate",
        "",
        _row("Metric", "Fixed workflow", "Agent"),
        "|---|---|---|",
        _row("Decision accuracy", _pct(fixed_agg), _pct(agent_agg)),
        _row(
            "Mean wall-clock per claim (s)",
            f"{fixed_agg['mean_seconds']:.4f}",
            f"{agent_agg['mean_seconds']:.4f}",
        ),
        _row(
            f"Total wall-clock, {fixed_agg['n']} claims (s)",
            f"{fixed_agg['total_seconds']:.4f}",
            f"{agent_agg['total_seconds']:.4f}",
        ),
        _row(
            "Mean LLM calls/claim (cost proxy)",
            f"{fixed_agg['mean_llm_calls']:.1f}",
            f"{agent_agg['mean_llm_calls']:.1f}",
        ),
        _row(
            "Deterministic assertions passed",
            f"{fixed_agg['assertions_rate']:.0%}",
            f"{agent_agg['assertions_rate']:.0%}",
        ),
        "",
        "`LLMResponse` (`app/llm.py`) does not currently expose token usage, so LLM "
        "call count is the cost proxy here -- every call in this comparison carries "
        "one claim-sized context, so call count tracks spend reasonably well. A "
        "token-metered comparison would need `AnthropicProvider`/`OpenAIProvider` to "
        "surface `response.usage`, which they do not do today.",
        "",
    ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--pdf", type=Path, default=DEFAULT_PDF)
    parser.add_argument("--scenarios", type=Path, default=DEFAULT_SCENARIOS)
    parser.add_argument(
        "--llm-provider",
        default="stub",
        choices=["stub", "anthropic", "openai", "auto"],
        help="default: stub -- deterministic, no API key, reproducible",
    )
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

    workdir = ROOT / ".w7-race"
    service = build_service(pdf_bytes, workdir=workdir, llm_provider=arguments.llm_provider)

    runs: list[Run] = []
    for scenario in scenarios:
        runs.append(_run_fixed(service, scenario))
        runs.append(_run_agent(service, scenario))

    table = render_markdown(runs, scenarios, llm_provider=arguments.llm_provider)
    print(table)

    arguments.out_md.parent.mkdir(parents=True, exist_ok=True)
    arguments.out_md.write_text(table, encoding="utf-8")
    print(f"\nwritten to {arguments.out_md}")

    raw = {
        "llm_provider": arguments.llm_provider,
        "scenarios": scenarios,
        "runs": [
            {
                "scenario_id": r.scenario_id,
                "pipeline": r.pipeline,
                "elapsed_seconds": round(r.elapsed_seconds, 4),
                "llm_calls": r.llm_calls,
                "coverage_decision": r.coverage_decision,
                "correct": r.correct,
                "assertions_passed": r.assertions_passed,
                "assertions_total": r.assertions_total,
                "summary": r.summary,
                "steps": r.steps,
            }
            for r in runs
        ],
    }
    arguments.out_json.parent.mkdir(parents=True, exist_ok=True)
    arguments.out_json.write_text(json.dumps(raw, indent=2), encoding="utf-8")
    print(f"written to {arguments.out_json}")


if __name__ == "__main__":
    main()
