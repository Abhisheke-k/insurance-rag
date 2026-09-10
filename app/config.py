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

from pydantic import Field, model_validator
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
    retrieval_mode: RetrievalMode = Field(
        default="dense",
        description=(
            "'dense' -- cosine search over the embedder only (the original behaviour). "
            "'hybrid_bm25_rrf' -- BM25 over chunk text, fused with dense ranks via "
            "Reciprocal Rank Fusion (k=60). See app/hybrid_retrieval.py and the Week 4 "
            "retrieval writeup in coursework/w4/."
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
