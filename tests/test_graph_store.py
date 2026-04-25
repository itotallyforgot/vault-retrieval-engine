from pathlib import Path

import pytest

from vault_engine.stores.graph_store import GraphStore
from vault_engine.vault_reader import iter_pages


def test_graph_build_from_vault_links_topics(sample_vault: Path):
    pages = iter_pages(sample_vault)
    g = GraphStore()
    g.rebuild(pages)
    assert g.has_node("alpha")
    assert g.has_node("beta")
    assert g.has_edge("alpha", "beta")  # alpha body links beta
    assert g.has_edge("2026-01-01-alpha-source", "alpha")


def test_graph_resolves_alias_to_canonical(sample_vault: Path):
    pages = iter_pages(sample_vault)
    g = GraphStore()
    g.rebuild(pages)
    # alpha-thing is an alias of alpha
    assert g.canonical("alpha-thing") == "alpha"
    assert g.canonical("ALPHA") == "alpha"
    assert g.canonical("nope") is None


def test_graph_walk_bfs_finds_paths(sample_vault: Path):
    pages = iter_pages(sample_vault)
    g = GraphStore()
    g.rebuild(pages)
    paths = g.walk(seeds=["2026-01-01-alpha-source"], max_depth=2)
    # source -> alpha -> beta
    slug_paths = [[n for n in p] for p in paths]
    assert any(p == ["2026-01-01-alpha-source", "alpha", "beta"] for p in slug_paths)


def test_graph_orphans(sample_vault: Path):
    pages = iter_pages(sample_vault)
    g = GraphStore()
    g.rebuild(pages)
    # In sample vault, beta has no inbound edges other than from alpha,
    # which is fine. raw page 2026-01-01-alpha-raw has no inbound edges.
    orphans = set(g.orphans())
    assert "2026-01-01-alpha-raw" in orphans


@pytest.fixture
def empty_graph_store() -> GraphStore:
    return GraphStore()


def test_add_edge_default_edge_type_extracted(empty_graph_store):
    gs = empty_graph_store
    gs.add_edge("topic-a", "topic-b", relation="references")
    edge = gs.graph.edges["topic-a", "topic-b"]
    assert edge["edge_type"] == "EXTRACTED"
    assert edge["relation"] == "references"


def test_add_edge_explicit_inferred(empty_graph_store):
    gs = empty_graph_store
    gs.add_edge("topic-a", "topic-c", relation="semantic_similar", edge_type="INFERRED", confidence=0.82)
    edge = gs.graph.edges["topic-a", "topic-c"]
    assert edge["edge_type"] == "INFERRED"
    assert edge["confidence"] == 0.82


def test_add_edge_rejects_unknown_edge_type(empty_graph_store):
    gs = empty_graph_store
    with pytest.raises(ValueError, match="edge_type must be one of"):
        gs.add_edge("topic-a", "topic-b", relation="x", edge_type="WHATEVER")
