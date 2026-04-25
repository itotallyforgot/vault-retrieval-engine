import time
from pathlib import Path

from vault_engine.service import Service
from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder


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
