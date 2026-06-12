from pathlib import Path

import numpy as np

import vault_engine.vault_reader as vault_reader
from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer, IndexReport


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
        for (_s, _d), data in (
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


def test_indexer_rebuild_reports_skipped_pages(sample_vault: Path, tmp_path: Path, monkeypatch):
    """E4: rebuild() counts oversize/unreadable pages on the report and logs them.

    Previously these were swallowed inside iter_pages with a never-wired
    'surface via logging at the indexer layer' comment. Now the report carries
    pages_skipped + the SkippedPage list.
    """
    monkeypatch.setattr(vault_reader, "_MAX_PAGE_BYTES", 2000)
    big = sample_vault / "wiki" / "topics" / "huge.md"
    big.write_text("---\ntitle: Huge\n---\n\n" + ("x" * 3000) + "\n", encoding="utf-8")

    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        report = idx.rebuild()
        assert report.pages_skipped == 1
        assert [s.path for s in report.skipped] == [big]
        assert "too large" in report.skipped[0].reason
        # The oversize page must not have been indexed.
        assert not idx.graph.has_node("huge")
    finally:
        idx.close()


def test_indexer_reindex_oversize_page_is_skipped_not_raised(
    sample_vault: Path, tmp_path: Path, monkeypatch
):
    """E4: reindex_page on an oversize file records a skip instead of raising
    into the watcher callback (which would otherwise be logged as a failure)."""
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        # Now make alpha oversize and reindex it.
        monkeypatch.setattr(vault_reader, "_MAX_PAGE_BYTES", 2000)
        alpha = sample_vault / "wiki" / "topics" / "alpha.md"
        alpha.write_text("---\ntitle: Alpha\n---\n\n" + ("y" * 3000) + "\n", encoding="utf-8")
        report = idx.reindex_page(alpha)
        assert report.pages_skipped == 1
        assert report.skipped[0].path == alpha
        # Stale alpha chunks must have been dropped.
        assert idx.vec.get_checksums("alpha") == {}
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


def _build_linked_vault(root: Path, n: int) -> Path:
    """Create ``n`` cross-linked topic pages that share vocabulary, so the
    INFERRED-edge pass has real work to do."""
    vault = root / "vault"
    (vault / "wiki" / "topics").mkdir(parents=True)
    for i in range(n):
        nxt = (i + 1) % n
        (vault / "wiki" / "topics" / f"p{i}.md").write_text(
            f"---\ntitle: Page {i}\naliases: []\ntags: [topic]\nsources: []\n"
            f"last_updated: 2026-01-01\n---\n\n# Page {i}\n\n"
            f"Body {i} shares vocab token{i % 5}. Links [[p{nxt}]].\n",
            encoding="utf-8",
        )
    return vault


def test_reindex_page_edges_match_cold_rebuild(tmp_path: Path):
    """E2: the cached INFERRED-edge fast path must be correctness-equivalent to
    a full cold rebuild from disk — identical edges, types and confidences.

    This is the safety property behind the page-vector cache: it changes
    performance, never results.
    """
    vault = _build_linked_vault(tmp_path, 12)
    cfg = EngineConfig(
        vault_path=vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=16,
        inferred_edge_threshold=0.85,
    )
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=16))
    idx.open()
    try:
        idx.rebuild()
        # Mutate one page's content, then take the cached reindex path.
        p3 = vault / "wiki" / "topics" / "p3.md"
        p3.write_text(
            "---\ntitle: Page 3\naliases: []\ntags: [topic]\nsources: []\n"
            "last_updated: 2026-02-02\n---\n\n# Page 3\n\n"
            "Body 3 now uses an entirely different token9 vocabulary set. Links [[p4]].\n",
            encoding="utf-8",
        )
        idx.reindex_page(p3)

        def edge_set(g):
            return {
                (u, v, d.get("edge_type"), round(float(d.get("confidence", -1.0)), 5))
                for u, v, d in g.graph.graph.edges(data=True)
            }

        cached = edge_set(idx)
    finally:
        idx.close()

    # Ground truth: a brand-new indexer doing a full cold rebuild over the same
    # on-disk vault state.
    cold = Indexer(
        cfg=EngineConfig(
            vault_path=vault,
            cache_dir=tmp_path / "cache_cold",
            embedding_model="mock",
            embedding_dim=16,
            inferred_edge_threshold=0.85,
        ),
        embedder=MockEmbedder(dim=16),
    )
    cold.open()
    try:
        cold.rebuild()
        truth = {
            (u, v, d.get("edge_type"), round(float(d.get("confidence", -1.0)), 5))
            for u, v, d in cold.graph.graph.edges(data=True)
        }
    finally:
        cold.close()

    assert cached == truth, (
        f"cached reindex diverged from cold rebuild: "
        f"only-cached={cached - truth}, only-truth={truth - cached}"
    )


def test_reindex_page_reuses_vector_cache_for_unchanged_pages(tmp_path: Path):
    """E2: a per-file reindex must NOT re-fetch every page's chunk vectors from
    the store. Only the changed slug's vectors should be read for the page-
    vector refresh (the rest come from the in-memory cache).
    """
    vault = _build_linked_vault(tmp_path, 8)
    cfg = EngineConfig(
        vault_path=vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=16,
        inferred_edge_threshold=0.85,
    )
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=16))
    idx.open()
    try:
        idx.rebuild()  # warms the cache

        # Count iter_chunks_for_page calls per slug during the reindex.
        real = idx.vec.iter_chunks_for_page
        fetched: list[str] = []

        def spy(slug: str):
            fetched.append(slug)
            return real(slug)

        idx.vec.iter_chunks_for_page = spy  # type: ignore[method-assign]
        p2 = vault / "wiki" / "topics" / "p2.md"
        p2.write_text(
            "---\ntitle: Page 2\naliases: []\ntags: [topic]\nsources: []\n"
            "last_updated: 2026-03-03\n---\n\n# Page 2\n\nBody 2 changed token4. Links [[p3]].\n",
            encoding="utf-8",
        )
        idx.reindex_page(p2)

        # The vector refresh must touch ONLY the changed page, not all 8.
        # (_index_page_chunks reads checksums, not vectors via this method, so
        # the only iter_chunks_for_page caller on the warm path is the single
        # changed slug's _refresh_page_vector.)
        assert fetched == ["p2"], f"expected only p2 re-fetched, got {fetched}"
    finally:
        idx.close()


def test_reindex_page_cold_cache_still_emits_inferred_edges(tmp_path: Path):
    """E2: reindex_page called WITHOUT a prior rebuild (empty cache) must still
    produce the full INFERRED-edge set — the cold-cache fallback populates the
    whole cache rather than emitting a single-page (edgeless) graph.
    """
    vault = _build_linked_vault(tmp_path, 10)
    # Low threshold so the mock-vector vault actually crosses it and emits
    # INFERRED edges (mxbai vectors run far higher; mock vectors are diffuse).
    cfg = EngineConfig(
        vault_path=vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=16,
        inferred_edge_threshold=0.3,
    )
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=16))
    idx.open()
    try:
        # Index chunks for all pages WITHOUT building the graph/cache, to
        # simulate a cold cache when reindex_page is first called.
        from vault_engine.chunker import chunk_page
        from vault_engine.vault_reader import read_page

        for page_path in sorted(vault.rglob("*.md")):
            pg = read_page(page_path)
            chunks = chunk_page(pg.slug, pg.body)
            idx._index_page_chunks(pg.slug, chunks, IndexReport())
        assert idx._page_vec_cache == {}  # cache is cold

        idx.reindex_page(vault / "wiki" / "topics" / "p0.md")
        # Cold-cache fallback must have populated the cache to full page count
        # and emitted the same INFERRED edges a cold rebuild would.
        assert len(idx._page_vec_cache) == 10  # now fully warm
        inferred = [
            (u, v)
            for u, v, d in idx.graph.graph.edges(data=True)
            if d.get("edge_type") == "INFERRED"
        ]
        assert inferred, "cold-cache reindex emitted no INFERRED edges"
    finally:
        idx.close()
