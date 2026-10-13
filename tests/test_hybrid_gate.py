"""Found by the Week 11 traces: in hybrid retrieval mode /ask answered nothing.

Hybrid mode (BM25 + dense, fused by Reciprocal Rank Fusion) reports a *fused rank score* -- at most about 0.033 with
k=60 -- but the relevance gate compared it with MIN_RELEVANCE=0.12, a floor calibrated for cosine similarity. Every
question was therefore "below the floor" and returned not-found with zero model calls. The default mode is dense,
which is why nothing noticed; all 51 /ask requests in the drill log were gated, which is what a trace field
(`output.gated`) makes visible and what no amount of reading answers would have.
"""

from __future__ import annotations

from pathlib import Path

from app.config import Settings
from app.rag import RagService
from app.registry import DocumentRegistry
from tests.conftest import StubGenerator

QUESTION = "What is the flood sub-limit in the annual aggregate?"


def build(settings: Settings, pdf: bytes, tmp_path: Path, **update: object) -> tuple[RagService, StubGenerator]:
    generator = StubGenerator()
    configured = settings.model_copy(update={"chroma_dir": tmp_path / "s", **update})
    service = RagService(configured, registry=DocumentRegistry(configured.registry_path), generator=generator)  # type: ignore[arg-type]
    service.ingest(pdf, "sample_endorsement_pack.pdf")
    return service, generator


def test_hybrid_mode_no_longer_rejects_every_question(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    service, generator = build(settings, sample_pdf_bytes, tmp_path, retrieval_mode="hybrid_bm25_rrf")
    outcome = service.ask(QUESTION)

    assert outcome.best_score is not None and outcome.best_score < settings.min_relevance  # the scores really are fused ranks
    assert outcome.gated is False and generator.call_count == 1
    assert outcome.answer.found


def test_the_cosine_floor_is_untouched_in_dense_mode(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    service, generator = build(settings, sample_pdf_bytes, tmp_path, retrieval_mode="dense")
    outcome = service.ask("What is the average annual rainfall in the Amazon basin?")
    assert outcome.gated is True and generator.call_count == 0  # an out-of-scope question still never reaches the model


def test_the_hybrid_floor_can_still_be_set(settings: Settings, sample_pdf_bytes: bytes, tmp_path: Path):
    service, generator = build(settings, sample_pdf_bytes, tmp_path, retrieval_mode="hybrid_bm25_rrf", min_relevance_hybrid=0.5)
    assert service.ask(QUESTION).gated is True and generator.call_count == 0
