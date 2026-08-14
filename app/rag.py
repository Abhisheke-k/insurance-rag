"""The RAG pipeline: ingestion, retrieval and the /ask orchestration.

This is the only module that knows about all the pieces at once, which keeps
parsing, chunking, embedding, storage and generation independently testable.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from app.chunking import StructureAwareChunker, build_chunker
from app.config import Settings
from app.embeddings import Embedder, build_embedder
from app.generation import AnswerGenerator, not_found_answer
from app.models import Chunk, DocumentRecord, GeneratedAnswer, RetrievedChunk
from app.parsing import parse_pdf_bytes
from app.registry import DocumentRegistry
from app.store import VectorStore, build_vector_store
from app.tokenizer import estimate_tokens

logger = logging.getLogger(__name__)

__all__ = ["AskOutcome", "IngestResult", "RagService"]


@dataclass(frozen=True, slots=True)
class IngestResult:
    """What a single ingested PDF produced."""

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


@dataclass(frozen=True, slots=True)
class AskOutcome:
    """A generated answer plus the retrieval evidence behind it."""

    answer: GeneratedAnswer
    retrieved: list[RetrievedChunk] = field(default_factory=list)
    best_score: float | None = None
    gated: bool = False


class RagService:
    """Wires the pipeline together and owns process-level state."""

    def __init__(
        self,
        settings: Settings,
        *,
        embedder: Embedder | None = None,
        store: VectorStore | None = None,
        registry: DocumentRegistry | None = None,
        generator: AnswerGenerator | None = None,
        chunker: StructureAwareChunker | None = None,
    ) -> None:
        self.settings = settings
        self.embedder = embedder or build_embedder(settings)
        self.store = store or build_vector_store(settings)
        self.registry = registry or DocumentRegistry(settings.registry_path)
        self.generator = generator or AnswerGenerator(settings)
        self.chunker = chunker or build_chunker(settings)

    # ------------------------------------------------------------------ #
    # Ingestion
    # ------------------------------------------------------------------ #
    @staticmethod
    def compute_doc_id(data: bytes) -> tuple[str, str]:
        """Content-addressed document id, so re-uploading a file is idempotent."""
        digest = hashlib.sha256(data).hexdigest()
        return digest[:16], digest

    def _embedding_text(self, chunk: Chunk) -> str:
        """Text actually embedded.

        The section breadcrumb is prepended to the vector input (never to the
        stored/cited text) so that a question phrased in the language of a
        heading -- "what does the flood endorsement exclude?" -- can match a
        clause whose body never repeats the heading.
        """
        if chunk.section_path:
            return f"{chunk.section_path}\n{chunk.text}"
        return chunk.text

    def ingest(self, data: bytes, filename: str) -> IngestResult:
        """Parse, chunk, embed and store one PDF."""
        if not data:
            raise ValueError("empty file")

        doc_id, sha256 = self.compute_doc_id(data)
        replaced = self.registry.get(doc_id) is not None
        if replaced:
            # Same bytes uploaded again: drop the old vectors so re-ingestion at
            # new CHUNK_SIZE/CHUNK_OVERLAP settings does not double up.
            logger.info("re-ingesting %s (doc_id=%s); removing previous chunks", filename, doc_id)
            self.store.delete_document(doc_id)

        parsed = parse_pdf_bytes(data, filename=filename, doc_id=doc_id)
        if not parsed.blocks:
            raise ValueError(
                f"no extractable text found in {filename!r} -- is it a scanned PDF? "
                "OCR is out of scope for this service."
            )

        chunks = self.chunker.chunk(parsed.blocks)
        if not chunks:
            raise ValueError(f"{filename!r} produced no chunks")

        vectors = self.embedder.embed_documents([self._embedding_text(chunk) for chunk in chunks])
        self.store.add_chunks(chunks, vectors)

        record = DocumentRecord(
            doc_id=doc_id,
            filename=filename,
            pages=parsed.page_count,
            blocks=len(parsed.blocks),
            chunks=len(chunks),
            tokens=sum(chunk.tokens for chunk in chunks),
            sha256=sha256,
            ingested_at=datetime.now(timezone.utc),
            chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
            embedding_model=self.embedder.name,
        )
        self.registry.upsert(record)

        logger.info(
            "ingested %s: %d pages, %d blocks, %d chunks (chunk_size=%d overlap=%d)",
            filename,
            parsed.page_count,
            len(parsed.blocks),
            len(chunks),
            self.settings.chunk_size,
            self.settings.chunk_overlap,
        )
        return IngestResult(
            doc_id=doc_id,
            filename=filename,
            pages=parsed.page_count,
            blocks=len(parsed.blocks),
            chunks=len(chunks),
            tokens=record.tokens,
            replaced=replaced,
            chunk_size=self.settings.chunk_size,
            chunk_overlap=self.settings.chunk_overlap,
            embedding_model=self.embedder.name,
        )

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    def retrieve(self, question: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """Embed the question and return the closest chunks, best first."""
        k = top_k or self.settings.top_k
        vector = self.embedder.embed_query(question)
        return self.store.query(vector, k)

    # ------------------------------------------------------------------ #
    # Ask
    # ------------------------------------------------------------------ #
    def ask(self, question: str, top_k: int | None = None) -> AskOutcome:
        """Retrieve, gate on relevance, then generate a cited answer."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")

        retrieved = self.retrieve(question, top_k)
        if not retrieved:
            return AskOutcome(
                answer=not_found_answer(notes=["no documents have been ingested yet"]),
                retrieved=[],
                best_score=None,
                gated=True,
            )

        best_score = retrieved[0].score
        if best_score < self.settings.min_relevance:
            # Nothing in the corpus is close enough to be worth an LLM call.
            # Answering here would mean asking the model to reason over noise.
            logger.info(
                "relevance gate: best score %.3f < %.3f; returning not-found",
                best_score,
                self.settings.min_relevance,
            )
            return AskOutcome(
                answer=not_found_answer(
                    chunks_used=0,
                    notes=[
                        f"best similarity {best_score:.3f} below MIN_RELEVANCE "
                        f"{self.settings.min_relevance:.3f}"
                    ],
                ),
                retrieved=retrieved,
                best_score=best_score,
                gated=True,
            )

        answer = self.generator.generate(question, retrieved)
        return AskOutcome(answer=answer, retrieved=retrieved, best_score=best_score, gated=False)

    # ------------------------------------------------------------------ #
    # Documents / health
    # ------------------------------------------------------------------ #
    def documents(self) -> list[DocumentRecord]:
        return self.registry.list()

    def delete_document(self, doc_id: str) -> bool:
        self.store.delete_document(doc_id)
        return self.registry.remove(doc_id)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "documents": len(self.registry),
            "chunks": self.store.count(),
            "chunking": {
                "chunk_size": self.settings.chunk_size,
                "chunk_overlap": self.settings.chunk_overlap,
                "min_chunk_size": self.settings.min_chunk_size,
                "token_counter": "estimate (offline heuristic)",
            },
            "embeddings": {
                "provider": self.settings.embedding_provider,
                "model": self.embedder.name,
                "dimension": self.embedder.dimension,
            },
            "vector_store": self.store.describe(),
            "retrieval": {
                "top_k": self.settings.top_k,
                "min_relevance": self.settings.min_relevance,
            },
            "generation": self.generator.status(),
        }


def token_estimate(text: str) -> int:
    """Re-exported for scripts that want the same counter the chunker uses."""
    return estimate_tokens(text)


def summarise_scores(retrieved: Sequence[RetrievedChunk]) -> list[dict[str, Any]]:
    """Compact retrieval trace, handy for debugging and the comparison script."""
    return [
        {
            "rank": index,
            "chunk_id": chunk.chunk_id,
            "score": round(chunk.score, 4),
            "page": chunk.page,
            "section": chunk.section,
        }
        for index, chunk in enumerate(retrieved, start=1)
    ]
