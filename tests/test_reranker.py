import pytest

from vault_engine.reranker import RankedHit, reciprocal_rank_fusion


def test_rrf_merges_two_rankings():
    a = [RankedHit("doc-1", 0.9, channel="vector"), RankedHit("doc-2", 0.8, channel="vector")]
    b = [RankedHit("doc-2", 0.7, channel="topology"), RankedHit("doc-3", 0.6, channel="topology")]
    fused = reciprocal_rank_fusion([a, b], k=60)
    ids = [h.doc_id for h in fused]
    assert ids[0] == "doc-2"
    assert "doc-1" in ids and "doc-3" in ids


def test_rrf_preserves_channel_provenance():
    a = [RankedHit("doc-1", 0.9, channel="vector")]
    b = [RankedHit("doc-1", 0.7, channel="topology")]
    fused = reciprocal_rank_fusion([a, b])
    assert sorted(fused[0].channels) == ["topology", "vector"]


def test_rrf_handles_empty_channels():
    a: list[RankedHit] = []
    b = [RankedHit("doc-1", 0.5, channel="vector")]
    fused = reciprocal_rank_fusion([a, b])
    assert len(fused) == 1
    assert fused[0].doc_id == "doc-1"


# --- chunk identity through fusion (roadmap item 5) ---


def test_rrf_carries_chunk_identity_from_the_channel():
    a = [RankedHit("doc-1", 0.9, channel="vector", chunk_idx=3, content="third chunk")]
    fused = reciprocal_rank_fusion([a])
    assert fused[0].chunk_idx == 3
    assert fused[0].content == "third chunk"
    assert fused[0].per_channel_chunks == {"vector": 3}


def test_rrf_still_fuses_on_page_not_chunk():
    """Two chunks of the same page fuse into ONE hit that accumulates both
    contributions. Chunk identity rides along; it never keys the accumulator."""
    a = [
        RankedHit("doc-1", 0.9, channel="vector", chunk_idx=0, content="c0"),
        RankedHit("doc-1", 0.8, channel="vector", chunk_idx=1, content="c1"),
    ]
    fused = reciprocal_rank_fusion([a], k=60)
    assert [h.doc_id for h in fused] == ["doc-1"]
    assert fused[0].rrf_score == pytest.approx(1 / 61 + 1 / 62)
    assert fused[0].chunk_idx == 0  # best-ranked chunk represents the page


def test_rrf_multi_channel_chunk_disagreement_keeps_both():
    """Vector's best chunk for a page differs from lexical's. The fused hit
    reports the best-ranked one and keeps the other in per_channel_chunks."""
    vector = [
        RankedHit("doc-other", 0.1, channel="vector", chunk_idx=0, content="x"),
        RankedHit("doc-1", 0.9, channel="vector", chunk_idx=3, content="vector chunk"),
    ]
    lexical = [RankedHit("doc-1", -8.0, channel="lexical", chunk_idx=7, content="lexical chunk")]
    fused = reciprocal_rank_fusion([vector, lexical])
    hit = next(h for h in fused if h.doc_id == "doc-1")
    # lexical ranked it first (rank 0) vs vector rank 1 -> lexical represents it
    assert hit.chunk_idx == 7
    assert hit.content == "lexical chunk"
    assert hit.per_channel_chunks == {"vector": 3, "lexical": 7}


def test_rrf_chunk_tie_breaks_on_channel_order():
    """Same rank in both channels: the earlier ranking wins, deterministically."""
    vector = [RankedHit("doc-1", 0.9, channel="vector", chunk_idx=3, content="v")]
    lexical = [RankedHit("doc-1", -8.0, channel="lexical", chunk_idx=7, content="l")]
    assert reciprocal_rank_fusion([vector, lexical])[0].chunk_idx == 3
    assert reciprocal_rank_fusion([lexical, vector])[0].chunk_idx == 7


def test_rrf_page_level_channel_contributes_no_chunk():
    """Topology walks pages, not chunks: it must not invent a chunk_idx."""
    topology = [RankedHit("doc-1", 0.5, channel="topology")]
    vector = [RankedHit("doc-1", 0.9, channel="vector", chunk_idx=2, content="c2")]
    only_topology = reciprocal_rank_fusion([topology])[0]
    assert only_topology.chunk_idx is None
    assert only_topology.per_channel_chunks == {}
    mixed = reciprocal_rank_fusion([topology, vector])[0]
    assert mixed.chunk_idx == 2  # the channel that HAS a chunk supplies it
    assert "topology" not in mixed.per_channel_chunks
