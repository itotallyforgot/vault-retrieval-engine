from pathlib import Path

import numpy as np

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer


class SpyEmbedder:
    """Counts encode() calls and tracks every text encoded."""

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim
        self._inner = MockEmbedder(dim=dim)
        self.calls: int = 0
        self.encoded_texts: list[str] = []

    def encode(self, texts: list[str]) -> np.ndarray:
        self.calls += 1
        self.encoded_texts.extend(texts)
        return self._inner.encode(texts)


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


def test_indexer_rebuild_skips_encoding_for_unchanged_chunks(sample_vault: Path, tmp_path: Path):
    """Warm rebuild must NOT call embedder.encode for unchanged chunks.

    This is the P3 perf fix: previously rebuild() called encode() on every
    page's chunks before the upsert path checked the checksum, so a no-op
    rebuild on the real vault took 10+ minutes against mxbai.
    """
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    spy = SpyEmbedder(dim=cfg.embedding_dim)
    idx = Indexer(cfg=cfg, embedder=spy)
    idx.open()
    try:
        first = idx.rebuild()
        first_calls = spy.calls
        first_texts = list(spy.encoded_texts)
        assert first.chunks_indexed > 0
        assert first_calls > 0
        assert len(first_texts) == first.chunks_indexed

        # Warm cache: nothing changed → encode must not be called at all.
        second = idx.rebuild()
        assert spy.calls == first_calls
        assert spy.encoded_texts == first_texts
        assert second.chunks_changed == 0
        assert second.chunks_unchanged == first.chunks_indexed
    finally:
        idx.close()


def test_indexer_rebuild_drops_chunks_no_longer_present(sample_vault: Path, tmp_path: Path):
    """After re-chunking yields fewer chunks, the dropped chunks must be removed.

    Without an explicit drop pass, a shrunken page would leave orphan chunks in
    the vec store with stale content that could surface in search.
    """
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        before = idx.vec.get_checksums("alpha")
        assert len(before) >= 2  # alpha has ## Details subsection in fixture

        # Strip alpha down to a single short body — only one chunk should remain.
        (sample_vault / "wiki" / "topics" / "alpha.md").write_text(
            "---\ntitle: Alpha\naliases: []\ntags: [topic]\n"
            "sources: []\nlast_updated: 2026-01-03\n---\n\nShort body only.\n",
            encoding="utf-8",
        )
        idx.rebuild()
        after = idx.vec.get_checksums("alpha")
        assert len(after) == 1
        # Search must not surface stale chunks from the prior version.
        hits = idx.vec.search(np.ones(cfg.embedding_dim, dtype=np.float32), top_k=20)
        alpha_idxs = {h.chunk_idx for h in hits if h.page_slug == "alpha"}
        assert alpha_idxs == set(after.keys())
    finally:
        idx.close()


def test_indexer_reindex_page_preserves_unchanged_chunks(sample_vault: Path, tmp_path: Path):
    """reindex_page should also skip encoding for chunks whose checksum is unchanged.

    A common case: frontmatter-only edit (or reordering of unrelated chunks)
    leaves the body chunks identical. We should not re-encode them.
    """
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    spy = SpyEmbedder(dim=cfg.embedding_dim)
    idx = Indexer(cfg=cfg, embedder=spy)
    idx.open()
    try:
        idx.rebuild()
        baseline_calls = spy.calls
        # Touch alpha but leave body identical (frontmatter date bump only).
        (sample_vault / "wiki" / "topics" / "alpha.md").write_text(
            "---\n"
            "title: Alpha\n"
            'aliases: ["alpha-thing"]\n'
            "tags: [topic]\n"
            'sources: ["[[2026-01-01-alpha-source]]"]\n'
            'last_updated: "2026-01-15-bumped"\n'  # only change (string, not date)
            "---\n"
            "\n"
            "# Alpha\n"
            "\n"
            "Alpha references [[beta]] and is described by alpha-thing.\n"
            "\n"
            "## Details\n"
            "\n"
            "More detail about alpha.\n",
            encoding="utf-8",
        )
        report = idx.reindex_page(sample_vault / "wiki" / "topics" / "alpha.md")
        # Body chunks unchanged → encode should not be called again.
        assert spy.calls == baseline_calls
        assert report.chunks_changed == 0
        assert report.pages_processed == 1
    finally:
        idx.close()


def test_indexer_rebuild_emits_inferred_edges(sample_vault: Path, tmp_path: Path):
    """rebuild() should populate INFERRED edges between semantically close pages
    that aren't already connected by an EXTRACTED wikilink. Threshold is read
    from EngineConfig.inferred_edge_threshold.
    """
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        # Mock embedder produces wide-spread vectors; pick a low threshold so
        # we exercise the INFERRED path on the tiny sample vault.
        inferred_edge_threshold=0.3,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        edge_types = {
            (s, d): data.get("edge_type") for s, d, data in idx.graph.graph.edges(data=True)
        }
        # The fixture's alpha→beta wikilink must remain EXTRACTED.
        assert edge_types[("alpha", "beta")] == "EXTRACTED"
        # At least one INFERRED edge must be present.
        assert "INFERRED" in edge_types.values()
        # All INFERRED edges must carry a confidence in [threshold, 1.0].
        for (s, d), data in (
            ((s, d), idx.graph.graph.edges[s, d])
            for (s, d), t in edge_types.items()
            if t == "INFERRED"
        ):
            assert cfg.inferred_edge_threshold <= float(data["confidence"]) <= 1.0
    finally:
        idx.close()


def test_indexer_inferred_edges_never_overwrite_extracted(sample_vault: Path, tmp_path: Path):
    """If alpha→beta is already EXTRACTED via wikilink, the INFERRED pass must
    not downgrade it even at threshold 0.0."""
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        inferred_edge_threshold=0.0,
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        ab = idx.graph.graph.edges["alpha", "beta"]
        assert ab["edge_type"] == "EXTRACTED"
        assert ab["relation"] == "wikilink"
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
