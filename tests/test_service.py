from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.router import QueryMode
from vault_engine.service import Service


def test_service_starts_and_stops_cleanly(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    assert svc.is_running()
    svc.stop()
    assert not svc.is_running()


def test_service_indexes_on_first_start(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        assert svc.graph_store.graph.number_of_nodes() > 0
    finally:
        svc.stop()


def test_service_dispatches_query_through_router(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        result = svc.query("anything")
        assert "fused_hits" in result
    finally:
        svc.stop()


def test_service_lookup_intent_uses_vault_slugs_titles_and_aliases(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        for query in ("alpha", "Alpha", "alpha-thing"):
            result = svc.query(query)
            assert result["intent"] == QueryMode.LOOKUP
    finally:
        svc.stop()
