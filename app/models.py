"""Internal domain objects shared by parsing, chunking, retrieval and generation.

These are plain frozen dataclasses rather than Pydantic models: they are never
parsed from untrusted input, they are created in hot loops during ingestion, and
keeping them dependency-free makes the chunker unit-testable on its own. The
HTTP-facing shapes live in :mod:`app.schemas`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

SECTION_PATH_SEPARATOR = " > "

BlockKind = Literal["heading", "body"]


@dataclass(frozen=True, slots=True)
class Block:
    """One logical paragraph (or heading line) extracted from a PDF page.

    ``section_path`` is the structural breadcrumb the block sits under, e.g.
    ``("ENDORSEMENT NO. 3", "SECTION 4 - PROPERTY DAMAGE", "4.2 Exclusions")``.
    It is the unit the chunker groups on, so it never contains sub-clause
    markers such as ``(a)`` -- those stay inside the text and are used only as
    fallback split points.
    """

    text: str
    page: int
    doc_id: str
    filename: str
    section_path: tuple[str, ...]
    clause_label: str | None
    kind: BlockKind = "body"

    @property
    def section_path_str(self) -> str:
        return SECTION_PATH_SEPARATOR.join(self.section_path)


@dataclass(frozen=True, slots=True)
class Piece:
    """A splittable span of text carrying the page range it came from.

    A piece starts life as a whole clause. Only if a clause is larger than the
    chunk target is it broken down into smaller pieces (paragraph, sub-clause,
    sentence, then a hard word window) -- which is what keeps small clauses
    intact inside a single chunk.
    """

    text: str
    tokens: int
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    clause_label: str | None
    #: True once the piece is the result of splitting an oversized clause.
    is_partial: bool = False
    #: True when the piece is repeated text carried over as chunk overlap.
    is_overlap: bool = False


@dataclass(frozen=True, slots=True)
class Chunk:
    """An embeddable unit of text plus the metadata every citation is built from."""

    chunk_id: str
    doc_id: str
    filename: str
    text: str
    tokens: int
    page: int
    page_end: int
    section_path: str
    clause_label: str | None
    chunk_index: int
    #: True when this chunk is one slice of a clause too large to keep whole.
    is_partial_clause: bool = False

    def to_metadata(self) -> dict[str, Any]:
        """Flat, scalar-only metadata (ChromaDB rejects nested values)."""
        return {
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "filename": self.filename,
            "page": self.page,
            "page_end": self.page_end,
            "section_path": self.section_path,
            "clause_label": self.clause_label or "",
            "chunk_index": self.chunk_index,
            "tokens": self.tokens,
            "is_partial_clause": self.is_partial_clause,
        }


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    """A chunk returned by the vector store, with its similarity score."""

    chunk_id: str
    doc_id: str
    filename: str
    text: str
    page: int
    page_end: int
    section_path: str
    clause_label: str | None
    score: float

    @property
    def section(self) -> str:
        """Human-readable section label used in citations."""
        return self.section_path or self.clause_label or "(unlabelled)"


@dataclass(frozen=True, slots=True)
class Citation:
    """A resolved pointer from a sentence in the answer back to a stored chunk."""

    filename: str
    page: int
    section: str
    chunk_id: str
    score: float
    snippet: str


@dataclass(frozen=True, slots=True)
class GeneratedAnswer:
    """What the generation layer returns before it is shaped into HTTP JSON."""

    answer: str
    found: bool
    citations: list[Citation]
    chunks_used: int
    model: str | None = None
    stop_reason: str | None = None
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class DocumentRecord:
    """One ingested PDF, as reported by ``GET /documents``."""

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

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "doc_id": self.doc_id,
            "filename": self.filename,
            "pages": self.pages,
            "blocks": self.blocks,
            "chunks": self.chunks,
            "tokens": self.tokens,
            "sha256": self.sha256,
            "ingested_at": self.ingested_at.isoformat(),
            "chunk_size": self.chunk_size,
            "chunk_overlap": self.chunk_overlap,
            "embedding_model": self.embedding_model,
        }
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "DocumentRecord":
        return cls(
            doc_id=payload["doc_id"],
            filename=payload["filename"],
            pages=int(payload["pages"]),
            blocks=int(payload.get("blocks", 0)),
            chunks=int(payload["chunks"]),
            tokens=int(payload.get("tokens", 0)),
            sha256=payload.get("sha256", ""),
            ingested_at=datetime.fromisoformat(payload["ingested_at"]),
            chunk_size=int(payload.get("chunk_size", 0)),
            chunk_overlap=int(payload.get("chunk_overlap", 0)),
            embedding_model=payload.get("embedding_model", ""),
        )
