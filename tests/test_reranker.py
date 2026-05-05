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
