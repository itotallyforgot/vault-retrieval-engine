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
        # Reentrant: callers (e.g. MCP wrapper) may take the lock and then
        # invoke svc.query() which also takes it. RLock prevents that
        # recursion from deadlocking.
        self._lock = threading.RLock()
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
    # Graph facade — typed accessors for transports (HTTP, MCP).
    #
    # Reaching into ``svc.graph_store.graph`` directly couples transports
    # to the underlying NetworkX DiGraph; these methods expose the most-
    # used patterns through a typed surface. Full GraphQuery facade with
    # all 10+ MCP tool primitives is on the v0.2.0 roadmap; the current
    # surface covers the highest-traffic call sites.
    #
    # Concurrency: the watcher thread mutates the graph (via
    # ``reindex_page`` -> ``graph.rebuild``) under ``self._lock``. Reads here
    # must take the SAME lock or they can observe a half-rebuilt graph
    # (``graph.rebuild`` reassigns ``self.graph`` and re-adds nodes/edges).
    # ``_lock`` is an ``RLock``, so a caller already holding it (e.g. the MCP
    # handler) can call these without deadlocking. The ``graph`` property
    # returns the live DiGraph for callers that iterate it themselves; those
    # callers are responsible for holding ``svc._lock`` across the iteration
    # (the MCP server does this), which is why mutation here is serialized
    # through the same lock.
    # ------------------------------------------------------------------

    def graph_lock(self):
        """Public re-entrant lock guarding graph reads/mutations.

        Transports that iterate the live graph across several statements
        (e.g. the MCP handlers walking ``svc.graph.nodes``) hold this for the
        whole critical section so a concurrent watcher reindex can't mutate
        the graph mid-iteration. Returns the service's ``RLock`` so callers
        don't have to reach into the private ``_lock`` attribute::

            with svc.graph_lock():
                for nid, data in svc.graph.nodes(data=True):
                    ...

        Re-entrant: nesting this with the typed accessors below (which also
        take the lock) does not deadlock.
        """
        return self._lock

    @property
    def graph(self):
        """The underlying NetworkX DiGraph. Prefer the typed accessors
        below where they cover the use case; this property exists so
        transports don't have to traverse ``.graph_store.graph`` and so
        a future GraphQuery facade has a single rename target.

        Callers that iterate the returned graph must hold ``graph_lock()``
        for the duration, otherwise a concurrent watcher reindex can mutate
        it mid-iteration. The typed accessors below do this internally.
        """
        with self._lock:
            return self.graph_store.graph

    def graph_node(self, slug: str) -> dict | None:
        """Return the node attribute dict for ``slug``, or None if absent."""
        with self._lock:
            g = self.graph_store.graph
            if not g.has_node(slug):
                return None
            return dict(g.nodes[slug])

    def graph_stats(self) -> dict:
        """Return summary stats: node count, edge count, communities, edge-type counts."""
        with self._lock:
            g = self.graph_store.graph
            type_counts: dict[str, int] = {}
            for _, _, d in g.edges(data=True):
                t = d.get("edge_type", "EXTRACTED")
                type_counts[t] = type_counts.get(t, 0) + 1
            communities = {d.get("community") for _, d in g.nodes(data=True) if "community" in d}
            return {
                "nodes": g.number_of_nodes(),
                "edges": g.number_of_edges(),
                "communities": len(communities),
                "edge_types": type_counts,
            }

    # ------------------------------------------------------------------
    # Internal callbacks
    # ------------------------------------------------------------------

    def _on_change(self, path: Path) -> None:
        """Re-index a single file after a filesystem change (called from watcher thread).

        Bails out if the service was stopped between the watchdog event firing
        and this callback acquiring the service lock; otherwise we'd call
        ``self.indexer.reindex_page(path)`` against a closed indexer.
        """
        with self._lock:
            if not self._running:
                return
            log.debug("Reindexing %s", path)
            try:
                self.indexer.reindex_page(path)
            except Exception:
                log.exception("Reindex failed for %s", path)
