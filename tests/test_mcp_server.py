import asyncio
import re

from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.mcp_server import build_server
from vault_engine.service import Service


def _service(sample_vault, tmp_path, dim: int = 8):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=dim,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=dim))
    svc.start()
    return svc


def _node_titles(text: str) -> list[str]:
    """Pull the ordered node titles out of a `query_graph` payload."""
    return re.findall(r"^NODE\s+(.*?)\s+\[", text, flags=re.MULTILINE)


def test_mcp_server_lists_expected_tools(sample_vault, tmp_path):
    svc = _service(sample_vault, tmp_path)
    try:
        server = build_server(svc)
        tools = asyncio.run(server.list_tools_handler())
        names = {t.name for t in tools}
        assert {
            "query_graph",
            "get_node",
            "get_neighbors",
            "get_community",
            "god_nodes",
            "graph_stats",
            "shortest_path",
        } <= names
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


def test_mcp_server_query_graph_preserves_lookup_intent(sample_vault, tmp_path):
    svc = _service(sample_vault, tmp_path)
    try:
        server = build_server(svc)
        out = asyncio.run(server.call_tool_handler("query_graph", {"question": "Alpha"}))
        assert "Intent: lookup" in out[0].text
    finally:
        svc.stop()


def test_mcp_stdio_subprocess_roundtrip_matches_in_process(sample_vault, tmp_path):
    """Drive a real `vault-engine mcp` child over stdio JSON-RPC.

    Every other test here calls the handlers in-process, so nothing exercises
    serve_stdio(), the `mcp` subcommand, the wire framing, or initialize().
    The CLI mock path builds MockEmbedder(dim=cfg.embedding_dim) == 1024, so
    the in-process ground truth uses the same dim to stay comparable.
    """
    svc = _service(sample_vault, tmp_path, dim=1024)
    try:
        server = build_server(svc)
        out = asyncio.run(server.call_tool_handler("query_graph", {"question": "Alpha"}))
        expected = _node_titles(out[0].text)
    finally:
        svc.stop()
    assert expected, "ground truth returned no NODE lines; comparison would be vacuous"

    params = StdioServerParameters(
        command="vault-engine",
        args=[
            "mcp",
            "--vault",
            str(sample_vault),
            # Keep the child off the user's real ~/.cache/vault-retrieval.
            "--cache",
            str(tmp_path / "mcp-sub"),
            # Without this the child downloads the real ~670MB model.
            "--embedder",
            "mock",
        ],
    )

    async def roundtrip() -> str:
        async with (
            stdio_client(params) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            result = await session.call_tool("query_graph", {"question": "Alpha"})
        assert isinstance(result.content[0], types.TextContent)
        return result.content[0].text

    # No timeout plugin installed; guard the subprocess by hand.
    text = asyncio.run(asyncio.wait_for(roundtrip(), timeout=30))
    assert _node_titles(text) == expected
