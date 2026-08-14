"""Embedding backends.

Three providers, resolved in this order when ``EMBEDDING_PROVIDER=auto``:

1. **Voyage AI** (``VOYAGE_API_KEY`` set) -- the quality default. Voyage
   supports asymmetric embedding, so documents and queries are embedded with
   different ``input_type`` values, which measurably helps retrieval.
2. **sentence-transformers / all-MiniLM-L6-v2** -- fully local, for offline use.
3. **Hashing** -- a dependency-free lexical fallback (signed feature hashing
   over word uni/bi-grams and character 4-grams).

The third one exists so that ingestion, retrieval and the whole test suite run
on a machine with no API key *and* no PyTorch. It is a genuine lexical
retriever, not a stub -- but it has no semantic generalisation, so treat it as
a development/CI backend and use Voyage or MiniLM for anything real. The active
provider is reported by ``GET /health`` and recorded per document so you always
know which vectors are in the store.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re
from abc import ABC, abstractmethod
from collections import Counter
from typing import Sequence

from app.config import Settings

logger = logging.getLogger(__name__)

__all__ = [
    "Embedder",
    "EmbeddingUnavailableError",
    "HashingEmbedder",
    "SentenceTransformerEmbedder",
    "VoyageEmbedder",
    "build_embedder",
]

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Function words carry almost no retrieval signal but dominate short questions
#: -- without this list "What is the average annual rainfall...?" matches a
#: policy front page on `what/is/the/average/annual` alone. Content words only.
_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at
    be because been before being below between both but by
    can cannot could did do does doing done down during
    each few for from further
    had has have having he her here hers herself him himself his how however
    i if in into is it its itself
    just
    me more most much must my myself
    no nor not now
    of off on once only or other our ours ourselves out over own
    please
    same shall she should so some such
    than that the their theirs them themselves then there these they this those through to too
    under until up upon us
    very
    was we were what when where whether which while who whom why will with within would
    you your yours yourself yourselves
    """.split()
)


class EmbeddingUnavailableError(RuntimeError):
    """Raised when an explicitly requested embedding backend cannot be used."""


class Embedder(ABC):
    """Common interface every backend implements."""

    #: Stable identifier recorded with each ingested document.
    name: str
    #: Vector width, used to sanity-check a pre-existing collection.
    dimension: int

    @abstractmethod
    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed chunk texts for storage."""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """Embed a user question for retrieval."""


# --------------------------------------------------------------------------- #
# Voyage AI
# --------------------------------------------------------------------------- #
class VoyageEmbedder(Embedder):
    """Voyage AI embeddings (asymmetric: separate document and query modes)."""

    def __init__(self, *, api_key: str, model: str, batch_size: int = 96) -> None:
        try:
            import voyageai  # noqa: PLC0415 -- optional dependency, imported lazily
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise EmbeddingUnavailableError(
                "VOYAGE_API_KEY is set but the 'voyageai' package is not installed. "
                "Run: pip install voyageai"
            ) from exc

        self._client = voyageai.Client(api_key=api_key)
        self._model = model
        self._batch_size = batch_size
        self.name = f"voyage:{model}"
        self.dimension = 0  # discovered on first call

    def _embed(self, texts: Sequence[str], input_type: str) -> list[list[float]]:
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = list(texts[start : start + self._batch_size])
            result = self._client.embed(batch, model=self._model, input_type=input_type)
            vectors.extend(result.embeddings)
        if vectors and not self.dimension:
            self.dimension = len(vectors[0])
        return vectors

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return self._embed(texts, "document") if texts else []

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text], "query")[0]


# --------------------------------------------------------------------------- #
# sentence-transformers
# --------------------------------------------------------------------------- #
class SentenceTransformerEmbedder(Embedder):
    """Local all-MiniLM-L6-v2 (or any sentence-transformers model)."""

    def __init__(self, *, model_name: str) -> None:
        try:
            from sentence_transformers import SentenceTransformer  # noqa: PLC0415
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise EmbeddingUnavailableError(
                "sentence-transformers is not installed. Run: pip install sentence-transformers"
            ) from exc

        logger.info("loading sentence-transformers model %s (first run downloads weights)", model_name)
        self._model = SentenceTransformer(model_name)
        self.name = f"sentence-transformers:{model_name}"
        self.dimension = int(self._model.get_sentence_embedding_dimension())

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        vectors = self._model.encode(
            list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False
        )
        return [vector.tolist() for vector in vectors]

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


# --------------------------------------------------------------------------- #
# Hashing fallback
# --------------------------------------------------------------------------- #
class HashingEmbedder(Embedder):
    """Deterministic lexical embeddings via signed feature hashing.

    Features are word unigrams, word bigrams and intra-word character 4-grams,
    weighted by sub-linear term frequency and projected into a fixed-width
    vector. Uses BLAKE2b rather than :func:`hash` so vectors are stable across
    processes (Python randomises string hashing per interpreter run).
    """

    _UNIGRAM_WEIGHT = 1.0
    _BIGRAM_WEIGHT = 0.5
    _CHARGRAM_WEIGHT = 0.3
    _CHARGRAM_SIZE = 4
    _MIN_WORD_FOR_CHARGRAMS = 6

    def __init__(self, *, dimension: int = 512) -> None:
        self.dimension = dimension
        self.name = f"hashing:{dimension}"

    @staticmethod
    def _bucket(feature: str, dimension: int) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        return value % dimension, 1.0 if (value >> 63) & 1 else -1.0

    def _features(self, text: str) -> Counter[str]:
        words = [word for word in _WORD_RE.findall(text.lower()) if word not in _STOPWORDS]
        features: Counter[str] = Counter()
        for word in words:
            features[f"w:{word}"] += 1
            if len(word) >= self._MIN_WORD_FOR_CHARGRAMS:
                for index in range(len(word) - self._CHARGRAM_SIZE + 1):
                    features[f"c:{word[index : index + self._CHARGRAM_SIZE]}"] += 1
        for left, right in zip(words, words[1:]):
            features[f"b:{left}_{right}"] += 1
        return features

    def _weight(self, feature: str, count: int) -> float:
        base = 1.0 + math.log(count)
        if feature.startswith("b:"):
            return base * self._BIGRAM_WEIGHT
        if feature.startswith("c:"):
            return base * self._CHARGRAM_WEIGHT
        return base * self._UNIGRAM_WEIGHT

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for feature, count in self._features(text).items():
            index, sign = self._bucket(feature, self.dimension)
            vector[index] += sign * self._weight(feature, count)

        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed_one(text)


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #
def _try_voyage(settings: Settings) -> Embedder | None:
    if not settings.voyage_api_key:
        return None
    try:
        return VoyageEmbedder(
            api_key=settings.voyage_api_key,
            model=settings.voyage_model,
            batch_size=settings.voyage_batch_size,
        )
    except EmbeddingUnavailableError as exc:
        logger.warning("Voyage requested but unavailable (%s); falling through", exc)
        return None


def _try_sentence_transformers(settings: Settings) -> Embedder | None:
    try:
        return SentenceTransformerEmbedder(model_name=settings.sentence_transformer_model)
    except EmbeddingUnavailableError:
        return None
    except Exception as exc:  # pragma: no cover - model download / torch failures
        logger.warning("sentence-transformers unavailable (%s); falling through", exc)
        return None


def build_embedder(settings: Settings) -> Embedder:
    """Resolve the embedding backend named by ``EMBEDDING_PROVIDER``."""
    provider = settings.embedding_provider

    if provider == "voyage":
        if not settings.voyage_api_key:
            raise EmbeddingUnavailableError("EMBEDDING_PROVIDER=voyage but VOYAGE_API_KEY is not set")
        embedder = _try_voyage(settings)
        if embedder is None:
            raise EmbeddingUnavailableError("Voyage embeddings could not be initialised")
        return embedder

    if provider == "sentence-transformers":
        return SentenceTransformerEmbedder(model_name=settings.sentence_transformer_model)

    if provider == "hashing":
        return HashingEmbedder(dimension=settings.hashing_dimension)

    # auto
    if embedder := _try_voyage(settings):
        logger.info("embeddings: %s", embedder.name)
        return embedder
    if embedder := _try_sentence_transformers(settings):
        logger.info("embeddings: %s", embedder.name)
        return embedder

    logger.warning(
        "Falling back to the hashing embedder: set VOYAGE_API_KEY or install "
        "sentence-transformers for semantic retrieval."
    )
    return HashingEmbedder(dimension=settings.hashing_dimension)
