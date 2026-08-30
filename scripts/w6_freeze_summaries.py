"""Generate the 25 claim summaries used for blind labeling, and freeze them.

    python -m scripts.w6_freeze_summaries

Writes coursework/w6/frozen_summaries_25.json -- both the blind-labeling step
and every judge run read from this frozen file, so "the judge" and "the human"
are looking at exactly the same 25 artifacts.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf
from scripts.w6_eval_set import EVAL_SET_PATH

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
FROZEN_PATH = ROOT / "coursework" / "w6" / "frozen_summaries_25.json"
LABEL_COUNT = 25


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
        chroma_dir=ROOT / ".eval_scratch" / "w6_store",
        llm_provider="stub",
        retrieval_mode="hybrid_bm25_rrf",
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(lib_pdf.read_bytes(), "form_ho0304_archive.pdf")

    cases = [json.loads(line) for line in EVAL_SET_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    cases = cases[:LABEL_COUNT]

    frozen = []
    for case in cases:
        outcome = service.summarize_claim(case["notes"])
        s = outcome.summary
        frozen.append(
            {
                "id": case["id"],
                "mode": case["mode"],
                "notes": case["notes"],
                "summary": {
                    "claim_number": s.claim_number,
                    "date_of_loss": s.date_of_loss,
                    "coverage_decision": s.coverage_decision,
                    "cited_exclusion_id": s.cited_exclusion_id,
                    "excess_amount": s.excess_amount,
                    "summary": s.summary,
                    "citations": [c.chunk_id for c in s.citations],
                },
                "retrieved": [
                    {
                        "chunk_id": c.chunk_id, "doc_id": c.doc_id, "filename": c.filename,
                        "page": c.page, "page_end": c.page_end, "section_path": c.section_path,
                        "clause_label": c.clause_label, "score": round(c.score, 4), "text": c.text,
                    }
                    for c in outcome.retrieved
                ],
            }
        )

    FROZEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    FROZEN_PATH.write_text(json.dumps(frozen, indent=2), encoding="utf-8")
    print(f"froze {len(frozen)} summaries to {FROZEN_PATH}")


if __name__ == "__main__":
    main()
