"""MCP stdio server. Built on the official `mcp` SDK.

Tool naming + shapes intentionally mirror Graphify's serve.py so that an agent
which already knows Graphify's surface can drive this engine without relearning.
Vault-specific tools are added on top with distinct names.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from mcp import types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from vault_engine.service import Service

log = logging.getLogger(__name__)


@dataclass
class _ServerHandle:
    """Test-friendly wrapper exposing handlers as plain coroutines."""

    list_tools_handler: Callable
    call_tool_handler: Callable
    server: Server


def build_server(svc: Service) -> _ServerHandle:
    server = Server("vault-engine")

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="query_graph",
                description=(
                    "Multi-hop graph search over the vault knowledge graph. "
                    "Returns relevant nodes + edges as text context."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "question": {"type": "string"},
                        "mode": {"type": "string", "enum": ["bfs", "dfs"], "default": "bfs"},
                        "depth": {"type": "integer", "default": 3},
                        "top_k": {"type": "integer", "default": 10},
                    },
                    "required": ["question"],
                },
            ),
            types.Tool(
                name="get_node",
                description="Get full details for a node by label or ID.",
                inputSchema={
                    "type": "object",
                    "properties": {"label": {"type": "string"}},
                    "required": ["label"],
                },
            ),
            types.Tool(
                name="get_neighbors",
                description="Direct neighbors of a node with edge metadata.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "label": {"type": "string"},
                        "relation_filter": {"type": "string"},
                    },
                    "required": ["label"],
                },
            ),
            types.Tool(
                name="get_community",
                description="All nodes in a community by community ID.",
                inputSchema={
                    "type": "object",
                    "properties": {"community_id": {"type": "integer"}},
                    "required": ["community_id"],
                },
            ),
            types.Tool(
                name="god_nodes",
                description="Most-connected nodes — the core abstractions of the vault graph.",
                inputSchema={
                    "type": "object",
                    "properties": {"top_n": {"type": "integer", "default": 10}},
                },
            ),
            types.Tool(
                name="graph_stats",
                description="Summary stats: node count, edge count, communities, edge-type breakdown.",
                inputSchema={"type": "object", "properties": {}},
            ),
            types.Tool(
                name="shortest_path",
                description="Shortest path between two concepts in the vault graph.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "source": {"type": "string"},
                        "target": {"type": "string"},
                        "max_hops": {"type": "integer", "default": 8},
                    },
                    "required": ["source", "target"],
                },
            ),
            # Vault-specific tools below.
            types.Tool(
                name="find_topic_page",
                description="Locate a topic page in wiki/ matching the query.",
                inputSchema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            ),
            types.Tool(
                name="find_unlinked_references",
                description=(
                    "Find candidate alias matches for a phrase that may already be a wiki topic."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"phrase": {"type": "string"}},
                    "required": ["phrase"],
                },
            ),
            types.Tool(
                name="get_linked_references",
                description="Return inbound wikilinks for a given page (path or title).",
                inputSchema={
                    "type": "object",
                    "properties": {"page_path": {"type": "string"}},
                    "required": ["page_path"],
                },
            ),
        ]

    # --- Tool implementations ---

    def _query_graph(args: dict) -> str:
        result = svc.query(args["question"], top_k=int(args.get("top_k", 10)))
        fused = result["fused_hits"]
        if not fused:
            return "No matching nodes found."
        lines = [
            f"Intent: {result['intent']} | {len(fused)} fused hits",
        ]
        G = svc.graph
        for h in fused:
            node = G.nodes.get(h.doc_id, {})
            lines.append(
                f"NODE {node.get('title', h.doc_id)} "
                f"[community={node.get('community', '')} "
                f"channels={','.join(sorted(set(h.channels)))} "
                f"rrf={h.rrf_score:.4f}]"
            )
        return "\n".join(lines)

    def _get_node(args: dict) -> str:
        label = args["label"]
        G = svc.graph
        if label not in G:
            # fallback: scan by title
            for nid, data in G.nodes(data=True):
                if data.get("title", "").lower() == label.lower():
                    label = nid
                    break
            else:
                return f"No node matching {label!r}."
        d = G.nodes[label]
        return "\n".join(
            [
                f"Node: {d.get('title', label)}",
                f"  ID: {label}",
                f"  Path: {d.get('path', '')}",
                f"  Kind: {d.get('kind', '')}",
                f"  Community: {d.get('community', '')}",
                f"  Degree: {G.degree(label)}",
            ]
        )

    def _get_neighbors(args: dict) -> str:
        label = args["label"]
        rel_filter = (args.get("relation_filter") or "").lower()
        G = svc.graph
        if label not in G:
            return f"No node matching {label!r}."
        lines = [f"Neighbors of {G.nodes[label].get('title', label)}:"]
        for nbr in G.neighbors(label):
            d = G.edges[label, nbr]
            rel = d.get("relation", "")
            if rel_filter and rel_filter not in rel.lower():
                continue
            lines.append(
                f"  --> {G.nodes[nbr].get('title', nbr)} [{rel}] [{d.get('edge_type', '')}]"
            )
        return "\n".join(lines)

    def _get_community(args: dict) -> str:
        cid = int(args["community_id"])
        G = svc.graph
        members = [n for n, d in G.nodes(data=True) if d.get("community") == cid]
        if not members:
            return f"Community {cid} not found."
        lines = [f"Community {cid} ({len(members)} nodes):"]
        for n in members:
            d = G.nodes[n]
            lines.append(f"  {d.get('title', n)} [{d.get('path', '')}]")
        return "\n".join(lines)

    def _god_nodes(args: dict) -> str:
        top_n = int(args.get("top_n", 10))
        G = svc.graph
        ranked = sorted(G.nodes(data=True), key=lambda nd: G.degree(nd[0]), reverse=True)[:top_n]
        lines = ["God nodes (most connected):"]
        for i, (nid, d) in enumerate(ranked, 1):
            lines.append(f"  {i}. {d.get('title', nid)} - {G.degree(nid)} edges")
        return "\n".join(lines)

    def _graph_stats(_args: dict) -> str:
        G = svc.graph
        types_count: dict[str, int] = {"EXTRACTED": 0, "INFERRED": 0, "AMBIGUOUS": 0}
        for _, _, d in G.edges(data=True):
            t = d.get("edge_type", "EXTRACTED")
            types_count[t] = types_count.get(t, 0) + 1
        total = sum(types_count.values()) or 1
        communities = {d.get("community") for _, d in G.nodes(data=True) if "community" in d}
        return (
            f"Nodes: {G.number_of_nodes()}\n"
            f"Edges: {G.number_of_edges()}\n"
            f"Communities: {len(communities)}\n"
            f"EXTRACTED: {round(types_count.get('EXTRACTED', 0) / total * 100)}%\n"
            f"INFERRED: {round(types_count.get('INFERRED', 0) / total * 100)}%\n"
            f"AMBIGUOUS: {round(types_count.get('AMBIGUOUS', 0) / total * 100)}%\n"
        )

    def _shortest_path(args: dict) -> str:
        from vault_engine.citations import build_citation_chain

        chain = build_citation_chain(
            svc.graph_store,
            anchor=args["source"],
            target=args["target"],
            max_hops=int(args.get("max_hops", 8)),
        )
        if chain is None:
            return f"No path between {args['source']!r} and {args['target']!r}."
        lines = [f"Shortest path ({len(chain.hops)} hops):"]
        for h in chain.hops:
            lines.append(
                f"  {h.src} --{h.relation} [{h.edge_type}"
                f"{f' conf={h.confidence:.2f}' if h.confidence is not None else ''}]"
                f"--> {h.dst}"
            )
        return "\n".join(lines)

    def _find_topic_page(args: dict) -> str:
        # Reuse vector-channel; restrict to topic-kind nodes.
        result = svc.query(args["query"], top_k=5)
        topic_hits = [
            h
            for h in result["fused_hits"]
            if svc.graph.nodes.get(h.doc_id, {}).get("kind") == "topic"
        ]
        if not topic_hits:
            return "No topic page matched."
        return "\n".join(
            f"{svc.graph.nodes[h.doc_id].get('path', h.doc_id)}: rrf={h.rrf_score:.4f}"
            for h in topic_hits
        )

    def _find_unlinked_references(args: dict) -> str:
        # Stub for P2: surface candidate matches via alias_map.
        phrase = args["phrase"].lower()
        G = svc.graph
        out = []
        for nid, d in G.nodes(data=True):
            aliases = [a.lower() for a in d.get("aliases", [])]
            if phrase in aliases or phrase == d.get("title", "").lower():
                out.append(f"{d.get('path', nid)}: {d.get('title', nid)}")
        if not out:
            return "No unlinked references candidates."
        return "\n".join(out)

    def _get_linked_references(args: dict) -> str:
        page = args["page_path"]
        G = svc.graph
        target = None
        for nid, d in G.nodes(data=True):
            if d.get("path") == page or nid == page:
                target = nid
                break
        if target is None:
            return f"No node for {page!r}."
        inbound = [u for u, v in G.in_edges(target)]
        if not inbound:
            return f"No inbound wikilinks to {page!r}."
        return "\n".join(
            f"{G.nodes[u].get('path', u)}: {G.nodes[u].get('title', u)}" for u in inbound
        )

    handlers = {
        "query_graph": _query_graph,
        "get_node": _get_node,
        "get_neighbors": _get_neighbors,
        "get_community": _get_community,
        "god_nodes": _god_nodes,
        "graph_stats": _graph_stats,
        "shortest_path": _shortest_path,
        "find_topic_page": _find_topic_page,
        "find_unlinked_references": _find_unlinked_references,
        "get_linked_references": _get_linked_references,
    }

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> list[types.TextContent]:
        handler = handlers.get(name)
        if handler is None:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
        try:
            # Hold the (reentrant) service lock so concurrent watcher
            # reindexes can't mutate the graph mid-iteration. Handlers that
            # internally call svc.query() re-enter the lock safely.
            with svc._lock:
                text = handler(arguments)
            return [types.TextContent(type="text", text=text)]
        except Exception as exc:
            log.exception("MCP tool %s failed; args=%r", name, arguments)
            return [types.TextContent(type="text", text=f"Error executing {name}: {exc}")]

    # Expose for tests
    return _ServerHandle(
        list_tools_handler=list_tools,
        call_tool_handler=call_tool,
        server=server,
    )


def serve_stdio(svc: Service) -> None:
    handle = build_server(svc)

    async def main() -> None:
        async with stdio_server() as streams:
            await handle.server.run(
                streams[0],
                streams[1],
                handle.server.create_initialization_options(),
            )

    asyncio.run(main())
