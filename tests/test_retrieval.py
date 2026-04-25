from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.retrieval import Retrieval


def _open_indexed(sample_vault: Path, tmp_path: Path) -> tuple[Indexer, Retrieval, EngineConfig]:
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    idx.rebuild()
    r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
    return idx, r, cfg


def test_search_returns_chunks_for_matching_query(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        hits = r.search("alpha", k=5)
        assert any(h.page_slug == "alpha" for h in hits)
        assert all(h.distance is not None for h in hits)
    finally:
        idx.close()


def test_expand_returns_full_page_body(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        body = r.expand("alpha")
        assert body is not None
        assert "Alpha" in body
        assert "Details" in body
    finally:
        idx.close()


def test_source_returns_raw_file_when_present(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        # The source page declares raw_path in frontmatter.
        text = r.source("2026-01-01-alpha-source")
        assert text is not None
        assert "Raw text body." in text
    finally:
        idx.close()


def test_source_returns_none_when_no_raw_path(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        assert r.source("alpha") is None
    finally:
        idx.close()


def test_consolidation_candidates_flags_orphan_raw(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        candidates = r.consolidation_candidates()
        assert "2026-01-01-alpha-raw" in candidates.orphan_pages
    finally:
        idx.close()
