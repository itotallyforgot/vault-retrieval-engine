"""Reciprocal Rank Fusion for combining vector + topology retrieval channels.

RRF formula: score(d) = sum over channels of 1 / (k + rank(d in channel))
Default k=60 per the original paper (Cormack 2009). Robust to score-distribution
mismatch between channels (vector returns cosine [0,1], topology returns
shortest-path-derived integers).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RankedHit:
    doc_id: str
    score: float
    channel: str


@dataclass
class FusedHit:
    doc_id: str
    rrf_score: float
    channels: list[str] = field(default_factory=list)
    per_channel_scores: dict[str, float] = field(default_factory=dict)


def reciprocal_rank_fusion(
    rankings: list[list[RankedHit]],
    k: int = 60,
) -> list[FusedHit]:
    """Merge multiple channel rankings using RRF.

    Args:
        rankings: list of per-channel rankings, each sorted best-first.
        k: smoothing constant. 60 is the canonical default.

    Returns:
        Single ranking sorted by RRF score (descending). Each FusedHit lists the
        channels it appeared in.
    """
    accum: dict[str, FusedHit] = {}
    for ranking in rankings:
        for rank_idx, hit in enumerate(ranking):
            rrf_contribution = 1.0 / (k + rank_idx + 1)  # 1-indexed rank
            entry = accum.get(hit.doc_id)
            if entry is None:
                entry = FusedHit(doc_id=hit.doc_id, rrf_score=0.0)
                accum[hit.doc_id] = entry
            entry.rrf_score += rrf_contribution
            entry.channels.append(hit.channel)
            entry.per_channel_scores[hit.channel] = hit.score
    fused = sorted(accum.values(), key=lambda h: h.rrf_score, reverse=True)
    return fused
