from vault_engine.citations import build_citation_chain
from vault_engine.stores.graph_store import GraphStore


def test_citation_chain_surfaces_edge_type():
    gs = GraphStore()
    gs.add_node("topic-a", title="Topic A", kind="topic", path="wiki/topics/topic-a.md")
    gs.add_node("topic-b", title="Topic B", kind="topic", path="wiki/topics/topic-b.md")
    gs.add_node("source-s", title="Source S", kind="source", path="raw/source-s.md")
    gs.add_edge("topic-a", "topic-b", relation="references")  # default EXTRACTED
    gs.add_edge("topic-b", "source-s", relation="cites", edge_type="INFERRED", confidence=0.74)

    chain = build_citation_chain(gs, anchor="topic-a", target="source-s")
    assert chain is not None
    assert len(chain.hops) == 2
    assert chain.hops[0].edge_type == "EXTRACTED"
    assert chain.hops[1].edge_type == "INFERRED"
    assert chain.hops[1].confidence == 0.74


def test_citation_chain_no_path_returns_none():
    gs = GraphStore()
    gs.add_node("a", title="A", kind="topic")
    gs.add_node("b", title="B", kind="topic")
    assert build_citation_chain(gs, anchor="a", target="b") is None


def test_citation_chain_respects_max_hops_boundary():
    """A 3-hop path is returned at max_hops=3 and rejected at max_hops=2."""
    gs = GraphStore()
    for slug in ("a", "b", "c", "d"):
        gs.add_node(slug, title=slug.upper(), kind="topic")
    gs.add_edge("a", "b", relation="links")
    gs.add_edge("b", "c", relation="links")
    gs.add_edge("c", "d", relation="links")

    # Path a->b->c->d has 3 hops.
    accepted = build_citation_chain(gs, anchor="a", target="d", max_hops=3)
    assert accepted is not None
    assert len(accepted.hops) == 3

    rejected = build_citation_chain(gs, anchor="a", target="d", max_hops=2)
    assert rejected is None
