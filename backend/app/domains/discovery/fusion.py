"""Hybrid retrieval fusion.

Keyword and vector search fail in opposite directions, which is exactly why
combining them works:

* Keyword nails exact names ("Fendika", "Tomoca") and rare tokens, and misses
  anything phrased differently from the listing.
* Vector handles paraphrase and intent ("somewhere quiet to work", "buna") and is
  weak on proper nouns, where lexical identity is the whole signal.

Fusion uses **Reciprocal Rank Fusion**. The alternative - normalising and adding
the two scores - requires the scores to be comparable, and they are not: one is a
BM25-ish relevance figure, the other a cosine distance. Their scales shift with
corpus and query, so any normalisation constant is a guess that silently rots.
RRF discards magnitudes and uses only rank position, which is why it is robust
without tuning.

    score(d) = Σ over retrievers  weight / (k + rank(d))

`k` damps the top-rank advantage so a single retriever cannot dominate on its
first hit alone.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

# 60 is the value from the original RRF paper and is a sensible default: large
# enough that ranks 1 and 2 are not wildly far apart, small enough that deep
# results still fade out.
RRF_K = 60

# Keyword is weighted slightly higher. Explorers search for named places more
# often than they paraphrase, and getting a proper noun wrong is more obviously
# broken to a user than missing a paraphrase.
KEYWORD_WEIGHT = 1.0
VECTOR_WEIGHT = 0.85


@dataclass(slots=True)
class FusedHit:
    experience_id: uuid.UUID
    score: float
    # Which retrievers found it, for debugging and for the search analytics the
    # specs ask for. A result found by both is a stronger signal than either alone.
    sources: set[str] = field(default_factory=set)

    @property
    def found_by_both(self) -> bool:
        return len(self.sources) > 1


def reciprocal_rank_fusion(
    ranked_lists: dict[str, list[uuid.UUID]],
    *,
    weights: dict[str, float] | None = None,
    k: int = RRF_K,
    limit: int | None = None,
) -> list[FusedHit]:
    """Fuse several ranked id lists into one.

    Input order is the only thing that matters; scores from the underlying
    retrievers are deliberately ignored.
    """
    weights = weights or {}
    accumulated: dict[uuid.UUID, FusedHit] = {}

    for source, ids in ranked_lists.items():
        weight = weights.get(source, 1.0)
        for rank, experience_id in enumerate(ids, start=1):
            hit = accumulated.get(experience_id)
            if hit is None:
                hit = FusedHit(experience_id=experience_id, score=0.0)
                accumulated[experience_id] = hit
            hit.score += weight / (k + rank)
            hit.sources.add(source)

    fused = sorted(accumulated.values(), key=lambda h: h.score, reverse=True)
    return fused[:limit] if limit else fused


def normalised_relevance(hits: list[FusedHit]) -> dict[str, float]:
    """Map fused scores to 0-1 for the ranking layer.

    The ranker treats relevance as one bounded signal among seven, so raw RRF
    scores - which have no fixed upper bound - have to be rescaled. Relative to
    the best hit in *this* result set, which is the only meaningful frame.
    """
    if not hits:
        return {}
    best = hits[0].score or 1.0
    return {str(hit.experience_id): min(1.0, hit.score / best) for hit in hits}
