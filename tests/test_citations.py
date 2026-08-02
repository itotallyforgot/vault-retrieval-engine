from pathlib import Path

from vault_engine.citations import Citation, CitationAssembler
from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.retrieval import Retrieval, SearchHit


def _setup(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    idx.rebuild()
    r = Retrieval(cfg=cfg, indexer=idx, embedder=idx.embedder)
    a = CitationAssembler(cfg=cfg, retrieval=r)
    return idx, a


def test_assemble_chain_for_topic_with_source(sample_vault: Path, tmp_path: Path):
    idx, a = _setup(sample_vault, tmp_path)
    try:
        hit = SearchHit(page_slug="alpha", chunk_idx=0, content="...", distance=0.0)
        chain: list[Citation] = a.assemble([hit])
        # alpha references source "2026-01-01-alpha-source" via frontmatter.
        slugs = [c.page_slug for c in chain]
        assert "alpha" in slugs
        assert "2026-01-01-alpha-source" in slugs
        # Source page in turn references raw file via raw_path frontmatter.
        raw_paths = [c.raw_path for c in chain if c.raw_path]
        assert any(p and p.endswith("2026-01-01-alpha-raw.md") for p in raw_paths)
    finally:
        idx.close()


def test_assemble_chain_drops_unresolved_silently(sample_vault: Path, tmp_path: Path):
    idx, a = _setup(sample_vault, tmp_path)
    try:
        hit = SearchHit(page_slug="ghost-page", chunk_idx=0, content="...", distance=0.0)
        chain = a.assemble([hit])
        # Ghost page is not in vault — assembler returns empty (and logs).
        assert chain == []
    finally:
        idx.close()


def test_citation_raw_path_refuses_traversal(sample_vault: Path, tmp_path: Path):
    """`raw_path` is attacker-influenced frontmatter (pages scraped from URLs
    land in raw/ carrying whatever the page authored). `_walk` must apply the
    same vault-containment guard `Retrieval.source` uses, so an out-of-vault
    target yields None rather than an absolute path a serialized citation
    would disclose.
    """
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    (sample_vault / "wiki" / "sources" / "evil-source.md").write_text(
        "---\n"
        "title: Evil Source\n"
        "tags: [source]\n"
        "raw_path: ../secret.txt\n"
        "---\n\n# Evil Source\n\nTries to escape the vault.\n",
        encoding="utf-8",
    )
    idx, a = _setup(sample_vault, tmp_path)
    try:
        hit = SearchHit(page_slug="evil-source", chunk_idx=0, content="...", distance=0.0)
        chain = a.assemble([hit])
        assert [c.page_slug for c in chain] == ["evil-source"]
        assert chain[0].raw_path is None, f"leaked out-of-vault path: {chain[0].raw_path}"
    finally:
        idx.close()


def test_citation_raw_path_none_when_target_missing(sample_vault: Path, tmp_path: Path):
    """A raw_path pointing at nothing must not be reported as a real location."""
    (sample_vault / "wiki" / "sources" / "dangling-source.md").write_text(
        "---\n"
        "title: Dangling Source\n"
        "tags: [source]\n"
        "raw_path: raw/does-not-exist.md\n"
        "---\n\n# Dangling Source\n\nPoints at nothing.\n",
        encoding="utf-8",
    )
    idx, a = _setup(sample_vault, tmp_path)
    try:
        hit = SearchHit(page_slug="dangling-source", chunk_idx=0, content="...", distance=0.0)
        chain = a.assemble([hit])
        assert chain[0].raw_path is None
    finally:
        idx.close()
