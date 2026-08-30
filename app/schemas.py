"""HTTP request/response shapes.

Kept separate from :mod:`app.models` so the wire contract can evolve without
dragging the ingestion internals along with it.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.claims import ClaimSummary
from app.models import Citation, DocumentRecord
from app.rag import IngestResult

__all__ = [
    "AskRequest",
    "AskResponse",
    "CitationModel",
    "ClaimSummaryResponse",
    "DocumentModel",
    "DocumentsResponse",
    "HealthResponse",
    "IngestResponse",
    "IngestedDocumentModel",
    "RetrievalHit",
    "SummarizeClaimRequest",
]


class CitationModel(BaseModel):
    """One traceable pointer from the answer back to a stored chunk."""

    filename: str
    page: int
    section: str
    # Extras beyond the required triple: they make a citation verifiable
    # without a second API call.
    chunk_id: str
    score: float
    snippet: str

    @classmethod
    def from_citation(cls, citation: Citation) -> "CitationModel":
        return cls(
            filename=citation.filename,
            page=citation.page,
            section=citation.section,
            chunk_id=citation.chunk_id,
            score=citation.score,
            snippet=citation.snippet,
        )


class RetrievalHit(BaseModel):
    """A retrieved chunk, whether or not the model ended up citing it."""

    rank: int
    chunk_id: str
    filename: str
    page: int
    section: str
    score: float
    cited: bool


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=4000)
    top_k: int | None = Field(default=None, ge=1, le=50)


class AskResponse(BaseModel):
    answer: str
    citations: list[CitationModel]
    chunks_used: int
    # Additive fields: `found` drives the amber "not found" banner in the UI,
    # `retrieval` lets you audit what was considered but not cited.
    found: bool
    model: str | None = None
    retrieval: list[RetrievalHit] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class SummarizeClaimRequest(BaseModel):
    adjuster_notes: str = Field(min_length=1, max_length=8000)
    top_k: int | None = Field(default=None, ge=1, le=50)


class ClaimSummaryResponse(BaseModel):
    claim_number: str
    date_of_loss: str
    coverage_decision: str
    cited_exclusion_id: str | None = None
    excess_amount: float | None = None
    summary: str
    citations: list[CitationModel]
    chunks_used: int
    model: str | None = None
    retrieval: list[RetrievalHit] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @classmethod
    def from_summary(cls, summary: ClaimSummary, retrieval: list[RetrievalHit]) -> "ClaimSummaryResponse":
        return cls(
            claim_number=summary.claim_number,
            date_of_loss=summary.date_of_loss,
            coverage_decision=summary.coverage_decision,
            cited_exclusion_id=summary.cited_exclusion_id,
            excess_amount=summary.excess_amount,
            summary=summary.summary,
            citations=[CitationModel.from_citation(c) for c in summary.citations],
            chunks_used=summary.chunks_used,
            model=summary.model,
            retrieval=retrieval,
            notes=summary.notes,
        )


class IngestedDocumentModel(BaseModel):
    doc_id: str
    filename: str
    pages: int
    blocks: int
    chunks: int
    tokens: int
    replaced: bool
    chunk_size: int
    chunk_overlap: int
    embedding_model: str

    @classmethod
    def from_result(cls, result: IngestResult) -> "IngestedDocumentModel":
        return cls(**asdict(result))


class IngestResponse(BaseModel):
    documents: list[IngestedDocumentModel]
    total_chunks: int
    errors: list[str] = Field(default_factory=list)


class DocumentModel(BaseModel):
    doc_id: str
    filename: str
    pages: int
    blocks: int
    chunks: int
    tokens: int
    sha256: str
    ingested_at: datetime
    chunk_size: int
    chunk_overlap: int
    embedding_model: str

    @classmethod
    def from_record(cls, record: DocumentRecord) -> "DocumentModel":
        return cls(**record.to_dict())


class DocumentsResponse(BaseModel):
    documents: list[DocumentModel]
    count: int
    total_chunks: int


class HealthResponse(BaseModel):
    status: str
    documents: int
    chunks: int
    chunking: dict[str, Any]
    embeddings: dict[str, Any]
    vector_store: dict[str, Any]
    retrieval: dict[str, Any]
    generation: dict[str, Any]
    claim_summaries: dict[str, Any]
