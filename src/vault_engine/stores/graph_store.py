"""In-memory NetworkX graph over vault pages.

Nodes  = page slugs (one per page). Aliases are NOT separate nodes; they map
         into the canonical slug via an alias map.
Edges  = wikilink references (source page -> target page). Anchor / display
         portions are stripped before resolution.
"""

from __future__ import annotations

from collections.abc import Iterable

import networkx as nx

from vault_engine.community import annotate_graph_with_communities
from vault_engine.vault_reader import Page, build_alias_map

ALLOWED_EDGE_TYPES: frozenset[str] = frozenset(
    {"EXTRACTED", "INFERRED", "AMBIGUOUS", "DECISION_TRACE"}
)

# Node ``kind`` for decision-trace prototype nodes (gated by
# ``EngineConfig.decision_trace_enabled``). A decision-trace node records a
# conclusion + the reasoning hops behind it; DECISION_TRACE edges chain them so a
# structural walk can answer "why was X concluded?" instead of returning semantic
# neighbors. See [[2026-06-06-decision-traces-context-graphs-neo4j]].
DECISION_TRACE_KIND: str = "decision-trace"
DECISION_TRACE_EDGE: str = "DECISION_TRACE"


class GraphStore:
    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()
        self._alias_map: dict[str, Page] = {}

    def add_node(self, slug: str, **attrs: object) -> None:
        """Add a node with arbitrary attributes. Thin pass-through to nx.DiGraph.add_node."""
        self.graph.add_node(slug, **attrs)

    def add_edge(
        self,
        src: str,
        dst: str,
        *,
        relation: str,
        edge_type: str = "EXTRACTED",
        confidence: float | None = None,
    ) -> None:
        if edge_type not in ALLOWED_EDGE_TYPES:
            raise ValueError(
                f"edge_type must be one of {sorted(ALLOWED_EDGE_TYPES)}, got {edge_type!r}"
            )
        attrs: dict[str, object] = {"relation": relation, "edge_type": edge_type}
        if confidence is not None:
            attrs["confidence"] = float(confidence)
        self.graph.add_edge(src, dst, **attrs)

    def rebuild(self, pages: list[Page]) -> None:
        """Rebuild the graph from a pre-fetched page list.

        ``pages`` is owned by the caller (typically Indexer or Service).
        This method does not walk the vault filesystem itself — that's the
        caller's job. Letting the caller pass a cached page list avoids
        repeated disk walks on per-page reindex paths.
        """
        self.graph = nx.DiGraph()
        self._alias_map = build_alias_map(pages)

        # Nodes first.
        for page in pages:
            self.graph.add_node(
                page.slug,
                title=page.title,
                kind=page.kind,
                aliases=list(page.aliases),
                path=str(page.path),
            )

        # Edges from wikilinks (resolved via alias map).
        for page in pages:
            for link in page.wikilinks:
                target_slug = self._resolve(link)
                if target_slug and target_slug != page.slug:
                    self.add_edge(page.slug, target_slug, relation="wikilink")

    def _resolve(self, name: str) -> str | None:
        page = self._alias_map.get(name.lower())
        return page.slug if page else None

    def canonical(self, name: str) -> str | None:
        return self._resolve(name)

    def has_node(self, slug: str) -> bool:
        return self.graph.has_node(slug)

    def has_edge(self, src: str, dst: str) -> bool:
        return self.graph.has_edge(src, dst)

    def walk(
        self,
        seeds: list[str],
        max_depth: int = 3,
        max_paths: int = 10_000,
    ) -> list[list[str]]:
        """Bounded BFS from each seed, yielding every simple path up to max_depth.

        Replaces a prior O(N^2) implementation that called
        ``nx.all_simple_paths`` per (seed, target) pair — which degenerates
        catastrophically on dense graphs (the engine's INFERRED edges tend
        to produce dense communities).

        Implementation:
        - One BFS per seed. Frontier carries the partial path so simple-path
          semantics are preserved without revisiting a node within the same
          path.
        - Each path of length >= 2 is emitted (excluding the trivial
          ``[seed]`` path).
        - Total returned paths capped at ``max_paths`` to bound memory under
          fan-out from highly-connected nodes; once the cap is hit, returns
          immediately.

        Args:
            seeds: Starting nodes. Missing nodes are skipped, not raised.
            max_depth: Maximum path length (number of edges, not nodes).
            max_paths: Hard cap on returned path count. Defaults to 10k —
                large enough for typical multi-hop UX, small enough to bound
                memory on pathological graphs.

        Returns:
            List of paths (each path = list of node slugs in BFS order).
            May be shorter than the natural BFS output when ``max_paths`` is
            hit.
        """
        from collections import deque

        paths: list[list[str]] = []
        for seed in seeds:
            if not self.graph.has_node(seed):
                continue
            queue: deque[list[str]] = deque([[seed]])
            while queue:
                if len(paths) >= max_paths:
                    return paths
                path = queue.popleft()
                # Emit non-trivial paths (length >= 2).
                if len(path) > 1:
                    paths.append(path)
                if len(path) > max_depth:  # max_depth edges = max_depth+1 nodes
                    continue
                tail = path[-1]
                for neighbor in self.graph.successors(tail):
                    if neighbor in path:
                        # Avoid revisiting within the same path (simple-path semantics).
                        continue
                    queue.append([*path, neighbor])
        return paths

    def add_decision_trace(
        self,
        slug: str,
        *,
        conclusion: str,
        because: list[str] | None = None,
        **attrs: object,
    ) -> None:
        """Add a decision-trace node and chain it to its reasoning predecessors.

        Prototype helper (gated by ``EngineConfig.decision_trace_enabled`` at the
        call site). Creates a ``kind=DECISION_TRACE_KIND`` node carrying the
        ``conclusion`` text, then draws a ``DECISION_TRACE`` edge from each slug
        in ``because`` into this node — modeling "this conclusion follows from
        those prior steps". Predecessor nodes are created on demand so a trace can
        be stitched incrementally.
        """
        self.graph.add_node(
            slug,
            kind=DECISION_TRACE_KIND,
            conclusion=conclusion,
            **attrs,
        )
        for src in because or []:
            if not self.graph.has_node(src):
                self.graph.add_node(src, kind=DECISION_TRACE_KIND)
            self.add_edge(src, slug, relation="because", edge_type=DECISION_TRACE_EDGE)

    def decision_trace_walk(self, seed: str, max_depth: int = 10) -> list[str]:
        """Walk DECISION_TRACE edges *backwards* from a conclusion to its roots.

        Answers "why was ``seed`` concluded?" by following reasoning edges in
        reverse (conclusion -> the steps it depends on), newest-first. Only edges
        whose ``edge_type`` is ``DECISION_TRACE`` are traversed; EXTRACTED /
        INFERRED wikilink edges are ignored, so the path is the reasoning chain,
        not the semantic neighborhood.

        Returns the ordered list of node slugs on the path starting at ``seed``
        (inclusive). A seed with no inbound DECISION_TRACE edge yields ``[seed]``.
        A missing seed yields ``[]``. Cycles and ``max_depth`` both bound the walk.
        """
        if not self.graph.has_node(seed):
            return []
        path: list[str] = [seed]
        seen: set[str] = {seed}
        current = seed
        for _ in range(max_depth):
            predecessors = [
                src
                for src in self.graph.predecessors(current)
                if self.graph.edges[src, current].get("edge_type") == DECISION_TRACE_EDGE
                and src not in seen
            ]
            if not predecessors:
                break
            nxt = predecessors[0]
            path.append(nxt)
            seen.add(nxt)
            current = nxt
        return path

    def orphans(self) -> Iterable[str]:
        """Nodes with zero in-degree (no inbound wikilinks)."""
        for node in self.graph.nodes:
            if self.graph.in_degree(node) == 0:
                yield node

    def neighbors(self, slug: str) -> list[str]:
        return list(self.graph.successors(slug)) if self.graph.has_node(slug) else []

    def finalize_build(self) -> None:
        """Call after all add_node / add_edge are done. Annotates communities on nodes."""
        annotate_graph_with_communities(self.graph)
