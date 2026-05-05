# ADR 0002 — NetworkX for graph storage and traversal

**Status:** Accepted
**Date:** 2026-05-04

## Context

The engine stores a wikilink graph over vault pages, plus inferred similarity edges from page-level vector cosine. The graph supports:

- Direct neighbor lookup (`get_neighbors`)
- Multi-hop walks (`graph_walk`, `multi_hop`)
- Shortest-path queries (`shortest_path` MCP tool)
- Community detection (Louvain via `python-louvain`)
- Most-connected-node ranking (`god_nodes`)

Scale: same as the vec store — hundreds to low-thousands of pages, low-thousands to tens-of-thousands of edges after INFERRED enrichment.

## Decision

**NetworkX** (Python pure-library) as the in-process graph data structure.

## Alternatives considered

| Option | Why rejected |
|---|---|
| **igraph** | Faster C core, smaller memory footprint. Wins on raw throughput for million-node graphs. We don't need it; NetworkX's pure-Python ergonomics + ecosystem fit (Louvain, community libs) are the better tradeoff at our scale. |
| **graph-tool** | Fastest of the in-process options, but Boost-graph C++ dependency makes installation painful (no wheels for some macOS / Windows configs). Defeats single-`pip install` portability. |
| **Neo4j** | Industry standard for graph queries with Cypher. Server process, license model for enterprise features, persistent on-disk store separate from the application. Defeats single-process / single-binary portability. |
| **Custom dict-of-sets** | Considered for the simplest case. Reimplements community detection, BFS, shortest-path from scratch. Not worth saving the NetworkX dependency. |

## Consequences

### Positive

- **Pure Python.** Zero non-Python build steps. Installable everywhere uv runs.
- **Ecosystem fit.** `python-louvain` integrates directly with NetworkX DiGraphs. No glue layer.
- **Algorithm coverage.** BFS, shortest-path, simple-paths, communities, centrality — all in `networkx.algorithms.*`. We don't reimplement.
- **Memory cost is acceptable.** Per-node and per-edge dict overhead scales with vault size; at our scale (sub-10k nodes) this stays well under 100 MB.

### Negative

- **All-in-memory.** Graph rebuilds on every `Indexer.rebuild()` and `reindex_page()` — there's no persistent NetworkX-on-disk format. Mitigated by the cheap rebuild cost at our scale (vault is the source of truth).
- **`nx.all_simple_paths` performance.** Used by `graph_store.walk()`. Worst-case combinatorial. The current v0.1.0 implementation is acceptable at <10k nodes; replacement with bounded BFS is on the v0.2.0 roadmap (see `KNOWN_ISSUES.md`).
- **Pure-Python BFS overhead.** Acceptable for current scale; hot-loop perf would benefit from a C-backed graph at 100k+ nodes.

## Status flags

Revisit if:

- Vault crosses ~50k nodes regularly and graph operations become the latency bottleneck.
- A NetworkX algorithm we depend on disappears or slows in a future release.
- Persistent-on-disk graph access becomes worthwhile (e.g. a service that wants to skip the in-memory rebuild on startup).
