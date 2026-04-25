from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer


def test_indexer_full_rebuild_populates_stores(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        report = idx.rebuild()
        assert report.pages_processed == 4
        assert report.chunks_indexed >= 4
        assert idx.graph.has_node("alpha")
        assert idx.graph.has_edge("alpha", "beta")
    finally:
        idx.close()


def test_indexer_incremental_skips_unchanged(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        first = idx.rebuild()
        second = idx.rebuild()
        assert second.chunks_changed == 0
        assert second.chunks_unchanged == first.chunks_indexed
    finally:
        idx.close()


def test_indexer_reindex_single_page(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        # modify a page on disk
        (sample_vault / "wiki" / "topics" / "alpha.md").write_text(
            "---\ntitle: Alpha\naliases: []\ntags: [topic]\nsources: []\nlast_updated: 2026-01-03\n---\n\n# Alpha\n\nNew body.\n",
            encoding="utf-8",
        )
        report = idx.reindex_page(sample_vault / "wiki" / "topics" / "alpha.md")
        assert report.chunks_changed >= 1
        # Graph should no longer have alpha->beta edge (body doesn't link beta).
        assert not idx.graph.has_edge("alpha", "beta")
    finally:
        idx.close()
