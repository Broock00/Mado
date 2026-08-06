"""Embedding, fusion and secret-redaction tests.

These cover the failures in this area that are silent rather than loud: a query
compared against vectors from a different model returns confident nonsense rather
than an error, and a leaked API key looks exactly like a normal log line. Both
were real defects here, so both are pinned.
"""

from __future__ import annotations

import uuid

import pytest

from app.core.logging import redact
from app.domains.discovery.fusion import (
    KEYWORD_WEIGHT,
    VECTOR_WEIGHT,
    normalised_relevance,
    reciprocal_rank_fusion,
)
from app.integrations.embeddings import (
    EMBEDDING_DIM,
    TASK_DOCUMENT,
    TASK_QUERY,
    HashingEmbeddingProvider,
    embedding_text_for_experience,
)

# --- secret redaction --------------------------------------------------------
# httpx renders the full request URL into the text of every transport error, so a
# key passed as `?key=` reaches the logs the first time a request fails. The
# clients now use header auth; this is the second line of defence.


@pytest.mark.parametrize(
    "raw",
    [
        "Client error '404' for url 'https://x/y:batchEmbedContents?key=AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789'",
        "GET /v1/thing?api_key=sk-abcdef123456789 failed",
        "headers: {'authorization': 'Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig'}",
        "connect failed: password=hunter2plaintext",
    ],
)
def test_redact_removes_credentials(raw: str) -> None:
    cleaned = redact(raw)
    for secret in ("AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789", "sk-abcdef123456789",
                   "eyJhbGciOiJIUzI1NiJ9.payload.sig", "hunter2plaintext"):
        assert secret not in cleaned
    assert "[redacted]" in cleaned


def test_redact_leaves_ordinary_text_alone() -> None:
    message = "vector_search returned 12 hits for city=addis-ababa in 8ms"
    assert redact(message) == message


def test_log_processor_scrubs_nested_error_strings() -> None:
    """The processor must scrub *values*, not just known-sensitive key names."""
    from app.core.logging import _redact

    event = {"event": "request_failed", "error": "url 'https://x?key=AIzaSy0123456789abcdefghij'"}
    assert "AIzaSy0123456789abcdefghij" not in _redact(None, None, event)["error"]


# --- hashing provider --------------------------------------------------------


@pytest.mark.anyio
async def test_hashing_provider_is_deterministic_and_unit_length() -> None:
    provider = HashingEmbeddingProvider()
    first = await provider.embed(["Ethiopian coffee ceremony"], task=TASK_DOCUMENT)
    second = await provider.embed(["Ethiopian coffee ceremony"], task=TASK_DOCUMENT)

    assert first.vectors == second.vectors
    assert len(first.vectors[0]) == EMBEDDING_DIM
    magnitude = sum(value * value for value in first.vectors[0]) ** 0.5
    assert magnitude == pytest.approx(1.0, abs=1e-6)


@pytest.mark.anyio
async def test_hashing_batch_reports_its_own_identity() -> None:
    """Callers store `batch.model`, so it must name the real producer."""
    batch = await HashingEmbeddingProvider().embed(["x"], task=TASK_QUERY)
    assert batch.model == "hashing-v1"
    assert batch.is_semantic is False


@pytest.mark.anyio
async def test_hashing_captures_morphology_but_not_meaning() -> None:
    """The honest limit of the offline provider, asserted so nobody assumes more."""
    provider = HashingEmbeddingProvider()
    batch = await provider.embed(["market", "markets", "buna"], task=TASK_DOCUMENT)
    market, markets, buna = batch.vectors

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    assert cosine(market, markets) > 0.5
    # "buna" means coffee, but no lexical method can know that.
    assert cosine(market, buna) < 0.3


@pytest.mark.anyio
async def test_empty_input_returns_empty_batch() -> None:
    batch = await HashingEmbeddingProvider().embed([], task=TASK_DOCUMENT)
    assert batch.vectors == []
    assert not batch


# --- embedding text composition ----------------------------------------------


def test_embedding_text_includes_location_and_category() -> None:
    """Location and category must be embedded even when the prose omits them."""
    text = embedding_text_for_experience(
        title="Azmari Night",
        summary="Live traditional music.",
        description="An evening of song.",
        category="Nightlife",
        tags=["live-music", "traditional"],
        venue="Fendika",
        neighborhood="Kazanchis",
    )
    assert text.count("Azmari Night") == 2  # repeated: models weight early content
    assert "Category: Nightlife" in text
    assert "Fendika, Kazanchis" in text
    assert "live-music" in text


# --- reciprocal rank fusion --------------------------------------------------


def _ids(n: int) -> list[uuid.UUID]:
    return [uuid.uuid4() for _ in range(n)]


def test_rrf_rewards_agreement_between_retrievers() -> None:
    """A document both retrievers find should outrank one that only one found."""
    a, b, c = _ids(3)
    fused = reciprocal_rank_fusion(
        {"keyword": [b, a], "vector": [c, a]},
        weights={"keyword": KEYWORD_WEIGHT, "vector": VECTOR_WEIGHT},
    )
    assert fused[0].experience_id == a
    assert fused[0].found_by_both


def test_rrf_ignores_retriever_score_magnitudes() -> None:
    """Only rank position may influence the outcome - that is the point of RRF."""
    a, b = _ids(2)
    assert reciprocal_rank_fusion({"keyword": [a, b]})[0].experience_id == a


def test_keyword_outweighs_vector_on_a_tie() -> None:
    """Proper-noun failures are more visibly broken than missed paraphrases."""
    a, b = _ids(2)
    fused = reciprocal_rank_fusion(
        {"keyword": [a], "vector": [b]},
        weights={"keyword": KEYWORD_WEIGHT, "vector": VECTOR_WEIGHT},
    )
    assert fused[0].experience_id == a


def test_normalised_relevance_is_bounded() -> None:
    fused = reciprocal_rank_fusion({"keyword": _ids(5)})
    scores = normalised_relevance(fused)
    assert max(scores.values()) == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in scores.values())


def test_normalised_relevance_handles_no_hits() -> None:
    assert normalised_relevance([]) == {}
