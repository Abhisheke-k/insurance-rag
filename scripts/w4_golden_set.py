"""Week 4 golden set: 12 real-adjuster-voice questions over the sample corpus.

Ground truth is resolved automatically -- each question carries an ``expects``
substring (a fact only the correct chunk's text contains), the same pattern
``scripts/make_sample_pdf.py`` already uses for ``EVAL_QUESTIONS`` and that
``scripts/chunk_size_comparison.py`` measures against. That is what lets
``golden_set.jsonl`` be regenerated deterministically from the corpus rather
than hand-typed chunk ids that would silently go stale the moment chunking
changes.

Five questions (H1-H5) hinge on an exact token a dense embedder has no
structural way to represent precisely: an exclusion code, a form edition
string, or a reference number. Seven (S1-S7) are ordinary coverage/condition
lookups the corpus already answers well. That mix -- not "12 questions
engineered to fail" -- is what makes the before/after numbers mean something.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import build_form_library_pdf, build_sample_pdf

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
GOLDEN_SET_PATH = ROOT / "coursework" / "w4" / "golden_set.jsonl"

#: The chunk_size this golden set is frozen against. Deliberately smaller than
#: the app's shipped CHUNK_SIZE=550: this corpus includes an exclusion-code
#: reference schedule (Form HO-0304, five editions), and schedule-style
#: content is looked up one code at a time -- it benefits from being
#: individually addressable rather than packed 3-4 clauses to a chunk the way
#: narrative endorsement text does. This is a deliberate, documented choice
#: for the golden-set corpus, not a change to the shipped default.
CHUNK_SIZE = 200
CHUNK_OVERLAP = 40
MIN_CHUNK_SIZE = 30

HardToken = Literal["exclusion_code", "form_edition", "reference_number", "policy_number", None]


@dataclass(frozen=True, slots=True)
class GoldenQuestion:
    id: str
    question: str
    expects: str  # substring present only in the correct chunk's text
    hard_token: HardToken  # None for the 7 ordinary semantic questions


QUESTIONS: list[GoldenQuestion] = [
    # -- H1-H5: exact-token lookups (exclusion code / form edition / reference / policy numbers) --
    GoldenQuestion(
        "H1",
        "Does exclusion E-17 apply under form HO-0304 ed. 03-24? Second flood at the "
        "same site, six weeks after the first.",
        "within 60 consecutive days",
        "exclusion_code",
    ),
    GoldenQuestion(
        "H2",
        "Under form HO-0304 edition 01-22, what's the day-count trigger for exclusion E-17?",
        "within 30 consecutive days",
        "form_edition",
    ),
    GoldenQuestion(
        "H3",
        "What's the endorsement reference number for the E-17 sequential water intrusion exclusion?",
        "END-2024-0417",
        "reference_number",
    ),
    GoldenQuestion(
        "H4",
        "What is the policy number shown on this endorsement pack?",
        "Policy Number GML-2024-88213",
        "policy_number",
    ),
    GoldenQuestion(
        "H5",
        "Under form HO-0304 edition 09-23, what's the day-count trigger for exclusion E-17?",
        "within 45 consecutive days",
        "form_edition",
    ),
    # -- S1-S7: ordinary semantic coverage/condition lookups --
    GoldenQuestion(
        "S1",
        "What did the flood sub-limit get bumped up to in the annual aggregate?",
        "GBP 5,000,000",
        None,
    ),
    GoldenQuestion(
        "S2",
        "What deductible applies if a flood loss happens inside a flood zone?",
        "GBP 250,000",
        None,
    ),
    GoldenQuestion(
        "S3",
        "How long is the waiting period on the business interruption section?",
        "72 consecutive hours",
        None,
    ),
    GoldenQuestion(
        "S4",
        "How much written notice do we need to give to cancel the policy?",
        "60 days written notice",
        None,
    ),
    GoldenQuestion(
        "S5",
        "How often does the insured need to test their flood barriers and sump pumps?",
        "six months",
        None,
    ),
    GoldenQuestion(
        "S6",
        "Does the cyber exclusion write back cover for fire damage?",
        "fire or explosion",
        None,
    ),
    GoldenQuestion(
        "S7",
        "How many days do we have to notify a business interruption claim?",
        "within 30 days",
        None,
    ),
]


def _existing_or_built(path: Path, builder) -> Path:
    """Reuse the PDF already on disk rather than rebuilding it.

    PyMuPDF stamps a creation timestamp into the file on every save, so two
    otherwise-identical builds hash differently -- and this corpus is
    content-addressed (``doc_id = sha256(bytes)[:16]``), which the frozen
    ``golden_set.jsonl`` chunk ids depend on. Regenerate only when the file is
    missing; run ``python -m scripts.make_sample_pdf`` explicitly to refresh it.
    """
    if not path.exists():
        builder(path)
    return path


def build_corpus_service(*, retrieval_mode: str = "dense", chroma_dir: Path | None = None) -> RagService:
    """Ingest the main pack + form library at the golden-set chunk settings."""
    main_pdf = _existing_or_built(DATA_DIR / "sample_endorsement_pack.pdf", build_sample_pdf)
    lib_pdf = _existing_or_built(DATA_DIR / "form_ho0304_archive.pdf", build_form_library_pdf)

    settings = Settings(
        embedding_provider="sentence-transformers",
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        min_chunk_size=MIN_CHUNK_SIZE,
        force_in_memory_store=True,
        chroma_dir=chroma_dir or (ROOT / ".eval_scratch" / "w4_store"),
        llm_provider="stub",
        retrieval_mode=retrieval_mode,  # type: ignore[arg-type]
        top_k=5,
        bm25_candidates=25,
    )
    service = RagService(settings, registry=DocumentRegistry(settings.registry_path))
    service.ingest(main_pdf.read_bytes(), "sample_endorsement_pack.pdf")
    service.ingest(lib_pdf.read_bytes(), "form_ho0304_archive.pdf")
    return service


def resolve_chunk_id(service: RagService, expects: str) -> str:
    """Find the one chunk in the corpus whose text contains ``expects``."""
    matches = [chunk.chunk_id for chunk in service.store.all_chunks() if expects in chunk.text]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one chunk containing {expects!r}, found {len(matches)}: {matches}")
    return matches[0]


def build_golden_set() -> list[dict]:
    """Resolve every question's known-correct chunk_id against the fixed corpus."""
    service = build_corpus_service(retrieval_mode="dense")
    rows = []
    for q in QUESTIONS:
        chunk_id = resolve_chunk_id(service, q.expects)
        rows.append(
            {
                "id": q.id,
                "question": q.question,
                "expects": q.expects,
                "hard_token": q.hard_token,
                "chunk_id": chunk_id,
            }
        )
    return rows


def main() -> None:
    rows = build_golden_set()
    GOLDEN_SET_PATH.parent.mkdir(parents=True, exist_ok=True)
    with GOLDEN_SET_PATH.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row) + "\n")
    hard = sum(1 for row in rows if row["hard_token"])
    print(f"wrote {GOLDEN_SET_PATH} ({len(rows)} questions, {hard} hard-token)")


if __name__ == "__main__":
    main()
