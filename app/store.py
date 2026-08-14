"""Vector storage.

ChromaDB persisted to ``./chroma_db`` is the production path. A small
in-process store implements the same interface so the test suite (and anyone
running without a writable Chroma directory) gets identical retrieval
behaviour with zero setup -- both use cosine similarity over the same vectors.

Scores are always returned as **cosine similarity in [-1, 1]**, never as a raw
distance, because the relevance gate in :mod:`app.rag` is expressed as a
similarity floor.
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, Sequence, runtime_checkable

from app.config import Settings
from app.models import Chunk, RetrievedChunk

logger = logging.getLogger(__name__)

__all__ = [
    "ChromaVectorStore",
    "InMemoryVectorStore",
    "VectorStore",
    "build_vector_store",
]


@runtime_checkable
class VectorStore(Protocol):
    """Minimal surface the RAG pipeline needs from a vector database."""

    backend: str

    def add_chunks(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None: ...

    def delete_document(self, doc_id: str) -> None: ...

    def query(self, embedding: Sequence[float], top_k: int) -> list[RetrievedChunk]: ...

    def count(self) -> int: ...

    def describe(self) -> dict[str, Any]: ...


def _to_retrieved(metadata: dict[str, Any], text: str, score: float) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=str(metadata.get("chunk_id", "")),
        doc_id=str(metadata.get("doc_id", "")),
        filename=str(metadata.get("filename", "")),
        text=text,
        page=int(metadata.get("page", 0) or 0),
        page_end=int(metadata.get("page_end", metadata.get("page", 0)) or 0),
        section_path=str(metadata.get("section_path", "")),
        clause_label=str(metadata.get("clause_label", "")) or None,
        score=score,
    )


class ChromaVectorStore:
    """Persistent ChromaDB collection. Embeddings are always supplied by us."""

    backend = "chromadb"

    def __init__(self, *, persist_dir, collection_name: str) -> None:
        import chromadb  # noqa: PLC0415 -- heavy import, kept out of module import time
        from chromadb.config import Settings as ChromaSettings  # noqa: PLC0415

        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(
            path=str(persist_dir),
            settings=ChromaSettings(anonymized_telemetry=False, allow_reset=True),
        )
        self._collection_name = collection_name
        self._collection = self._get_or_create(collection_name)
        self.persist_dir = persist_dir

    def _get_or_create(self, name: str):
        """Create the collection with cosine space, tolerating API drift.

        Chroma moved the HNSW space setting from ``metadata`` to
        ``configuration`` across releases; try the newer shape first.
        """
        attempts = (
            {"configuration": {"hnsw": {"space": "cosine"}}},
            {"metadata": {"hnsw:space": "cosine"}},
            {},
        )
        last_error: Exception | None = None
        for extra in attempts:
            try:
                return self._client.get_or_create_collection(
                    name=name, embedding_function=None, **extra
                )
            except Exception as exc:  # pragma: no cover - depends on chroma version
                last_error = exc
                logger.debug("collection creation with %s failed: %s", extra, exc)
        raise RuntimeError(f"could not open Chroma collection {name!r}") from last_error

    def add_chunks(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None:
        if not chunks:
            return
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must be the same length")

        self._collection.upsert(
            ids=[chunk.chunk_id for chunk in chunks],
            documents=[chunk.text for chunk in chunks],
            metadatas=[chunk.to_metadata() for chunk in chunks],
            embeddings=[list(vector) for vector in embeddings],
        )

    def delete_document(self, doc_id: str) -> None:
        self._collection.delete(where={"doc_id": doc_id})

    def query(self, embedding: Sequence[float], top_k: int) -> list[RetrievedChunk]:
        if self.count() == 0:
            return []

        result = self._collection.query(
            query_embeddings=[list(embedding)],
            n_results=min(top_k, self.count()),
            include=["documents", "metadatas", "distances"],
        )
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]

        retrieved: list[RetrievedChunk] = []
        for text, metadata, distance in zip(documents, metadatas, distances):
            retrieved.append(_to_retrieved(dict(metadata or {}), text or "", 1.0 - float(distance)))
        return retrieved

    def count(self) -> int:
        return int(self._collection.count())

    def reset(self) -> None:
        """Drop and recreate the collection (used by re-ingestion tooling)."""
        self._client.delete_collection(self._collection_name)
        self._collection = self._get_or_create(self._collection_name)

    def describe(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "collection": self._collection_name,
            "path": str(self.persist_dir),
            "chunks": self.count(),
        }


class InMemoryVectorStore:
    """Exact cosine search over in-process vectors. No persistence."""

    backend = "in-memory"

    def __init__(self) -> None:
        self._chunks: dict[str, Chunk] = {}
        self._vectors: dict[str, list[float]] = {}

    def add_chunks(self, chunks: Sequence[Chunk], embeddings: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(embeddings):
            raise ValueError("chunks and embeddings must be the same length")
        for chunk, vector in zip(chunks, embeddings):
            self._chunks[chunk.chunk_id] = chunk
            self._vectors[chunk.chunk_id] = list(vector)

    def delete_document(self, doc_id: str) -> None:
        for chunk_id in [cid for cid, chunk in self._chunks.items() if chunk.doc_id == doc_id]:
            self._chunks.pop(chunk_id, None)
            self._vectors.pop(chunk_id, None)

    def query(self, embedding: Sequence[float], top_k: int) -> list[RetrievedChunk]:
        import numpy as np  # noqa: PLC0415

        if not self._vectors:
            return []

        query_vector = np.asarray(embedding, dtype=float)
        query_norm = float(np.linalg.norm(query_vector)) or 1.0

        scored: list[tuple[float, str]] = []
        for chunk_id, vector in self._vectors.items():
            candidate = np.asarray(vector, dtype=float)
            norm = float(np.linalg.norm(candidate)) or 1.0
            scored.append((float(query_vector @ candidate) / (query_norm * norm), chunk_id))

        scored.sort(key=lambda item: item[0], reverse=True)
        results: list[RetrievedChunk] = []
        for score, chunk_id in scored[:top_k]:
            chunk = self._chunks[chunk_id]
            results.append(_to_retrieved(chunk.to_metadata(), chunk.text, score))
        return results

    def count(self) -> int:
        return len(self._chunks)

    def describe(self) -> dict[str, Any]:
        return {"backend": self.backend, "collection": "memory", "chunks": self.count()}


def build_vector_store(settings: Settings) -> VectorStore:
    """Open the configured store, degrading to in-memory if Chroma cannot start."""
    if settings.force_in_memory_store:
        return InMemoryVectorStore()

    try:
        return ChromaVectorStore(
            persist_dir=settings.chroma_dir,
            collection_name=settings.collection_name,
        )
    except Exception as exc:  # pragma: no cover - missing package / unwritable dir
        logger.warning("ChromaDB unavailable (%s); using the in-memory store instead", exc)
        return InMemoryVectorStore()
