"""HTTP contract tests for the four endpoints."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.generation import NOT_FOUND_MESSAGE
from app.main import create_app
from app.rag import RagService

from .conftest import EVAL_QUESTIONS, OUT_OF_SCOPE_QUESTION, StubGenerator


@pytest.fixture
def client(settings: Settings, service: RagService) -> TestClient:
    with TestClient(create_app(settings=settings, service=service)) as test_client:
        yield test_client


@pytest.fixture
def loaded_client(client: TestClient, sample_pdf_bytes: bytes) -> TestClient:
    response = client.post(
        "/ingest",
        files={"files": ("sample_endorsement_pack.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert response.status_code == 200, response.text
    return client


# --------------------------------------------------------------------------- #
def test_health_reports_resolved_configuration(client: TestClient):
    body = client.get("/health").json()

    assert body["status"] == "ok"
    assert body["chunking"]["chunk_size"] == 550
    assert body["chunking"]["chunk_overlap"] == 120
    assert body["embeddings"]["model"].startswith("hashing:")
    assert body["vector_store"]["backend"] in {"chromadb", "in-memory"}
    assert body["retrieval"]["top_k"] == 5
    assert "configured" in body["generation"]


def test_ingest_returns_per_document_counts(client: TestClient, sample_pdf_bytes: bytes):
    response = client.post(
        "/ingest", files={"files": ("pack.pdf", sample_pdf_bytes, "application/pdf")}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["errors"] == []
    assert len(body["documents"]) == 1

    document = body["documents"][0]
    assert document["filename"] == "pack.pdf"
    assert document["chunks"] > 0
    assert document["pages"] >= 3
    assert body["total_chunks"] == document["chunks"]


def test_ingest_rejects_a_non_pdf(client: TestClient):
    response = client.post(
        "/ingest", files={"files": ("notes.txt", b"just some text", "text/plain")}
    )

    assert response.status_code == 400
    assert "PDF" in response.json()["detail"]


def test_ingest_accepts_several_files_at_once(client: TestClient, sample_pdf_bytes: bytes):
    response = client.post(
        "/ingest",
        files=[
            ("files", ("a.pdf", sample_pdf_bytes, "application/pdf")),
            ("files", ("b.pdf", sample_pdf_bytes, "application/pdf")),
        ],
    )

    assert response.status_code == 200
    body = response.json()
    # Same bytes -> same content-addressed doc id, so the second replaces the first.
    assert len(body["documents"]) == 2
    assert body["documents"][1]["replaced"] is True


def test_documents_lists_what_was_ingested(loaded_client: TestClient):
    body = loaded_client.get("/documents").json()

    assert body["count"] == 1
    assert body["total_chunks"] > 0
    document = body["documents"][0]
    assert document["filename"] == "sample_endorsement_pack.pdf"
    assert document["chunk_size"] == 550
    assert document["embedding_model"].startswith("hashing:")


def test_ask_returns_answer_citations_and_chunks_used(loaded_client: TestClient):
    response = loaded_client.post("/ask", json={"question": EVAL_QUESTIONS[0]["question"]})

    assert response.status_code == 200
    body = response.json()
    assert set(body) >= {"answer", "citations", "chunks_used"}
    assert body["found"] is True
    assert body["chunks_used"] > 0
    assert body["citations"]

    citation = body["citations"][0]
    assert set(citation) >= {"filename", "page", "section"}
    assert citation["filename"].endswith(".pdf")
    assert citation["page"] >= 1

    # Retrieval trace: everything considered, flagged with whether it was cited.
    assert body["retrieval"]
    assert any(hit["cited"] for hit in body["retrieval"])


def test_ask_out_of_scope_returns_the_not_found_shape(
    loaded_client: TestClient, stub_generator: StubGenerator
):
    body = loaded_client.post("/ask", json={"question": OUT_OF_SCOPE_QUESTION}).json()

    assert body["answer"] == NOT_FOUND_MESSAGE
    assert body["found"] is False
    assert body["citations"] == []
    assert body["chunks_used"] == 0
    assert stub_generator.call_count == 0


def test_ask_rejects_an_empty_question(loaded_client: TestClient):
    assert loaded_client.post("/ask", json={"question": ""}).status_code == 422


def test_ask_honours_top_k(loaded_client: TestClient, stub_generator: StubGenerator):
    loaded_client.post("/ask", json={"question": "flood deductible", "top_k": 2})

    assert stub_generator.call_count == 1
    _question, retrieved = stub_generator.calls[0]
    assert len(retrieved) == 2


def test_delete_document_removes_it(loaded_client: TestClient):
    doc_id = loaded_client.get("/documents").json()["documents"][0]["doc_id"]

    assert loaded_client.delete(f"/documents/{doc_id}").status_code == 204
    assert loaded_client.get("/documents").json()["count"] == 0
    assert loaded_client.delete(f"/documents/{doc_id}").status_code == 404


def test_agent_process_claim_returns_steps_and_decision(loaded_client: TestClient):
    response = loaded_client.post(
        "/agent/process-claim",
        json={"adjuster_notes": "Claim CLM-2024-00123: flood damage to the warehouse on 12 March 2024. GBP 15,000 claimed."},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["stopped_reason"] == "finalized"
    assert body["llm_calls"] == len(body["steps"])
    assert len(body["steps"]) >= 3
    assert {step["action"] for step in body["steps"]} >= {"search_policy", "check_assertions"}
    assert body["coverage_decision"] in {"covered", "denied", "partially_covered", "needs_review"}


def test_agent_process_claim_rejects_empty_notes(loaded_client: TestClient):
    assert loaded_client.post("/agent/process-claim", json={"adjuster_notes": ""}).status_code == 422


def test_frontend_is_served(client: TestClient):
    response = client.get("/ui/", follow_redirects=True)
    assert response.status_code == 200
    assert "<html" in response.text.lower()
