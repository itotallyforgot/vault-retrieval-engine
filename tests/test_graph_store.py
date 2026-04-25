from pathlib import Path

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
