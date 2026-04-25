"""Long-running engine wrapper. Holds the indexer, watcher, router, stores."""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import SentenceTransformerEmbedder
from vault_engine.indexer import Indexer
from vault_engine.router import Router
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore
from vault_engine.watcher import VaultWatcher

log = logging.getLogger(__name__)


class Service:
    """Assembles all engine components into a coherent long-running process.

    Lifecycle:
        svc = Service(cfg)          # or Service(cfg, embedder=MockEmbedder())
        svc.start()                 # opens stores, full rebuild, starts watcher
        result = svc.query("...")   # route query through Router
        svc.stop()                  # stop watcher, close stores
    """

    def __init__(self, cfg: EngineConfig, *, embedder=None) -> None:
        self.cfg = cfg
        # Use the caller-supplied embedder (e.g. MockEmbedder in tests) or
        # fall back to the production SentenceTransformerEmbedder.
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = SentenceTransformerEmbedder(model_name=cfg.embedding_model)

        # Indexer owns vec_store and graph_store internally.
        self.indexer = Indexer(cfg, self.embedder)

        # Expose the indexer's internal stores as Service attributes so that
        # callers (and tests) can read graph/vec state directly.
        self.vec_store: VecStore = self.indexer.vec
        self.graph_store: GraphStore = self.indexer.graph

        # Router is wired to the *same* store objects as the Indexer so that
        # every rebuild is immediately visible to new queries.
        self.router = Router(
            cfg=cfg,
            embedder=self.embedder,
            vec_store=self.vec_store,
            graph_store=self.graph_store,
        )

        self.watcher: VaultWatcher | None = None
        self._lock = threading.Lock()
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Open stores, run full rebuild, start filesystem watcher."""
        with self._lock:
            if self._running:
                return
            log.info("Service starting; full rebuild over %s", self.cfg.vault_path)
            self.indexer.open()
            self.indexer.rebuild()
            self.watcher = VaultWatcher(self.cfg, on_change=self._on_change)
            self.watcher.start()
            self._running = True
            log.info(
                "Service started; %d nodes indexed",
                self.graph_store.graph.number_of_nodes(),
            )

    def stop(self) -> None:
        """Stop the watcher and close stores.

        Watcher.stop() blocks waiting for the observer thread, which itself
        may be waiting on `_lock` to deliver a pending `_on_change` callback.
        We therefore detach the watcher and DROP the lock before joining the
        observer to avoid a circular wait.
        """
        with self._lock:
            if not self._running:
                return
            watcher = self.watcher
            self.watcher = None
            self._running = False

        if watcher is not None:
            watcher.stop()

        with self._lock:
            self.indexer.close()
            log.info("Service stopped.")

    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Query interface
    # ------------------------------------------------------------------

    def query(self, q: str, *, seed_node: str | None = None, top_k: int = 10) -> dict:
        """Dispatch query through the Router under the service lock."""
        with self._lock:
            return self.router.dispatch(q, seed_node=seed_node, top_k=top_k)

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_change(self, path: Path) -> None:
        """Re-index a single file after a filesystem change (called from watcher thread)."""
        with self._lock:
            log.debug("Reindexing %s", path)
            try:
                self.indexer.reindex_page(path)
            except Exception:
                log.exception("Reindex failed for %s", path)
