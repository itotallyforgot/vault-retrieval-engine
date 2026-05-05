import networkx as nx

from vault_engine.community import communities_summary, compute_communities


def test_compute_communities_returns_node_to_cid_map():
    G = nx.DiGraph()
    G.add_edges_from([("a", "b"), ("b", "c"), ("c", "a")])
    G.add_edges_from([("x", "y"), ("y", "z"), ("z", "x")])
    G.add_edge("c", "x")

    node_to_cid = compute_communities(G)
    assert node_to_cid["a"] == node_to_cid["b"] == node_to_cid["c"]
    assert node_to_cid["x"] == node_to_cid["y"] == node_to_cid["z"]
    assert node_to_cid["a"] != node_to_cid["x"]


def test_communities_summary_groups_by_cid():
    G = nx.DiGraph()
    G.add_edges_from([("a", "b"), ("b", "c"), ("c", "a"), ("x", "y"), ("y", "z"), ("z", "x")])
    summary = communities_summary(G)
    assert len(summary) == 2
    sizes = sorted(c["size"] for c in summary.values())
    assert sizes == [3, 3]


def test_compute_communities_empty_graph_safe():
    G = nx.DiGraph()
    assert compute_communities(G) == {}
