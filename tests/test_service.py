import threading
import time

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.router import QueryMode
from vault_engine.service import Service


def test_service_starts_and_stops_cleanly(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    assert svc.is_running()
    svc.stop()
    assert not svc.is_running()


def test_service_indexes_on_first_start(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        assert svc.graph_store.graph.number_of_nodes() > 0
    finally:
        svc.stop()


def test_service_dispatches_query_through_router(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        result = svc.query("anything")
        assert "fused_hits" in result
    finally:
        svc.stop()


def test_service_lookup_intent_uses_vault_slugs_titles_and_aliases(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        for query in ("alpha", "Alpha", "alpha-thing"):
            result = svc.query(query)
            assert result["intent"] == QueryMode.LOOKUP
    finally:
        svc.stop()


def test_service_facade_serializes_reads_against_graph_mutation(sample_vault, tmp_path):
    """Deterministic regression for E1: a facade read must not observe a
    half-mutated graph.

    A patched ``add_similarity_edges`` deletes the entire node set, opens a
    barrier (so a reader is guaranteed to be mid-flight), sleeps, then restores
    the nodes — all under the writer's ``self._lock`` (held for the whole
    reindex). With the facade taking the SAME lock, ``graph_stats`` blocks until
    the graph is whole again and returns the consistent (restored) node count.
    Without the lock it would observe the emptied intermediate state.

    Ordering is forced with events, so the result is deterministic, not a
    probabilistic race.
    """
    import vault_engine.indexer as indexer_mod

    mutating = threading.Event()  # set while the graph is in its broken state
    release = threading.Event()  # reader sets this once it has called the facade

    def hostile_add_edges(graph, vec, threshold):
        g = graph.graph
        saved = list(g.nodes(data=True))
        g.remove_nodes_from([n for n, _ in saved])  # graph now empty (broken)
        mutating.set()
        # Hold the broken state until the reader has entered the facade call.
        release.wait(timeout=5)
        time.sleep(0.05)  # keep the window open a beat longer
        for n, data in saved:  # restore → graph whole again
            g.add_node(n, **data)

    monkeypatch_target = "add_similarity_edges"
    original = getattr(indexer_mod, monkeypatch_target)
    setattr(indexer_mod, monkeypatch_target, hostile_add_edges)
    try:
        cfg = EngineConfig(
            vault_path=sample_vault,
            cache_dir=tmp_path / "cache",
            embedding_model="mock",
            embedding_dim=8,
        )
        svc = Service(cfg, embedder=MockEmbedder(dim=8))
        # start() runs a normal rebuild first (real edges) so we restore the
        # real function for startup, then install the hostile one.
        setattr(indexer_mod, monkeypatch_target, original)
        svc.start()
        expected_nodes = svc.graph_stats()["nodes"]
        setattr(indexer_mod, monkeypatch_target, hostile_add_edges)

        results: list[int] = []
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                svc._on_change(sample_vault / "wiki" / "topics" / "alpha.md")
            except BaseException as exc:  # surface to the test
                errors.append(exc)

        def reader() -> None:
            try:
                mutating.wait(timeout=5)  # wait until the graph is broken
                release.set()  # tell the writer we're about to read
                # If the facade locked correctly, this blocks until the writer
                # finishes and returns the consistent node count.
                results.append(svc.graph_stats()["nodes"])
            except BaseException as exc:  # surface to the test
                errors.append(exc)

        wt = threading.Thread(target=writer)
        rt = threading.Thread(target=reader)
        wt.start()
        rt.start()
        wt.join(timeout=10)
        rt.join(timeout=10)
        svc.stop()

        assert not errors, f"concurrent access raised: {errors!r}"
        # The read must reflect the whole graph, never the emptied mid-state.
        assert results == [expected_nodes], (
            f"facade observed a half-mutated graph: saw {results}, expected [{expected_nodes}]"
        )
    finally:
        setattr(indexer_mod, monkeypatch_target, original)


def test_service_concurrent_query_and_reindex_smoke(sample_vault, tmp_path):
    """Smoke: hammer facade reads in several threads while another thread
    reindexes the same page in a loop. Asserts no exception and no deadlock
    (the join timeouts catch a hang, e.g. if the reentrant RLock were swapped
    for a plain Lock and the MCP-style ``graph_lock()`` + facade nesting
    deadlocked).
    """
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    errors: list[BaseException] = []
    stop = threading.Event()
    alpha = sample_vault / "wiki" / "topics" / "alpha.md"

    def reindex_loop() -> None:
        n = 0
        try:
            while not stop.is_set():
                alpha.write_text(
                    f"---\ntitle: Alpha\naliases: []\ntags: [topic]\nsources: []\n"
                    f"last_updated: 2026-04-{n % 28:02d}\n---\n\n# Alpha\n\nBody rev {n}.\n",
                    encoding="utf-8",
                )
                svc._on_change(alpha)  # drive the watcher callback directly
                n += 1
        except BaseException as exc:  # surface to the test
            errors.append(exc)

    def reader_loop() -> None:
        try:
            while not stop.is_set():
                stats = svc.graph_stats()
                assert isinstance(stats["edge_types"], dict)
                node = svc.graph_node("beta")
                assert node is None or "title" in node
                # Live-graph walk the way MCP handlers do: graph_lock() held
                # across the whole iteration (and re-entered by the facade).
                with svc.graph_lock():
                    g = svc.graph
                    assert sum(1 for _ in g.edges(data=True)) >= 0
        except BaseException as exc:  # surface to the test
            errors.append(exc)

    writer = threading.Thread(target=reindex_loop)
    readers = [threading.Thread(target=reader_loop) for _ in range(4)]
    try:
        writer.start()
        for r in readers:
            r.start()
        threading.Event().wait(0.75)
    finally:
        stop.set()
        writer.join(timeout=10)
        for r in readers:
            r.join(timeout=10)
        svc.stop()

    assert not errors, f"concurrent access raised: {errors!r}"
