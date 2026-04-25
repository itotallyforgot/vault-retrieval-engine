"""Dual-channel Router tests: vector + topology fan-out fused via RRF."""
import hashlib

import pytest

from vault_engine.router import Router
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore
from vault_engine.embedder import MockEmbedder
from vault_engine.config import EngineConfig


def _populated_stores(tmp_path):
    """Build a tiny graph + vec store suitable for fan-out tests."""
    # EngineConfig uses cache_dir (not store_path); vault_path must exist.
    wiki_dir = tmp_path / "wiki"
    wiki_dir.mkdir()
    cfg = EngineConfig(
        vault_path=tmp_path,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)

    embedder = MockEmbedder(dim=8)

    # VecStore.__init__ signature: (db_path, dim, model_name)
    vec = VecStore(cfg.cache_dir / "store.db", dim=8, model_name="mock")
    vec.open()

    graph = GraphStore()
    graph.add_node("topic-a", title="Topic A", kind="topic")
    graph.add_node("topic-b", title="Topic B", kind="topic")
    graph.add_node("source-s", title="Source S", kind="source")
    graph.add_edge("topic-a", "topic-b", relation="references")
    graph.add_edge("topic-a", "source-s", relation="cites")
    graph.finalize_build()

    # VecStore.upsert signature: (page_slug, chunk_idx, content, checksum, embedding)
    # MockEmbedder.encode(texts) returns ndarray of shape (n, dim); take [0] for single.
    for node_id, text in [
        ("topic-a", "auth mfa"),
        ("topic-b", "session token"),
        ("source-s", "totp paper"),
    ]:
        emb = embedder.encode([text])[0]
        checksum = hashlib.sha256(text.encode()).hexdigest()
        vec.upsert(
            page_slug=node_id,
            chunk_idx=0,
            content=text,
            checksum=checksum,
            embedding=emb,
        )

    return cfg, embedder, vec, graph


def test_router_dual_channel_runs_both_and_fuses(tmp_path):
    cfg, embedder, vec, graph = _populated_stores(tmp_path)
    router = Router(cfg=cfg, embedder=embedder, vec_store=vec, graph_store=graph)
    result = router.dispatch("auth mfa related to topic-a", seed_node="topic-a")
    assert "fused_hits" in result
    fused = result["fused_hits"]
    assert len(fused) > 0
    channels_seen = {c for hit in fused for c in hit.channels}
    assert "vector" in channels_seen
    assert "topology" in channels_seen


def test_router_vector_only_when_no_seed(tmp_path):
    """Without a seed_node and no multi-hop intent, only vector channel runs."""
    cfg, embedder, vec, graph = _populated_stores(tmp_path)
    router = Router(cfg=cfg, embedder=embedder, vec_store=vec, graph_store=graph)
    result = router.dispatch("auth mfa token")
    assert "fused_hits" in result
    fused = result["fused_hits"]
    assert len(fused) > 0
    channels_seen = {c for hit in fused for c in hit.channels}
    assert "vector" in channels_seen
    # topology should NOT appear (no seed, no multi-hop heuristic triggered)
    assert "topology" not in channels_seen


def test_router_multi_hop_intent_triggers_topology(tmp_path):
    """Query with 'related to' heuristic triggers topology even without explicit seed_node."""
    cfg, embedder, vec, graph = _populated_stores(tmp_path)
    router = Router(cfg=cfg, embedder=embedder, vec_store=vec, graph_store=graph)
    # "related to" triggers multi-hop heuristic; top vector hit becomes the seed
    result = router.dispatch("session token related to topic-a")
    assert "fused_hits" in result
    assert "vector_hits" in result
    assert "topology_hits" in result
    assert "intent" in result


def test_router_return_dict_has_all_keys(tmp_path):
    """Dispatch always returns all four keys regardless of channel path."""
    cfg, embedder, vec, graph = _populated_stores(tmp_path)
    router = Router(cfg=cfg, embedder=embedder, vec_store=vec, graph_store=graph)
    result = router.dispatch("totp paper")
    for key in ("intent", "vector_hits", "topology_hits", "fused_hits"):
        assert key in result, f"missing key: {key}"
