"""Shared fixtures.

The suite is hermetic: it uses the deterministic hashing embedder and the
in-memory vector store, so it needs no API key, no model download and no
writable Chroma directory. Tests that genuinely need Claude are marked and skip
themselves when ``ANTHROPIC_API_KEY`` is absent.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Sequence

import pytest

from app.chunking import StructureAwareChunker
from app.config import Settings
from app.generation import NOT_FOUND_MESSAGE, not_found_answer
from app.models import Block, Chunk, Citation, GeneratedAnswer, RetrievedChunk
from app.parsing import ParsedDocument, parse_pdf_file
from app.rag import RagService
from app.registry import DocumentRegistry
from scripts.make_sample_pdf import EVAL_QUESTIONS, OUT_OF_SCOPE_QUESTION, build_sample_pdf

CHUNK_SIZE = 550
CHUNK_OVERLAP = 120


class StubGenerator:
    """Records what the pipeline would have sent to Claude, and answers offline.

    By default it "answers" using the first retrieved passage, which is enough
    to exercise the plumbing. Tests override :attr:`response` to simulate a
    model that fabricates citations or returns nothing.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[RetrievedChunk]]] = []
        self.response: GeneratedAnswer | None = None

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def status(self) -> dict[str, object]:
        return {"configured": True, "model": "stub", "effort": "n/a", "error": None}

    def generate(self, question: str, retrieved: Sequence[RetrievedChunk]) -> GeneratedAnswer:
        self.calls.append((question, list(retrieved)))
        if self.response is not None:
            return self.response
        if not retrieved:
            return not_found_answer()

        top = retrieved[0]
        return GeneratedAnswer(
            answer=f"(stub) {top.text[:80]}",
            found=True,
            citations=[
                Citation(
                    filename=top.filename,
                    page=top.page,
                    section=top.section,
                    chunk_id=top.chunk_id,
                    score=top.score,
                    snippet=top.text[:200],
                )
            ],
            chunks_used=len(retrieved),
            model="stub",
        )


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The synthetic endorsement pack, generated once per test session."""
    directory = tmp_path_factory.mktemp("sample")
    return build_sample_pdf(directory / "sample_endorsement_pack.pdf")


@pytest.fixture(scope="session")
def sample_pdf_bytes(sample_pdf: Path) -> bytes:
    return sample_pdf.read_bytes()


@pytest.fixture(scope="session")
def parsed_document(sample_pdf: Path) -> ParsedDocument:
    return parse_pdf_file(sample_pdf, doc_id="sample")


@pytest.fixture(scope="session")
def blocks(parsed_document: ParsedDocument) -> list[Block]:
    return parsed_document.blocks


@pytest.fixture
def chunker() -> StructureAwareChunker:
    return StructureAwareChunker(
        chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP, min_chunk_size=100
    )


@pytest.fixture
def chunks(chunker: StructureAwareChunker, blocks: list[Block]) -> list[Chunk]:
    return chunker.chunk(blocks)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Hermetic settings: deterministic embeddings, no external services."""
    return Settings(
        chunk_size=CHUNK_SIZE,
        chunk_overlap=CHUNK_OVERLAP,
        min_chunk_size=100,
        embedding_provider="hashing",
        force_in_memory_store=True,
        chroma_dir=tmp_path / "store",
        top_k=5,
        # Measured separation for the hashing embedder on the sample pack:
        # in-scope questions score 0.13-0.36, out-of-scope 0.06-0.10.
        min_relevance=0.12,
        anthropic_api_key=None,
    )


@pytest.fixture
def stub_generator() -> StubGenerator:
    return StubGenerator()


@pytest.fixture
def service(settings: Settings, stub_generator: StubGenerator) -> RagService:
    return RagService(
        settings,
        registry=DocumentRegistry(settings.registry_path),
        generator=stub_generator,  # type: ignore[arg-type]
    )


@pytest.fixture
def ingested_service(service: RagService, sample_pdf_bytes: bytes) -> RagService:
    service.ingest(sample_pdf_bytes, "sample_endorsement_pack.pdf")
    return service


__all__ = [
    "CHUNK_OVERLAP",
    "CHUNK_SIZE",
    "EVAL_QUESTIONS",
    "NOT_FOUND_MESSAGE",
    "OUT_OF_SCOPE_QUESTION",
    "StubGenerator",
    "replace",
]
