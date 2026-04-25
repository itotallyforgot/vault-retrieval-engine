"""Tests for topology_walk retrieval channel."""
import pytest

from vault_engine.retrieval import topology_walk
from vault_engine.stores.graph_store import GraphStore


def test_topology_walk_returns_neighbors_within_depth(tmp_path):
    gs = GraphStore()
    gs.add_node("A", title="A")
    gs.add_node("B", title="B")
    gs.add_node("C", title="C")
    gs.add_node("D", title="D")
    gs.add_node("X", title="X")
    gs.add_edge("A", "B", relation="links")
    gs.add_edge("B", "C", relation="links")
    gs.add_edge("C", "D", relation="links")
    gs.add_edge("A", "X", relation="links")

    hits = topology_walk(gs, seed="A", depth=2)
    ids = [h.doc_id for h in hits]
    # depth 2 from A reaches B, C, X but not D
    assert "B" in ids and "C" in ids and "X" in ids
    assert "D" not in ids
    for h in hits:
        assert h.channel == "topology"


def test_topology_walk_seed_not_in_graph_returns_empty(empty_graph_store):
    assert topology_walk(empty_graph_store, seed="missing", depth=3) == []
