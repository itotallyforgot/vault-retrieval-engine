import asyncio

from vault_engine.mcp_server import build_server
from vault_engine.service import Service
from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder


def _service(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    return svc


def test_mcp_server_lists_expected_tools(sample_vault, tmp_path):
    svc = _service(sample_vault, tmp_path)
    try:
        server = build_server(svc)
        tools = asyncio.run(server.list_tools_handler())
        names = {t.name for t in tools}
        assert {"query_graph", "get_node", "get_neighbors", "get_community",
                "god_nodes", "graph_stats", "shortest_path"} <= names
        assert {"find_topic_page", "find_unlinked_references", "get_linked_references"} <= names
    finally:
        svc.stop()


def test_mcp_server_graph_stats_returns_counts(sample_vault, tmp_path):
    svc = _service(sample_vault, tmp_path)
    try:
        server = build_server(svc)
        out = asyncio.run(server.call_tool_handler("graph_stats", {}))
        text = out[0].text
        assert "Nodes:" in text
        assert "EXTRACTED" in text
    finally:
        svc.stop()


def test_mcp_server_query_graph_returns_subgraph_text(sample_vault, tmp_path):
    svc = _service(sample_vault, tmp_path)
    try:
        server = build_server(svc)
        out = asyncio.run(server.call_tool_handler("query_graph", {"question": "anything"}))
        text = out[0].text
        assert "NODE" in text or "EDGE" in text or "No matching" in text
    finally:
        svc.stop()
