"""Generate a week of claim-summary traffic and write it to the trace log.

    python -m scripts.w5_generate_traffic

Ingests the corpus at the app's shipped defaults (CHUNK_SIZE=550) with
RETRIEVAL_MODE=hybrid_bm25_rrf -- this traffic is generated as if Week 4's fix
has already shipped, so what Week 5 finds here is whatever is left over after
retrieval was already fixed, not a rediscovery of the same problem.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from app.tracing import build_trace_record, write_trace
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf
from scripts.w5_scenarios import build_scenarios

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TRACE_LOG = ROOT / "coursework" / "w5" / "traces.log"


def main() -> None:
    main_pdf = DATA_DIR / "sample_endorsement_pack.pdf"
    lib_pdf = DATA_DIR / "form_ho0304_archive.pdf"
    if not main_pdf.exists():
        build_sample_pdf(main_pdf)
    if not lib_pdf.exists():
        build_form_library_pdf(lib_pdf)

    settings = Settings(
        embedding_provider="sentence-transformers",
        force_in_memory_store=True,
        chroma_dir=ROOT / ".eval_scratch" / "w5_store",
        llm_provider="stub",
        retrieval_mode="hybrid_bm25_rrf",
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(lib_pdf.read_bytes(), "form_ho0304_archive.pdf")

    TRACE_LOG.parent.mkdir(parents=True, exist_ok=True)
    if TRACE_LOG.exists():
        TRACE_LOG.unlink()

    scenarios = build_scenarios()
    decisions: dict[str, int] = {}
    for index, scenario in enumerate(scenarios, start=1):
        trace_id = f"trc_{index:05d}"
        outcome = service.summarize_claim(scenario.notes)
        record = build_trace_record(
            trace_id=trace_id,
            adjuster_notes=scenario.notes,
            retrieval_mode=settings.retrieval_mode,
            retrieved=outcome.retrieved,
            summary=outcome.summary,
            effort=settings.answer_effort,
            max_tokens=settings.answer_max_tokens,
        )
        write_trace(record, TRACE_LOG)
        decisions[outcome.summary.coverage_decision] = decisions.get(outcome.summary.coverage_decision, 0) + 1

    line_count = len(TRACE_LOG.read_text(encoding="utf-8").splitlines())
    print(f"wrote {len(scenarios)} traces ({line_count} lines) to {TRACE_LOG}")
    print("coverage_decision distribution:", decisions)


if __name__ == "__main__":
    main()
