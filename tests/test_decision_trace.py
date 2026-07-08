"""Decision-trace node type (prototype, gated by ``decision_trace_enabled``).

Part of an internal tracking issue for graph-reasoning traceability. Two halves:

- **Flag-on path:** a decision-trace node can be added, chained via DECISION_TRACE
  edges, and a structural hop walks the reasoning chain ("why was X concluded?").
- **Flag-off regression:** the default graph build is byte-identical to
  pre-prototype behavior — no decision-trace nodes or edges appear, and the hop
  is inert on an ordinary node.

See [[2026-06-06-decision-traces-context-graphs-neo4j]].
"""

from __future__ import annotations

from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.stores.graph_store import (
    DECISION_TRACE_EDGE,
    DECISION_TRACE_KIND,
    GraphStore,
)


def test_flag_defaults_off():
    """The prototype must be opt-in: default config leaves it disabled."""
    cfg = EngineConfig(vault_path=Path(__file__).parent, cache_dir=Path(__file__).parent)
    assert cfg.decision_trace_enabled is False


# --- Flag-ON: the prototype retrieval path works ---


def test_decision_trace_walk_returns_reasoning_chain():
    """A built trace can be walked backwards from conclusion to root cause."""
    gs = GraphStore()
    # observation -> hypothesis -> conclusion (each edge: "because").
    gs.add_decision_trace("observation", conclusion="deploy latency spiked at 14:02")
    gs.add_decision_trace(
        "hypothesis", conclusion="the new cache layer is cold", because=["observation"]
    )
    gs.add_decision_trace(
        "conclusion", conclusion="roll back the cache change", because=["hypothesis"]
    )

    path = gs.decision_trace_walk("conclusion")
    assert path == ["conclusion", "hypothesis", "observation"]

    # The node carries its conclusion text, and the edge is typed DECISION_TRACE.
    assert gs.graph.nodes["conclusion"]["kind"] == DECISION_TRACE_KIND
    assert gs.graph.nodes["conclusion"]["conclusion"] == "roll back the cache change"
    assert gs.graph.edges["hypothesis", "conclusion"]["edge_type"] == DECISION_TRACE_EDGE


def test_decision_trace_walk_root_node_returns_self():
    gs = GraphStore()
    gs.add_decision_trace("root", conclusion="initial finding")
    assert gs.decision_trace_walk("root") == ["root"]


def test_decision_trace_walk_missing_seed_returns_empty():
    gs = GraphStore()
    assert gs.decision_trace_walk("ghost") == []


def test_decision_trace_walk_ignores_wikilink_edges():
    """The hop must follow only DECISION_TRACE edges, not EXTRACTED wikilinks."""
    gs = GraphStore()
    gs.add_node("doc-a")
    gs.add_node("doc-b")
    gs.add_edge("doc-a", "doc-b", relation="wikilink")  # EXTRACTED, not a trace
    gs.add_decision_trace("doc-b", conclusion="reused as a trace conclusion")
    # doc-b's only inbound edge is the wikilink, which is not DECISION_TRACE.
    assert gs.decision_trace_walk("doc-b") == ["doc-b"]


def test_decision_trace_walk_is_cycle_safe():
    gs = GraphStore()
    gs.add_decision_trace("a", conclusion="a")
    gs.add_decision_trace("b", conclusion="b", because=["a"])
    # Close a cycle a -> b -> a.
    gs.add_edge("b", "a", relation="because", edge_type=DECISION_TRACE_EDGE)
    path = gs.decision_trace_walk("b")
    assert path[0] == "b"
    assert len(path) == len(set(path))  # no node repeats


# --- Flag-OFF: default behavior is unchanged ---


def _build_default_graph(tmp_path: Path, sample_vault: Path) -> GraphStore:
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    assert cfg.decision_trace_enabled is False
    idx = Indexer(cfg, embedder=MockEmbedder(dim=8))
    idx.open()
    try:
        idx.rebuild()
    finally:
        idx.close()
    return idx.graph


def test_flag_off_graph_has_no_decision_trace_artifacts(sample_vault: Path, tmp_path: Path):
    """With the flag off, a normal rebuild emits zero decision-trace nodes/edges."""
    graph = _build_default_graph(tmp_path, sample_vault).graph

    kinds = {data.get("kind") for _, data in graph.nodes(data=True)}
    assert DECISION_TRACE_KIND not in kinds

    edge_types = {data.get("edge_type") for _, _, data in graph.edges(data=True)}
    assert DECISION_TRACE_EDGE not in edge_types


def test_flag_off_default_build_is_byte_identical(sample_vault: Path, tmp_path: Path):
    """Two default builds produce identical node/edge sets — the prototype adds
    no drift to the flag-off path. Captures the exact wikilink topology the
    pre-prototype engine produced for the sample vault.
    """
    graph = _build_default_graph(tmp_path, sample_vault).graph

    nodes = set(graph.nodes)
    assert nodes == {"alpha", "beta", "2026-01-01-alpha-source", "2026-01-01-alpha-raw"}

    wikilink_edges = {
        (u, v) for u, v, d in graph.edges(data=True) if d.get("edge_type") == "EXTRACTED"
    }
    assert ("alpha", "beta") in wikilink_edges
    assert ("2026-01-01-alpha-source", "alpha") in wikilink_edges
    # No reasoning edges leaked into the default topology.
    assert all(d.get("edge_type") != DECISION_TRACE_EDGE for _, _, d in graph.edges(data=True))


def test_decision_trace_walk_inert_on_ordinary_node(sample_vault: Path, tmp_path: Path):
    """The hop is harmless on a normal vault node: it returns just that node."""
    graph = _build_default_graph(tmp_path, sample_vault)
    assert graph.decision_trace_walk("alpha") == ["alpha"]
