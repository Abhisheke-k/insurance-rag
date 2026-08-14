"""Retrieval quality.

The headline requirement: **a known question retrieves the correct chunk in the
top 5**. "Correct" is defined by the fact the answer depends on (e.g. the flood
sub-limit of GBP 5,000,000), not by chunk id, so the assertion stays meaningful
when chunk boundaries move.
"""

from __future__ import annotations

import re

import pytest

from app.rag import RagService

from .conftest import EVAL_QUESTIONS, OUT_OF_SCOPE_QUESTION


def _normalise(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# --------------------------------------------------------------------------- #
# (b) the right chunk comes back in the top 5
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "case", EVAL_QUESTIONS, ids=[case["clause"][:28] for case in EVAL_QUESTIONS]
)
def test_known_question_retrieves_the_answering_chunk(ingested_service: RagService, case: dict):
    hits = ingested_service.retrieve(case["question"], top_k=5)

    assert len(hits) <= 5
    texts = [_normalise(hit.text) for hit in hits]
    assert any(_normalise(case["expects"]) in text for text in texts), (
        f"{case['question']!r} did not retrieve a chunk containing {case['expects']!r}.\n"
        + "\n".join(f"  #{i} {hit.score:.3f} {hit.section}" for i, hit in enumerate(hits, 1))
    )


def test_flood_sublimit_is_the_top_hit(ingested_service: RagService):
    """The clearest question in the pack should rank its clause first, not fifth."""
    hits = ingested_service.retrieve(
        "What is the flood sub-limit in the annual aggregate after the endorsement?", top_k=5
    )
    assert hits
    assert "GBP 5,000,000" in _normalise(hits[0].text)


def test_retrieval_scores_are_similarities_in_order(ingested_service: RagService):
    hits = ingested_service.retrieve("flood deductible", top_k=5)
    assert hits
    scores = [hit.score for hit in hits]
    assert scores == sorted(scores, reverse=True), "hits must be ordered best-first"
    assert all(-1.0 <= score <= 1.0 for score in scores), "scores must be cosine similarities"


def test_out_of_scope_question_scores_below_in_scope(ingested_service: RagService):
    """The relevance gate is only meaningful if the two populations separate."""
    in_scope = ingested_service.retrieve(EVAL_QUESTIONS[0]["question"], top_k=1)
    out_of_scope = ingested_service.retrieve(OUT_OF_SCOPE_QUESTION, top_k=1)

    assert in_scope and out_of_scope
    assert out_of_scope[0].score < in_scope[0].score
    assert out_of_scope[0].score < ingested_service.settings.min_relevance <= in_scope[0].score


def test_every_hit_carries_a_resolvable_citation(ingested_service: RagService):
    for hit in ingested_service.retrieve("cyber exclusion write-back", top_k=5):
        assert hit.chunk_id
        assert hit.filename.endswith(".pdf")
        assert hit.page >= 1
        assert hit.section


# --------------------------------------------------------------------------- #
# ingestion behaviour
# --------------------------------------------------------------------------- #
def test_ingest_reports_what_it_stored(service: RagService, sample_pdf_bytes: bytes):
    result = service.ingest(sample_pdf_bytes, "pack.pdf")

    assert result.pages >= 3
    assert result.chunks > 0
    assert result.blocks > result.chunks
    assert result.replaced is False
    assert service.store.count() == result.chunks
    assert result.chunk_size == service.settings.chunk_size


def test_reingesting_the_same_bytes_replaces_rather_than_duplicates(
    service: RagService, sample_pdf_bytes: bytes
):
    first = service.ingest(sample_pdf_bytes, "pack.pdf")
    second = service.ingest(sample_pdf_bytes, "pack.pdf")

    assert second.doc_id == first.doc_id
    assert second.replaced is True
    assert service.store.count() == first.chunks
    assert len(service.documents()) == 1


def test_ingest_rejects_a_non_pdf(service: RagService):
    with pytest.raises(Exception):
        service.ingest(b"this is not a pdf", "notes.txt")


def test_documents_records_the_settings_used(ingested_service: RagService):
    records = ingested_service.documents()
    assert len(records) == 1

    record = records[0]
    assert record.filename.endswith(".pdf")
    assert record.chunk_size == ingested_service.settings.chunk_size
    assert record.chunk_overlap == ingested_service.settings.chunk_overlap
    assert record.embedding_model.startswith("hashing:")
    assert record.sha256


def test_deleting_a_document_removes_its_chunks(ingested_service: RagService):
    record = ingested_service.documents()[0]

    assert ingested_service.delete_document(record.doc_id) is True
    assert ingested_service.documents() == []
    assert ingested_service.store.count() == 0
    assert ingested_service.retrieve("flood sub-limit") == []
