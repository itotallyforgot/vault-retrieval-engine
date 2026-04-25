from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore


def test_indexer_emits_extracted_edges(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    graph = GraphStore()
    indexer = Indexer(cfg, embedder=MockEmbedder(dim=8))
    indexer.graph = graph
    indexer.open()
    indexer.rebuild()

    assert graph.graph.number_of_edges() > 0
    for u, v, data in graph.graph.edges(data=True):
        assert data["edge_type"] == "EXTRACTED", (u, v, data)


def test_indexer_full_rebuild_assigns_communities(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    graph = GraphStore()
    indexer = Indexer(cfg, embedder=MockEmbedder(dim=8))
    indexer.graph = graph
    indexer.open()
    indexer.rebuild()

    for node, data in graph.graph.nodes(data=True):
        assert "community" in data, f"{node} missing community"
