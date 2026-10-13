"""The RAG pipeline: ingestion, retrieval and the /ask orchestration.

This is the only module that knows about all the pieces at once, which keeps
parsing, chunking, embedding, storage and generation independently testable.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from collections.abc import Collection
from datetime import date, datetime, timezone
from typing import Any, Sequence

from app.agent import AgentClaimResult, ClaimsAgent
from app.chunking import StructureAwareChunker, build_chunker
from app.clause_stem import carry_clause_stems
from app.claims import ClaimSummary, ClaimSummaryGenerator
from app.config import Settings
from app.embeddings import Embedder, build_embedder
from app.generation import AnswerGenerator, not_found_answer
from app.hybrid_retrieval import BM25Index, hybrid_retrieve
from app.models import Chunk, DocumentRecord, GeneratedAnswer, RetrievedChunk
from app.observability import JsonlSink, Tracer, span
from app.parsing import parse_pdf_bytes
from app.registry import DocumentRegistry
from app.store import VectorStore, build_vector_store
from app.suspicion import suspect_flags
from app.tokenizer import estimate_tokens
from app.versions import VersionIndex

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


@dataclass(frozen=True, slots=True)
class ClaimSummaryOutcome:
    """A claim summary plus the policy/endorsement evidence behind it."""

    summary: ClaimSummary
    retrieved: list[RetrievedChunk] = field(default_factory=list)


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
        claim_generator: ClaimSummaryGenerator | None = None,
        claims_agent: ClaimsAgent | None = None,
    ) -> None:
        self.settings = settings
        self.tracer = Tracer(
            JsonlSink(settings.trace_log_path) if settings.trace_log_path else None,
            price_model=settings.answer_model,
        )
        self.embedder = embedder or build_embedder(settings)
        self.store = store or build_vector_store(settings)
        self.registry = registry or DocumentRegistry(settings.registry_path)
        self.generator = generator or AnswerGenerator(settings)
        self.chunker = chunker or build_chunker(settings)
        self.claim_generator = claim_generator or ClaimSummaryGenerator(settings)
        self.claims_agent = claims_agent or ClaimsAgent(settings, searcher=self)
        self._bm25_index: BM25Index | None = None
        self._bm25_dirty = True
        self._chunk_lookup: dict[tuple[str, int], RetrievedChunk] | None = None
        self._version_index: VersionIndex | None = None

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
        self._bm25_dirty = True
        self._chunk_lookup = None
        self._version_index = None

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
    def _ensure_bm25_index(self) -> BM25Index:
        if self._bm25_index is None or self._bm25_dirty:
            self._bm25_index = BM25Index()
            self._bm25_index.build(self.store.all_chunks())
            self._bm25_dirty = False
        return self._bm25_index

    def retrieve(self, question: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """Return the closest chunks to ``question``, best first -- as a ``retrieval`` span.

        The span records the context ids and scores (never the query text): enough to see
        exactly which wording an answer was built from without logging what was asked.
        """
        k = top_k or self.settings.top_k
        with span(
            "retrieve",
            "retrieval",
            mode=self.settings.retrieval_mode,
            top_k=k,
            query_digest=hashlib.sha256(question.encode("utf-8")).hexdigest()[:12],
        ) as current:
            hits = self._retrieve(question, k)
            carried = 0
            if self.settings.carry_clause_stem:
                hits, carried = carry_clause_stems(hits, self._lookup_chunk)
            if current is not None:
                current.set(
                    n=len(hits),
                    context_ids=[hit.chunk_id for hit in hits],
                    scores=[round(hit.score, 4) for hit in hits],
                    carry_clause_stem=self.settings.carry_clause_stem,
                    stems_carried=carried,
                )
            return hits

    def _ensure_version_index(self) -> VersionIndex:
        if self._version_index is None:
            self._version_index = VersionIndex.build(self.store.all_chunks())
        return self._version_index

    def retrieve_as_of(
        self,
        question: str,
        top_k: int | None = None,
        as_of: date | None = None,
        documents: Collection[str] | None = None,
    ) -> list[RetrievedChunk]:
        """Retrieval that only offers wording in force on ``as_of`` (the loss date) -- the single entry point for coverage questions.

        The date filter is enforced here, in code, not requested in a prompt: a candidate pool of ``VERSION_POOL_FACTOR * k``
        is fetched, each candidate is narrowed to the part of it in force on the loss date (a chunk holding two editions keeps
        only one), what is left is tagged with its edition and effective interval, and the best ``k`` are kept. With no loss
        date, or ``VERSION_FILTER=false``, this is plain :meth:`retrieve`. ``documents`` scopes the search to the files a policy
        was issued on (filtering the pool *after* retrieval, so a corpus of many policies would need a store-side filter instead).
        """
        k = top_k or self.settings.top_k
        scope = set(documents) if documents is not None else None
        if as_of is None or not self.settings.version_filter:
            if scope is None:
                return self.retrieve(question, k)
            wide = self.retrieve(question, k * self.settings.version_pool_factor)
            return [hit for hit in wide if hit.filename in scope][:k]
        with span(
            "retrieve",
            "retrieval",
            mode=self.settings.retrieval_mode,
            top_k=k,
            query_digest=hashlib.sha256(question.encode("utf-8")).hexdigest()[:12],
            as_of=as_of.isoformat(),
            version_filter=True,
        ) as current:
            pool = self._retrieve(question, k * self.settings.version_pool_factor)
            carried = 0
            if self.settings.carry_clause_stem:
                pool, carried = carry_clause_stems(pool, self._lookup_chunk)
            index = self._ensure_version_index()
            kept: list[RetrievedChunk] = []
            dropped = 0
            for hit in pool:
                if scope is not None and hit.filename not in scope:
                    continue
                narrowed, _ = index.resolve(hit, as_of)
                if narrowed is None:
                    dropped += 1
                    continue
                kept.append(narrowed)
                if len(kept) == k:
                    break
            if current is not None:
                current.set(
                    n=len(kept),
                    pool=len(pool),
                    out_of_force_dropped=dropped,
                    context_ids=[hit.chunk_id for hit in kept],
                    scores=[round(hit.score, 4) for hit in kept],
                    editions=sorted({tag.edition for hit in kept for tag in hit.versions if tag.edition}),
                    carry_clause_stem=self.settings.carry_clause_stem,
                    stems_carried=carried,
                )
            return kept

    def passage(self, chunk_id: str, as_of: date | None = None) -> tuple[RetrievedChunk, RetrievedChunk | None] | None:
        """The stored chunk behind a citation, and (given a loss date) the part of it in force that day.

        ``None`` means no such chunk. A chunk that exists but has nothing in force on ``as_of`` returns ``(chunk, None)``:
        that is the answer to "does this citation support a loss on that date?".
        """
        chunk = next((c for c in self.store.all_chunks() if c.chunk_id == chunk_id), None)
        if chunk is None:
            return None
        if as_of is None:
            return chunk, chunk
        narrowed, _ = self._ensure_version_index().resolve(chunk, as_of)
        return chunk, narrowed

    def _lookup_chunk(self, doc_id: str, chunk_index: int) -> RetrievedChunk | None:
        """The stored chunk at ``(doc_id, chunk_index)`` -- how a continuation finds the chunk before it."""
        if self._chunk_lookup is None:
            self._chunk_lookup = {(c.doc_id, c.chunk_index): c for c in self.store.all_chunks()}
        return self._chunk_lookup.get((doc_id, chunk_index))

    def _retrieve(self, question: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """The retrieval itself.

        Two modes, selected by ``RETRIEVAL_MODE`` (see app/config.py and the
        Week 4 writeup in coursework/w4/): 'dense' is cosine search over the
        embedder alone (the original behaviour); 'hybrid_bm25_rrf' additionally
        searches a BM25 index and fuses the two rankings, which is materially
        better at exact identifiers (exclusion codes, form numbers) that dense
        embeddings blur. MMR diversification runs after fusion when
        MMR_LAMBDA is set.
        """
        k = top_k or self.settings.top_k
        if self.settings.retrieval_mode == "hybrid_bm25_rrf":
            result = hybrid_retrieve(
                question,
                embedder=self.embedder,
                store=self.store,
                bm25_index=self._ensure_bm25_index(),
                top_k=k,
                candidate_pool=self.settings.bm25_candidates,
                rrf_k=self.settings.rrf_k,
                mmr_lambda=self.settings.mmr_lambda,
            )
            return result.hits

        vector = self.embedder.embed_query(question)
        return self.store.query(vector, k)

    # ------------------------------------------------------------------ #
    # Ask
    # ------------------------------------------------------------------ #
    def ask(
        self, question: str, top_k: int | None = None, *, user: str | None = None, input_type: str = "policy_question"
    ) -> AskOutcome:
        """Retrieve, gate on relevance, then generate a cited answer."""
        question = question.strip()
        if not question:
            raise ValueError("question must not be empty")
        with self.tracer.trace("ask", input_text=question, user=user, input_type=input_type) as active:
            outcome = self._ask(question, top_k)
            active.set_output(
                found=outcome.answer.found,
                gated=outcome.gated,
                best_score=None if outcome.best_score is None else round(outcome.best_score, 4),
                citations=[c.chunk_id for c in outcome.answer.citations],
                summary=outcome.answer.answer,
            )
            return outcome

    def _ask(self, question: str, top_k: int | None) -> AskOutcome:
        retrieved = self.retrieve(question, top_k)
        if not retrieved:
            return AskOutcome(
                answer=not_found_answer(notes=["no documents have been ingested yet"]),
                retrieved=[],
                best_score=None,
                gated=True,
            )

        best_score = retrieved[0].score
        # Hybrid mode's scores are fused ranks, not cosines: the cosine-calibrated floor does not apply to them.
        floor = self.settings.min_relevance if self.settings.retrieval_mode == "dense" else self.settings.min_relevance_hybrid
        if best_score < floor:
            # Nothing in the corpus is close enough to be worth an LLM call.
            # Answering here would mean asking the model to reason over noise.
            logger.info(
                "relevance gate: best score %.3f < %.3f; returning not-found",
                best_score,
                floor,
            )
            return AskOutcome(
                answer=not_found_answer(
                    chunks_used=0,
                    notes=[
                        f"best similarity {best_score:.3f} below the relevance floor "
                        f"{floor:.3f} ({self.settings.retrieval_mode})"
                    ],
                ),
                retrieved=retrieved,
                best_score=best_score,
                gated=True,
            )

        answer = self.generator.generate(question, retrieved)
        return AskOutcome(answer=answer, retrieved=retrieved, best_score=best_score, gated=False)

    # ------------------------------------------------------------------ #
    # Claim summaries
    # ------------------------------------------------------------------ #
    def summarize_claim(
        self, adjuster_notes: str, top_k: int | None = None, *, user: str | None = None, input_type: str = "fnol_note"
    ) -> ClaimSummaryOutcome:
        """Retrieve policy/endorsement context for ``adjuster_notes`` and summarise coverage.

        No relevance gate here, unlike ``ask()``: a claim always deserves a
        summary, even a ``needs_review`` one when nothing in the corpus
        clearly applies -- silently refusing is not an option a claims desk
        can work with the way it is for open-ended Q&A.
        """
        adjuster_notes = adjuster_notes.strip()
        if not adjuster_notes:
            raise ValueError("adjuster notes must not be empty")

        with self.tracer.trace("claim_summary", input_text=adjuster_notes, user=user, input_type=input_type) as active:
            retrieved = self.retrieve(adjuster_notes, top_k or self.settings.claim_top_k)
            summary = self.claim_generator.summarize(adjuster_notes, retrieved)
            self._record_summary(active, summary)
            return ClaimSummaryOutcome(summary=summary, retrieved=retrieved)

    def _record_summary(self, active: Any, summary: ClaimSummary) -> None:
        """Put the fields a complaint can be traced by on the trace's top level."""
        claim_ref = None
        if re.fullmatch(r"CLM-\d{4}-\d{5}", summary.claim_number or ""):
            claim_ref = self.tracer.redactor.pseudonym("claim_number", summary.claim_number)
        active.set_output(
            claim_ref=claim_ref,
            decision=summary.coverage_decision,
            cited_exclusion_id=summary.cited_exclusion_id,
            excess_amount=summary.excess_amount,
            citations=[c.chunk_id for c in summary.citations],
            summary=summary.summary,
            notes=summary.notes,
            suspect=suspect_flags(summary.coverage_decision, summary.summary, [c.chunk_id for c in summary.citations]),
        )

    def process_claim_with_agent(
        self,
        adjuster_notes: str,
        top_k: int | None = None,
        *,
        user: str | None = None,
        input_type: str = "fnol_note",
    ) -> AgentClaimResult:
        """The Week 7 multi-step counterpart to :meth:`summarize_claim`.

        Same eventual output shape (a :class:`~app.claims.ClaimSummary`), but
        reached by a think/act/observe loop that searches and self-checks
        rather than one retrieve-then-generate call. See :mod:`app.agent`.
        """
        if not adjuster_notes.strip():
            return self.claims_agent.process(adjuster_notes, top_k)  # raises the agent's own ValueError
        with self.tracer.trace("claims_agent", input_text=adjuster_notes, user=user, input_type=input_type) as active:
            result = self.claims_agent.process(adjuster_notes, top_k)
            self._record_summary(active, result.summary)
            active.set_output(stopped_reason=result.stopped_reason, steps=[step.action for step in result.steps])
            return result

    # ------------------------------------------------------------------ #
    # Documents / health
    # ------------------------------------------------------------------ #
    def documents(self) -> list[DocumentRecord]:
        return self.registry.list()

    def delete_document(self, doc_id: str) -> bool:
        self.store.delete_document(doc_id)
        self._bm25_dirty = True
        self._chunk_lookup = None
        self._version_index = None
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
                "min_relevance_hybrid": self.settings.min_relevance_hybrid,
                "mode": self.settings.retrieval_mode,
                "rrf_k": self.settings.rrf_k if self.settings.retrieval_mode == "hybrid_bm25_rrf" else None,
                "mmr_lambda": self.settings.mmr_lambda,
            },
            "generation": self.generator.status(),
            "claim_summaries": self.claim_generator.status(),
            "claims_agent": self.claims_agent.status(),
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
