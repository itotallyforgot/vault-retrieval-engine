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


def test_source_refuses_raw_path_escaping_vault(sample_vault: Path, tmp_path: Path):
    """E6: a crafted raw_path that traverses outside the vault root must NOT be
    read. raw_path is attacker-influenced frontmatter, so source() confines the
    resolved target to the vault (mirroring the write-path containment guard).
    """
    # Plant a secret OUTSIDE the vault, then a source page that tries to read it
    # via a traversal raw_path.
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET — must not leak", encoding="utf-8")
    # Vault root is tmp_path/vault, so a single ".." climbs to tmp_path where the
    # secret lives — outside the vault. That's the escape the guard must block.
    rel_to_secret = Path("..") / "secret.txt"
    (sample_vault / "wiki" / "sources" / "evil-source.md").write_text(
        "---\n"
        "title: Evil Source\n"
        "tags: [source]\n"
        f"raw_path: {rel_to_secret.as_posix()}\n"
        "---\n\n# Evil Source\n\nTries to escape the vault.\n",
        encoding="utf-8",
    )
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        # Sanity: the traversal really does point at the secret on disk.
        assert (sample_vault / rel_to_secret).resolve() == secret.resolve()
        # …but source() must refuse to read outside the vault root.
        assert r.source("evil-source") is None
    finally:
        idx.close()


def test_consolidation_candidates_flags_orphan_raw(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        candidates = r.consolidation_candidates()
        assert "2026-01-01-alpha-raw" in candidates.orphan_pages
    finally:
        idx.close()


def test_graph_walk_from_seeds_returns_paths(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        paths = r.graph_walk(seeds=["2026-01-01-alpha-source"], depth=2)
        slug_paths = [list(p) for p in paths]
        assert any(p == ["2026-01-01-alpha-source", "alpha", "beta"] for p in slug_paths)
    finally:
        idx.close()


def test_multi_hop_returns_paths_touching_multiple_seeds(sample_vault: Path, tmp_path: Path):
    idx, r, _ = _open_indexed(sample_vault, tmp_path)
    try:
        result = r.multi_hop(seed_query="alpha and beta", min_seeds_touched=2)
        # Should find at least one path through alpha->beta region.
        assert any("alpha" in p and "beta" in p for p in result.paths)
    finally:
        idx.close()
