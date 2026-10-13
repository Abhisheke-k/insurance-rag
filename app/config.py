"""Environment-driven configuration.

Every knob that affects ingestion output (chunk size, overlap, embedding
provider, collection name) is settable from the environment so that a full
re-ingestion at a different setting is a config change, not a code change.
That is what makes the chunk-size comparison in the README reproducible.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

EmbeddingProvider = Literal["auto", "voyage", "sentence-transformers", "hashing"]
Effort = Literal["low", "medium", "high", "xhigh", "max"]
LLMProviderName = Literal["auto", "anthropic", "openai", "stub"]
RetrievalMode = Literal["dense", "hybrid_bm25_rrf"]


class Settings(BaseSettings):
    """Runtime configuration, read from the environment and/or a `.env` file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # Chunking
    # ------------------------------------------------------------------ #
    chunk_size: int = Field(
        default=550,
        ge=64,
        description="Target chunk size in (estimated) tokens. 500-600 is the tuned default.",
    )
    chunk_overlap: int = Field(
        default=120,
        ge=0,
        description="Overlap between adjacent chunks, in (estimated) tokens.",
    )
    min_chunk_size: int = Field(
        default=100,
        ge=0,
        description="Chunks smaller than this are merged into their neighbour when possible.",
    )

    # ------------------------------------------------------------------ #
    # Embeddings
    # ------------------------------------------------------------------ #
    embedding_provider: EmbeddingProvider = Field(
        default="auto",
        description=(
            "'auto' picks Voyage when VOYAGE_API_KEY is set, then sentence-transformers, "
            "then the dependency-free hashing embedder."
        ),
    )
    voyage_api_key: str | None = None
    voyage_model: str = "voyage-3-large"
    voyage_batch_size: int = Field(default=96, ge=1, le=128)
    sentence_transformer_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    hashing_dimension: int = Field(default=512, ge=64)

    # ------------------------------------------------------------------ #
    # Vector store
    # ------------------------------------------------------------------ #
    chroma_dir: Path = Path("./chroma_db")
    collection_name: str = "endorsement_chunks"
    force_in_memory_store: bool = Field(
        default=False,
        description="Skip ChromaDB and use the in-process store (used by the test suite).",
    )

    # ------------------------------------------------------------------ #
    # Retrieval
    # ------------------------------------------------------------------ #
    top_k: int = Field(default=5, ge=1, le=50)
    claim_top_k: int = Field(
        default=4,
        ge=1,
        le=50,
        description=(
            "How many passages a claim summary / claims-agent search retrieves (``/ask`` keeps TOP_K). 4, not 5: the governing "
            "passage is still in the top 4 for 99% of claims (98% at 3, 84% at 2) while input tokens per request fall ~16%. "
            "Measured by scripts/w11_cost_improvement.py; k=3 was rejected because the governing passage is then missing for "
            "54% of regulatory-fine claims (17% at k=5)."
        ),
    )
    min_relevance: float = Field(
        default=0.12,
        ge=-1.0,
        le=1.0,
        description=(
            "Cosine-similarity floor. If the best retrieved chunk scores below this, "
            "/ask short-circuits to the not-found answer without calling Claude. "
            "PROVIDER-SPECIFIC: the default is measured against the hashing embedder "
            "(in-scope 0.13-0.36, out-of-scope 0.06-0.10 on the sample pack). Voyage and "
            "MiniLM cosines sit much higher -- re-tune before trusting the gate on those."
        ),
    )
    min_relevance_hybrid: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description=(
            "The relevance floor used instead of MIN_RELEVANCE when RETRIEVAL_MODE is hybrid_bm25_rrf. Hybrid mode "
            "reports fused Reciprocal-Rank-Fusion scores (at most ~0.033 with k=60), not cosines, so the cosine-calibrated "
            "MIN_RELEVANCE (0.12) rejected *every* question -- found by the Week 11 traces. 0 disables the gate: it is "
            "a cost optimisation, and citation verification is what prevents fabricated answers."
        ),
    )
    retrieval_mode: RetrievalMode = Field(
        default="dense",
        description=(
            "'dense' -- cosine search over the embedder only (the original behaviour). "
            "'hybrid_bm25_rrf' -- BM25 over chunk text, fused with dense ranks via "
            "Reciprocal Rank Fusion (k=60). See app/hybrid_retrieval.py and the Week 4 "
            "retrieval writeup in coursework/w4/."
        ),
    )
    carry_clause_stem: bool = Field(
        default=True,
        description=(
            "When a retrieved chunk is a continuation of a list clause, prefix it with the clause's lead-in "
            "(app/clause_stem.py). Without it an exclusion's items can arrive stripped of the words 'this policy "
            "excludes ... the following:' and read as covered. CARRY_CLAUSE_STEM=false is the instant rollback."
        ),
    )
    version_filter: bool = Field(
        default=True,
        description=(
            "Week 12. When a loss date is supplied, drop wording that was not in force on that date (app/versions.py) and tag what "
            "remains with its edition. VERSION_FILTER=false is the pre-fix behaviour -- every edition is offered and the model "
            "picks -- kept as a switch so the failure it prevents can be reproduced (the closed loop in coursework/w12/)."
        ),
    )
    version_pool_factor: int = Field(
        default=3,
        ge=1,
        le=10,
        description=(
            "Loss-date retrieval fetches factor*k candidates, narrows them to the wording in force, then keeps k -- so that "
            "dropping a superseded edition does not leave the answer short a passage."
        ),
    )
    rrf_k: int = Field(default=60, ge=1, description="RRF fusion constant k in 1/(k+rank).")
    bm25_candidates: int = Field(
        default=25, ge=1, description="How many candidates each of BM25/dense contributes before fusion."
    )
    mmr_lambda: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description=(
            "If set, apply Maximal Marginal Relevance over the fused candidate list before "
            "truncating to top_k. 1.0 = pure relevance (no diversity), 0.0 = pure diversity. "
            "None disables MMR. Week 4 bonus challenge."
        ),
    )

    # ------------------------------------------------------------------ #
    # Generation -- LLM provider
    #
    # Mirrors EMBEDDING_PROVIDER: one interface (app/llm.py), several backends,
    # resolved in this order under 'auto': Anthropic -> OpenAI -> deterministic
    # stub. The stub makes the whole pipeline (including the Week 5/6 tracing
    # and eval tooling) runnable with no API key and no network call.
    # ------------------------------------------------------------------ #
    llm_provider: LLMProviderName = "auto"
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    answer_model: str = "claude-opus-5"
    answer_effort: Effort = "medium"
    answer_max_tokens: int = Field(default=16000, ge=1024)

    # ------------------------------------------------------------------ #
    # Claims agent (Week 7) -- stop conditions for the think/act/observe loop
    # ------------------------------------------------------------------ #
    agent_max_steps: int = Field(
        default=6,
        ge=1,
        description="Hard cap on loop turns. Hit without finalizing -> needs_review, not a hang.",
    )
    agent_timeout_seconds: float = Field(
        default=30.0,
        ge=1.0,
        description="Wall-clock budget for the whole loop, checked once per turn.",
    )

    # ------------------------------------------------------------------ #
    # Observability (Week 11)
    # ------------------------------------------------------------------ #
    trace_log_path: Path | None = Field(
        default=Path("./logs/traces.jsonl"),
        description=(
            "Where per-request traces are appended, one JSON line each (app/observability.py). "
            "Set TRACE_LOG_PATH= (empty) to turn trace writing off; spans still run, nothing is written."
        ),
    )

    # ------------------------------------------------------------------ #
    # Service
    # ------------------------------------------------------------------ #
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    cors_allow_origins: str = Field(
        default="*",
        description="Comma-separated list; '*' lets you open frontend/index.html from disk.",
    )

    @model_validator(mode="after")
    def _check_overlap(self) -> "Settings":
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be smaller than "
                f"CHUNK_SIZE ({self.chunk_size})"
            )
        return self

    @field_validator("trace_log_path", mode="before")
    @classmethod
    def _empty_trace_path_means_off(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    @property
    def registry_path(self) -> Path:
        """Where the document manifest that backs `GET /documents` lives."""
        return self.chroma_dir / "documents.json"

    @property
    def cors_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allow_origins.split(",") if origin.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton (cache cleared by the test suite)."""
    return Settings()
