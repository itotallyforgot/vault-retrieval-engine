"""Index orchestration: chunks every page, embeds, upserts vec store, rebuilds graph."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from vault_engine.chunker import chunk_page
from vault_engine.config import EngineConfig
from vault_engine.embedder import Embedder
from vault_engine.inference import (
    add_similarity_edges,
    compute_page_vectors,
    page_vector_from_chunks,
)
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore
from vault_engine.vault_reader import SkippedPage, iter_pages, parse_wikilinks, read_page

log = logging.getLogger(__name__)


@dataclass
class IndexReport:
    pages_processed: int = 0
    chunks_indexed: int = 0
    chunks_changed: int = 0
    chunks_unchanged: int = 0
    pages_deleted: int = 0
    # Pages a vault walk could not read (oversize / unreadable) and dropped
    # from the index. Surfaced loudly instead of silently swallowed (E4): the
    # paths feed a per-skip warning log, and ``pages_skipped`` is the count.
    pages_skipped: int = 0
    skipped: list[SkippedPage] = field(default_factory=list)


class Indexer:
    def __init__(self, cfg: EngineConfig, embedder: Embedder) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.vec: VecStore = VecStore(
            db_path=cfg.embeddings_db,
            dim=cfg.embedding_dim,
            model_name=cfg.embedding_model,
        )
        self.graph: GraphStore = GraphStore()
        self._opened = False
        # In-memory cache of per-page mean-pooled vectors, keyed by slug.
        # INFERRED-edge inference needs every page's vector on every reindex;
        # re-fetching them all from SQLite is ~99% of a per-file reindex's cost
        # (measured ~2.3s at 1.5k pages). The cache lets a single-file reindex
        # refresh only the changed slug's vector and reuse the rest, turning a
        # multi-second per-save outage into tens of milliseconds. ``rebuild``
        # repopulates it wholesale; ``reindex_page`` patches one entry.
        self._page_vec_cache: dict[str, np.ndarray] = {}

    def open(self, force_reset: bool = False) -> None:
        """Open the vec store. force_reset=True wipes if model fingerprint mismatches."""
        self.cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        self.vec.open(force_reset=force_reset)
        self._opened = True

    def close(self) -> None:
        if self._opened:
            self.vec.close()
            self._opened = False

    def _index_page_chunks(
        self,
        page_slug: str,
        chunks: list,
        report: IndexReport,
    ) -> None:
        """Encode + upsert ONLY chunks whose checksum differs from what's stored.

        This is the P3 perf-critical path. Pre-P3, embedder.encode() ran on every
        chunk in every page on every rebuild — ~10 minutes against mxbai on a
        warm cache because encoding ran *before* the checksum-skip check inside
        upsert. Now we ask the vec store what's already there, diff against the
        new chunk set, and only encode what actually changed. Chunks that
        disappeared from the new set (e.g. the page lost a section) are
        dropped explicitly so search doesn't surface stale rows.
        """
        existing = self.vec.get_checksums(page_slug)
        new_idxs = {c.idx for c in chunks}

        # Drop chunks that no longer exist in the new chunk set.
        for old_idx in existing.keys() - new_idxs:
            self.vec.delete_chunk(page_slug, old_idx)

        changed_chunks = [c for c in chunks if existing.get(c.idx) != c.checksum]
        if changed_chunks:
            vectors = self.embedder.encode([c.text for c in changed_chunks])
            for chunk, vec in zip(changed_chunks, vectors, strict=True):
                self.vec.upsert(
                    page_slug=chunk.page_slug,
                    chunk_idx=chunk.idx,
                    content=chunk.text,
                    checksum=chunk.checksum,
                    embedding=vec,
                )

        report.chunks_changed += len(changed_chunks)
        report.chunks_unchanged += len(chunks) - len(changed_chunks)
        report.chunks_indexed += len(chunks)

    def _walk_pages(self, report: IndexReport) -> list:
        """Walk the vault, recording + logging any pages skipped as unreadable.

        Centralises the ``iter_pages`` call so every reindex path (full or
        per-page) surfaces oversize/unreadable skips the same way: a warning
        per skip plus the running count on the report (E4).
        """
        skipped: list[SkippedPage] = []
        pages = iter_pages(self.cfg.vault_path, skipped=skipped)
        for s in skipped:
            log.warning("Skipped unreadable page during index walk: %s (%s)", s.path, s.reason)
        report.skipped.extend(skipped)
        report.pages_skipped += len(skipped)
        return pages

    def _refresh_page_vector(self, slug: str) -> None:
        """Recompute and cache one page's mean-pooled vector from the vec store.

        Evicts the slug from the cache when it has no chunks (deleted/empty
        page). Called on per-file reindex so only the changed page touches the
        DB for its vectors; every other page's vector is served from cache.
        """
        chunk_rows = self.vec.iter_chunks_for_page(slug)
        if not chunk_rows:
            self._page_vec_cache.pop(slug, None)
            return
        v = page_vector_from_chunks([row[1] for row in chunk_rows])
        if v is None:
            self._page_vec_cache.pop(slug, None)
        else:
            self._page_vec_cache[slug] = v

    def rebuild(self) -> IndexReport:
        """Re-read every page and re-index from scratch.

        Vec store: incremental — checksum-skip unchanged chunks WITHOUT
        re-encoding (see _index_page_chunks).
        Graph: full rebuild — cheap at vault scale.
        Page-vector cache: rebuilt wholesale from the vec store.
        """
        report = IndexReport()
        pages = self._walk_pages(report)
        for page in pages:
            chunks = chunk_page(page.slug, page.body)
            if chunks:
                self._index_page_chunks(page.slug, chunks, report)
            report.pages_processed += 1

        self.graph.rebuild(pages)
        # Repopulate the page-vector cache from the freshly-indexed store, then
        # feed it to the INFERRED-edge pass. The cache becomes the warm input
        # for subsequent per-file reindexes.
        self._page_vec_cache = compute_page_vectors(self.graph, self.vec)
        # P3 #6: enrich with INFERRED similarity edges before community
        # detection so Louvain sees the full graph.
        add_similarity_edges(
            self.graph,
            self.vec,
            threshold=self.cfg.inferred_edge_threshold,
            page_vecs=self._page_vec_cache,
        )
        self.graph.finalize_build()
        return report

    def reindex_page(self, path: Path) -> IndexReport:
        """Re-index a single page after a file change. Rebuilds graph.

        Reuses the rebuild() encode-skip path, so frontmatter-only edits
        (or any change that leaves body chunks identical) are essentially free.

        Walks the vault once via ``iter_pages`` — the result is reused for
        the graph rebuild. Previous versions walked disk twice on
        ``rebuild()`` paths; this implementation passes the cached page list
        through.

        Performance (E2): the INFERRED-edge pass reuses the in-memory
        page-vector cache and refreshes only the changed slug's vector, instead
        of re-reading every page's chunk vectors from SQLite. The graph
        rebuild + Louvain still run in full (both sub-100ms at vault scale and
        thus left exact, so EXTRACTED edges, link resolution, and community
        labels stay correct with zero staleness), but the dominant cost — the
        full per-reindex vector re-fetch — is eliminated.
        """
        report = IndexReport()
        if not path.exists():
            # Deleted file: drop chunks and rebuild graph from current vault state.
            slug = path.stem
            self.vec.delete_page(slug)
            report.pages_deleted = 1
        else:
            try:
                page = read_page(path)
            except ValueError:
                # The changed file is itself oversize/unreadable. Drop any
                # stale chunks for its slug here; the skip is logged + counted
                # by the _walk_pages call below (the single source of truth for
                # skip accounting, so this oversize file isn't double-reported).
                self.vec.delete_page(path.stem)
                page = None
            if page is None:
                # Oversize/unreadable: the slug still drives the vector-cache
                # refresh below (its chunks were just dropped, so the refresh
                # evicts it from the cache).
                slug = path.stem
            else:
                slug = page.slug
                page.wikilinks = parse_wikilinks(page.body)
                chunks = chunk_page(page.slug, page.body)
                if chunks:
                    self._index_page_chunks(page.slug, chunks, report)
                else:
                    # Empty page (no chunks): drop everything for this slug.
                    self.vec.delete_page(page.slug)
                report.pages_processed = 1

        # Single disk walk for the graph rebuild. iter_pages is the only
        # way the engine knows which pages exist post-rename / post-delete,
        # so this stays even for the deleted-file branch. Skips here are
        # logged + counted via _walk_pages.
        pages = self._walk_pages(report)
        self.graph.rebuild(pages)

        if self._page_vec_cache:
            # Warm cache: refresh ONLY the changed slug's vector from the
            # store; every other page's vector is reused from the cache.
            self._refresh_page_vector(slug)
        else:
            # Cold cache (reindex_page called without a prior rebuild, e.g. in
            # isolation): populate it fully so no page's INFERRED edges are
            # dropped. Correctness over the fast path when there's no cache yet.
            self._page_vec_cache = compute_page_vectors(self.graph, self.vec)

        add_similarity_edges(
            self.graph,
            self.vec,
            threshold=self.cfg.inferred_edge_threshold,
            page_vecs=self._page_vec_cache,
        )
        self.graph.finalize_build()
        return report
