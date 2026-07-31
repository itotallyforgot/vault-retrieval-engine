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
    """One channel's hit on one document.

    ``doc_id`` is a page slug — fusion is page-level and stays that way.
    ``chunk_idx`` / ``content`` name the chunk that made this channel rank the
    page, and are ``None`` for channels that have no chunk to name (topology
    walks pages, not chunks).
    """

    doc_id: str
    score: float
    channel: str
    chunk_idx: int | None = None
    content: str | None = None


@dataclass
class FusedHit:
    """A fused, page-level hit that still knows which chunk matched.

    ``chunk_idx`` / ``content`` are the *representative* chunk: the one from
    the single best-ranked channel contribution to this page. When two
    channels disagree (vector's best chunk for a page is not lexical's),
    ``per_channel_chunks`` keeps both, so the disagreement is inspectable
    rather than discarded. Ties on rank are broken by channel order in the
    ``rankings`` argument (the Router passes vector, lexical, topology), which
    makes the pick deterministic, not arbitrary.
    """

    doc_id: str
    rrf_score: float
    channels: list[str] = field(default_factory=list)
    per_channel_scores: dict[str, float] = field(default_factory=dict)
    chunk_idx: int | None = None
    content: str | None = None
    per_channel_chunks: dict[str, int] = field(default_factory=dict)


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

    Fusion is keyed on ``doc_id`` (a page slug), not on the chunk. Chunk
    identity rides along on the fused hit; it never keys the accumulator, so
    the ranking is identical to the pre-chunk-identity implementation.
    """
    accum: dict[str, FusedHit] = {}
    best_rank: dict[str, int] = {}
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
            if hit.chunk_idx is None:
                continue
            # First occurrence within a channel is that channel's best rank.
            entry.per_channel_chunks.setdefault(hit.channel, hit.chunk_idx)
            # Representative chunk = best-ranked contribution seen so far.
            # Strict `<` keeps the earlier channel on a tie.
            prev = best_rank.get(hit.doc_id)
            if prev is None or rank_idx < prev:
                best_rank[hit.doc_id] = rank_idx
                entry.chunk_idx = hit.chunk_idx
                entry.content = hit.content
    fused = sorted(accum.values(), key=lambda h: h.rrf_score, reverse=True)
    return fused
