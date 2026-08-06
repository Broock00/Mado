"""Embedding providers.

Spec 82.01 names Gemini Embeddings as the provider and BGE/Nomic/E5 as
alternatives, and spec 82.02 s17 requires embeddings to be versioned independently
of the chat model so they can be upgraded without touching application logic.

Two implementations ship:

* :class:`GeminiEmbeddingProvider` - the real one. Produces vectors with genuine
  semantic structure, so "buna" retrieves coffee and "somewhere quiet to work"
  retrieves cafes with wifi.

* :class:`HashingEmbeddingProvider` - a deterministic local fallback. **It is not
  semantic.** It projects character n-grams into a fixed space, which captures
  lexical and morphological overlap only: "coffee"/"coffees" land near each other,
  "buna"/"coffee" do not. It exists so the whole retrieval pipeline - indexing,
  ANN search, fusion, memory recall - is runnable and testable with no API key and
  no spend, and so a provider outage degrades quality rather than breaking search.

The distinction matters and is deliberately not blurred: code that needs to know
whether real semantics are available can check :attr:`is_semantic`.
"""

from __future__ import annotations

import hashlib
import math
import re
from dataclasses import dataclass
from functools import lru_cache
from typing import Protocol

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger, redact

logger = get_logger("mado.embeddings")

# Must match catalog.models.EMBEDDING_DIM and the pgvector column width.
EMBEDDING_DIM = 768

# text-embedding-004 is retired and now 404s. gemini-embedding-001 is the current
# model; it emits 3072 dimensions by default but is trained with Matryoshka
# representation learning, so a 768-dimension prefix is a valid embedding in its
# own right rather than a lossy crop. `outputDimensionality` asks for that prefix,
# which keeps the pgvector column at 768 - a quarter of the storage and index size
# for a negligible retrieval-quality difference at this corpus size.
GEMINI_EMBEDDING_MODEL = "gemini-embedding-001"

# Retrieval and storage need different vectors from the same text: a stored
# document is being described, a query is asking. Gemini exposes this as task type
# and it measurably improves retrieval over using one undifferentiated embedding.
TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
TASK_QUERY = "RETRIEVAL_QUERY"


@dataclass(slots=True)
class EmbeddingBatch:
    """Vectors plus the identity of whatever actually produced them.

    The model is returned rather than read off the configured provider because
    :class:`GeminiEmbeddingProvider` falls back to hashing during an outage. The
    caller stores this alongside the vector, so a later query can tell which rows
    are comparable to it and which are leftovers from a different vector space.
    """

    vectors: list[list[float]]
    model: str
    is_semantic: bool
    # Cosine distance past which a neighbour is not worth returning. Travels with
    # the batch because it is a property of the vector space, not of the caller.
    max_distance: float

    def __bool__(self) -> bool:
        return bool(self.vectors)


class EmbeddingProvider(Protocol):
    name: str
    is_semantic: bool
    max_distance: float

    async def embed(self, texts: list[str], *, task: str = TASK_DOCUMENT) -> EmbeddingBatch: ...


def _normalise(vector: list[float]) -> list[float]:
    """Scale to unit length so cosine distance and inner product agree."""
    magnitude = math.sqrt(sum(value * value for value in vector))
    if magnitude == 0:
        return vector
    return [value / magnitude for value in vector]


class HashingEmbeddingProvider:
    """Deterministic lexical embedding. Not semantic - see the module docstring."""

    name = "hashing-v1"
    is_semantic = False
    # Sparse hashed features are near-orthogonal when they share no tokens, so
    # unrelated text sits close to distance 1.0 and a loose cutoff is correct here.
    max_distance = 0.75

    async def embed(self, texts: list[str], *, task: str = TASK_DOCUMENT) -> EmbeddingBatch:
        return EmbeddingBatch(
            vectors=[self._embed_one(text) for text in texts],
            model=self.name,
            is_semantic=self.is_semantic,
            max_distance=self.max_distance,
        )

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIM
        tokens = _tokenize(text)
        if not tokens:
            return vector

        for token in tokens:
            # Whole word plus character trigrams, so morphological variants
            # ("market"/"markets") land close together.
            for feature in (token, *_trigrams(token)):
                index = (
                    int.from_bytes(hashlib.blake2b(feature.encode(), digest_size=4).digest(), "big")
                    % EMBEDDING_DIM
                )
                # Sign from a second hash keeps unrelated collisions from always
                # reinforcing each other.
                sign = 1.0 if hashlib.md5(feature.encode()).digest()[0] % 2 else -1.0
                # Sublinear weighting: a word repeated ten times is not ten times
                # as informative.
                vector[index] += sign / math.sqrt(len(tokens))

        return _normalise(vector)


_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")

# Words too common in this corpus to carry retrieval signal.
_STOPWORDS = frozenset(
    """a an the and or but if then than that this these those of in on at to for with
    from by is are was were be been being it its as into over under about you your we
    our they their he she his her i me my not no yes do does did will would can could
    should there here when where what which who whom how all any some more most other
    each every own same so such only just also very""".split()  # noqa: SIM905 - reads better than 90 quoted strings
)


def _tokenize(text: str) -> list[str]:
    return [
        token
        for token in _TOKEN_PATTERN.findall(text.lower())
        if len(token) > 2 and token not in _STOPWORDS
    ]


def _trigrams(token: str) -> list[str]:
    if len(token) <= 3:
        return [token]
    padded = f"^{token}$"
    return [padded[i : i + 3] for i in range(len(padded) - 2)]


class GeminiEmbeddingProvider:
    """Adapter for the Gemini embedding API."""

    name = GEMINI_EMBEDDING_MODEL
    is_semantic = True

    # Cosine distance beyond which a match is noise. Model-specific on purpose:
    # gemini-embedding-001 has a high similarity floor - two unrelated phrases
    # still score ~0.49 - so a threshold tuned for a model whose floor is near
    # zero would admit the entire corpus and filter nothing.
    max_distance = 0.45

    def __init__(self, api_key: str, *, timeout: float = 30.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._base = "https://generativelanguage.googleapis.com/v1beta"
        self._fallback = HashingEmbeddingProvider()

    async def embed(self, texts: list[str], *, task: str = TASK_DOCUMENT) -> EmbeddingBatch:
        if not texts:
            return EmbeddingBatch([], self.name, self.is_semantic, self.max_distance)

        url = f"{self._base}/models/{GEMINI_EMBEDDING_MODEL}:batchEmbedContents"
        payload = {
            "requests": [
                {
                    "model": f"models/{GEMINI_EMBEDDING_MODEL}",
                    "content": {"parts": [{"text": text[:8000]}]},
                    "taskType": task,
                    "outputDimensionality": EMBEDDING_DIM,
                }
                for text in texts
            ]
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                # Header auth, not `?key=`. httpx puts the full URL into the string
                # form of every transport error, so a key in the query string ends
                # up verbatim in logs and tracebacks the moment a request fails.
                response = await client.post(
                    url, headers={"x-goog-api-key": self._api_key}, json=payload
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPError as exc:
            # Degrade rather than fail: search that is lexically-only is far better
            # than a discovery surface that returns an error (spec 55.01 s40).
            logger.warning("embedding_request_failed_falling_back", error=redact(str(exc)))
            return await self._fallback.embed(texts, task=task)

        vectors = [item.get("values", []) for item in body.get("embeddings", [])]
        if len(vectors) != len(texts) or any(len(v) != EMBEDDING_DIM for v in vectors):
            logger.warning(
                "embedding_shape_unexpected",
                returned=len(vectors),
                expected=len(texts),
                dim=len(vectors[0]) if vectors else 0,
            )
            return await self._fallback.embed(texts, task=task)

        # Re-normalise: a 768-dimension Matryoshka prefix of a unit 3072-vector is
        # not itself unit length, and cosine distance assumes it is.
        return EmbeddingBatch(
            vectors=[_normalise(vector) for vector in vectors],
            model=self.name,
            is_semantic=self.is_semantic,
            max_distance=self.max_distance,
        )


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    """Select the embedding provider.

    Embeddings are configured separately from chat generation: they are far
    cheaper, and it is reasonable to want real semantic retrieval while keeping
    generation local. ``MADO_EMBEDDING_PROVIDER=auto`` follows the chat provider,
    which keeps the default behaviour unsurprising.
    """
    settings = get_settings()
    choice = settings.embedding_provider
    if choice == "auto":
        choice = "gemini" if settings.ai_provider == "gemini" else "hashing"

    if choice == "gemini":
        if not settings.gemini_api_key:
            logger.warning("gemini_embeddings_selected_without_key_falling_back")
            return HashingEmbeddingProvider()
        return GeminiEmbeddingProvider(
            settings.gemini_api_key, timeout=settings.ai_request_timeout_seconds
        )
    return HashingEmbeddingProvider()


def embedding_text_for_experience(
    *,
    title: str,
    summary: str | None,
    description: str,
    category: str | None,
    tags: list[str],
    venue: str | None,
    neighborhood: str | None,
) -> str:
    """Compose the text an experience is embedded from.

    Ordered by signal strength, and repeating the title once, because embedding
    models weight earlier and repeated content more heavily. Location and category
    are included so "somewhere in Piassa" and "live music" retrieve sensibly even
    when the description never uses those words.
    """
    parts = [title, title]
    if summary:
        parts.append(summary)
    if category:
        parts.append(f"Category: {category}")
    if tags:
        parts.append("Tags: " + ", ".join(tags))
    location = ", ".join(filter(None, [venue, neighborhood]))
    if location:
        parts.append(f"Located at {location}")
    parts.append(description)
    return "\n".join(parts)
