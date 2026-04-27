"""Index orchestration: chunks every page, embeds, upserts vec store, rebuilds graph."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vault_engine.chunker import chunk_page
from vault_engine.config import EngineConfig
from vault_engine.embedder import Embedder
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

    def rebuild(self) -> IndexReport:
        """Re-read every page and re-index from scratch.

        Vec store: incremental — checksum-skip unchanged chunks.
        Graph: full rebuild — cheap at vault scale.
        """
        report = IndexReport()
        pages = iter_pages(self.cfg.vault_path)
        for page in pages:
            chunks = chunk_page(page.slug, page.body)
            if chunks:
                vectors = self.embedder.encode([c.text for c in chunks])
                for chunk, vec in zip(chunks, vectors, strict=True):
                    changed = self.vec.upsert(
                        page_slug=chunk.page_slug,
                        chunk_idx=chunk.idx,
                        content=chunk.text,
                        checksum=chunk.checksum,
                        embedding=vec,
                    )
                    if changed:
                        report.chunks_changed += 1
                    else:
                        report.chunks_unchanged += 1
                    report.chunks_indexed += 1
            report.pages_processed += 1

        self.graph.rebuild(pages)
        self.graph.finalize_build()
        return report

    def reindex_page(self, path: Path) -> IndexReport:
        """Re-index a single page after a file change. Rebuilds graph."""
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
            # Drop all prior chunks for this page; the new chunk set replaces them.
            self.vec.delete_page(page.slug)
            if chunks:
                vectors = self.embedder.encode([c.text for c in chunks])
                for chunk, vec in zip(chunks, vectors, strict=True):
                    self.vec.upsert(
                        page_slug=chunk.page_slug,
                        chunk_idx=chunk.idx,
                        content=chunk.text,
                        checksum=chunk.checksum,
                        embedding=vec,
                    )
                    report.chunks_changed += 1
                    report.chunks_indexed += 1
            report.pages_processed = 1

        # Always rebuild the graph after a single-page change — cheap at vault scale.
        self.graph.rebuild(iter_pages(self.cfg.vault_path))
        self.graph.finalize_build()
        return report
