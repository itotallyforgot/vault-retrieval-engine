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
