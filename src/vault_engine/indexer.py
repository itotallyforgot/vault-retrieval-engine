"""Index orchestration: chunks every page, embeds, upserts vec store, rebuilds graph."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vault_engine.chunker import chunk_page
from vault_engine.config import EngineConfig
from vault_engine.embedder import Embedder
from vault_engine.inference import add_similarity_edges
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore
from vault_engine.vault_reader import iter_pages, parse_wikilinks, read_page


@dataclass
class IndexReport:
    pages_processed: int = 0
    chunks_indexed: int = 0
    chunks_changed: int = 0
    chunks_unchanged: int = 0
    pages_deleted: int = 0


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

    def rebuild(self) -> IndexReport:
        """Re-read every page and re-index from scratch.

        Vec store: incremental — checksum-skip unchanged chunks WITHOUT
        re-encoding (see _index_page_chunks).
        Graph: full rebuild — cheap at vault scale.
        """
        report = IndexReport()
        pages = iter_pages(self.cfg.vault_path)
        for page in pages:
            chunks = chunk_page(page.slug, page.body)
            if chunks:
                self._index_page_chunks(page.slug, chunks, report)
            report.pages_processed += 1

        self.graph.rebuild(pages)
        # P3 #6: enrich with INFERRED similarity edges before community
        # detection so Louvain sees the full graph.
        add_similarity_edges(
            self.graph,
            self.vec,
            threshold=self.cfg.inferred_edge_threshold,
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
        """
        report = IndexReport()
        if not path.exists():
            # Deleted file: drop chunks and rebuild graph from current vault state.
            slug = path.stem
            self.vec.delete_page(slug)
            report.pages_deleted = 1
        else:
            page = read_page(path)
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
        # so this stays even for the deleted-file branch.
        pages = iter_pages(self.cfg.vault_path)
        self.graph.rebuild(pages)
        add_similarity_edges(
            self.graph,
            self.vec,
            threshold=self.cfg.inferred_edge_threshold,
        )
        self.graph.finalize_build()
        return report
