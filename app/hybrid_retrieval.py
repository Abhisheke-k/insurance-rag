"""BM25 + Reciprocal Rank Fusion hybrid retrieval, and an optional MMR pass.

**Week 4's retrieval change.** The app's default retriever is cosine search
over an embedder -- dense-only. A dense embedding is a real number, and a real
number cannot preserve an arbitrary discrete identifier: "E-17", "HO-0304 ed.
03-24" and "END-2024-0417" all get projected into whichever semantic
neighbourhood the surrounding prose puts them near, which is exactly why the
sample corpus's three near-duplicate exclusion editions (see
``scripts/make_sample_pdf.py``) are so easily confused by a dense-only
retriever. BM25 has the opposite failure mode: pure token overlap, no semantic
generalisation, and it does not care that "flood" and "water intrusion" mean
almost the same thing -- but it will never confuse "60 consecutive days" with
"45 consecutive days" because those are different tokens.

Fusing the two RANKINGS with Reciprocal Rank Fusion (Cormack, Clarke & Buettcher
2009) rather than blending their SCORES is deliberate: a cosine similarity and
a BM25 score are not on the same scale and were never meant to be added or
averaged. RRF only looks at where each ranker placed a document:

    RRF(d) = sum over rankers r that returned d of  1 / (k + rank_r(d))

``k=60`` is the paper's own default and is not tuned per corpus -- using ranks
instead of scores is what makes the fusion constant portable in the first
place.

MMR (Carbonell & Goldstein 1998) is a separate, optional pass applied *after*
fusion, for the Week 4 bonus challenge: it re-orders the fused candidate list
to trade relevance for diversity, controlled by ``lambda`` (1.0 = pure
relevance, 0.0 = pure diversity).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

from app.models import RetrievedChunk

if TYPE_CHECKING:
    from app.embeddings import Embedder
    from app.store import VectorStore

__all__ = ["BM25Index", "HybridRetrieval", "hybrid_retrieve", "mmr_select", "rrf_fuse"]

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9\-.]*")

#: Standard BM25 practice strips function words before indexing -- without it,
#: incidental overlap on "file", "what's", "that" etc. can outweigh a genuine
#: match on the one token (a code, a date) that actually carries the question's
#: meaning, especially in a small corpus where IDF alone does not discount them
#: enough.
_STOPWORDS = frozenset(
    "a about after again against all also am an and any are as at be because been "
    "before being below between both but by can cannot could did do does doing done "
    "down during each few for from further had has have having he her here hers herself "
    "him himself his how however i if in into is it its itself just me more most much "
    "must my myself no nor not now of off on once only or other our ours ourselves out "
    "over own please same shall she should so some such than that the their theirs them "
    "themselves then there these they this those through to too under until up upon us "
    "very was we were what when where whether which while who whom why will with within "
    "would you your yours yourself yourselves whats thats".split()
)


def _tokenize(text: str) -> list[str]:
    return [tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS]


class BM25Index:
    """A BM25 index over a snapshot of every chunk currently in the vector store.

    Rebuilt on demand from :meth:`VectorStore.all_chunks`. Ingestion is
    infrequent relative to queries in this app, so there is no incremental
    update path -- callers rebuild after any ingest/delete.
    """

    def __init__(self) -> None:
        from rank_bm25 import BM25Okapi  # noqa: PLC0415 -- optional dependency, only needed in hybrid mode

        self._BM25Okapi = BM25Okapi
        self._chunks: list[RetrievedChunk] = []
        self._bm25: object | None = None

    @property
    def size(self) -> int:
        return len(self._chunks)

    def build(self, chunks: Sequence[RetrievedChunk]) -> None:
        self._chunks = list(chunks)
        corpus = [_tokenize(chunk.text) for chunk in self._chunks]
        self._bm25 = self._BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int) -> list[tuple[RetrievedChunk, float]]:
        """Top ``top_k`` chunks by BM25 score, best first."""
        if self._bm25 is None or not self._chunks:
            return []
        scores = self._bm25.get_scores(_tokenize(query))
        ranked = sorted(zip(self._chunks, scores), key=lambda pair: pair[1], reverse=True)
        return [(chunk, float(score)) for chunk, score in ranked[:top_k] if score > 0]


def rrf_fuse(
    ranked_lists: Sequence[Sequence[RetrievedChunk]],
    *,
    k: int = 60,
) -> list[tuple[RetrievedChunk, float]]:
    """Reciprocal Rank Fusion over any number of best-first ranked lists.

    Returns ``(chunk, fused_score)`` pairs sorted best first. A chunk missing
    from one ranker simply does not receive that ranker's term -- it is not
    penalised beyond "not being ranked there".
    """
    scores: dict[str, float] = {}
    representative: dict[str, RetrievedChunk] = {}
    for ranked in ranked_lists:
        for rank, chunk in enumerate(ranked, start=1):
            scores[chunk.chunk_id] = scores.get(chunk.chunk_id, 0.0) + 1.0 / (k + rank)
            representative.setdefault(chunk.chunk_id, chunk)

    fused = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [(representative[chunk_id], score) for chunk_id, score in fused]


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(y * y for y in b) ** 0.5
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def mmr_select(
    candidates: Sequence[RetrievedChunk],
    *,
    query_vector: Sequence[float],
    candidate_vectors: Sequence[Sequence[float]],
    lambda_: float,
    top_k: int,
) -> list[RetrievedChunk]:
    """Maximal Marginal Relevance re-ranking over an already-fused candidate list.

    ``lambda_=1.0`` is pure relevance (identical order to the input);
    ``lambda_=0.0`` picks the most relevant first chunk, then greedily
    maximises distance from what's already selected, ignoring relevance
    entirely thereafter.
    """
    if len(candidates) != len(candidate_vectors):
        raise ValueError("candidates and candidate_vectors must be the same length")

    remaining = list(range(len(candidates)))
    relevance = [_cosine(query_vector, vector) for vector in candidate_vectors]
    selected: list[int] = []

    while remaining and len(selected) < top_k:
        best_index, best_value = None, float("-inf")
        for i in remaining:
            diversity_penalty = (
                max(_cosine(candidate_vectors[i], candidate_vectors[j]) for j in selected) if selected else 0.0
            )
            value = lambda_ * relevance[i] - (1.0 - lambda_) * diversity_penalty
            if value > best_value:
                best_value, best_index = value, i
        assert best_index is not None
        selected.append(best_index)
        remaining.remove(best_index)

    return [candidates[i] for i in selected]


@dataclass(frozen=True, slots=True)
class HybridRetrieval:
    """Everything a caller needs to know about how a hybrid query was answered."""

    hits: list[RetrievedChunk]
    dense_ms: float
    bm25_ms: float
    fusion_ms: float
    mmr_ms: float
    dense_candidates: int
    bm25_candidates: int


def hybrid_retrieve(
    question: str,
    *,
    embedder: "Embedder",
    store: "VectorStore",
    bm25_index: BM25Index,
    top_k: int,
    candidate_pool: int,
    rrf_k: int,
    mmr_lambda: float | None,
) -> HybridRetrieval:
    """Dense search + BM25 search, fused with RRF, optionally re-ordered with MMR."""
    t0 = time.perf_counter()
    query_vector = embedder.embed_query(question)
    dense_hits = store.query(query_vector, candidate_pool)
    t1 = time.perf_counter()

    bm25_hits = [chunk for chunk, _score in bm25_index.search(question, candidate_pool)]
    t2 = time.perf_counter()

    fused = rrf_fuse([dense_hits, bm25_hits], k=rrf_k)
    fused_chunks = [chunk for chunk, _score in fused]
    t3 = time.perf_counter()

    mmr_ms = 0.0
    if mmr_lambda is not None and fused_chunks:
        pool = fused_chunks[: max(candidate_pool, top_k)]
        vectors = embedder.embed_documents([chunk.text for chunk in pool])
        t_mmr0 = time.perf_counter()
        ordered = mmr_select(
            pool, query_vector=query_vector, candidate_vectors=vectors, lambda_=mmr_lambda, top_k=top_k
        )
        mmr_ms = (time.perf_counter() - t_mmr0) * 1000
        hits = ordered
    else:
        hits = fused_chunks[:top_k]

    # Surface the fused RRF score as `.score` so downstream consumers (the eval
    # harness, the inspection view) see the number retrieval actually ranked by
    # in this mode, rather than a stale cosine similarity from the dense pass.
    fused_scores = {chunk.chunk_id: score for chunk, score in fused}
    hits = [
        RetrievedChunk(
            chunk_id=c.chunk_id,
            doc_id=c.doc_id,
            filename=c.filename,
            text=c.text,
            page=c.page,
            page_end=c.page_end,
            section_path=c.section_path,
            clause_label=c.clause_label,
            score=fused_scores.get(c.chunk_id, 0.0),
        )
        for c in hits
    ]

    return HybridRetrieval(
        hits=hits,
        dense_ms=(t1 - t0) * 1000,
        bm25_ms=(t2 - t1) * 1000,
        fusion_ms=(t3 - t2) * 1000,
        mmr_ms=mmr_ms,
        dense_candidates=len(dense_hits),
        bm25_candidates=len(bm25_hits),
    )
