"""Louvain community detection for the in-memory graph."""
from __future__ import annotations

import networkx as nx
import community as community_louvain  # python-louvain package


def compute_communities(graph: nx.Graph) -> dict[str, int]:
    """Return {node_id: community_id}.

    Louvain runs on the undirected projection of the input — direction is
    irrelevant for modularity. Empty graph returns {}.
    """
    if graph.number_of_nodes() == 0:
        return {}
    undirected = graph.to_undirected() if graph.is_directed() else graph
    return community_louvain.best_partition(undirected, random_state=42)


def communities_summary(graph: nx.Graph) -> dict[int, dict[str, object]]:
    """{community_id: {"size": int, "members": list[str]}}."""
    node_to_cid = compute_communities(graph)
    summary: dict[int, dict[str, object]] = {}
    for node, cid in node_to_cid.items():
        bucket = summary.setdefault(cid, {"size": 0, "members": []})
        bucket["members"].append(node)  # type: ignore[union-attr]
        bucket["size"] = len(bucket["members"])  # type: ignore[arg-type]
    return summary


def annotate_graph_with_communities(graph: nx.Graph) -> None:
    """Mutate: write community_id onto every node as `community` attribute."""
    node_to_cid = compute_communities(graph)
    for node, cid in node_to_cid.items():
        graph.nodes[node]["community"] = cid
