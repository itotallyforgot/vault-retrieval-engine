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

ALLOWED_EDGE_TYPES: frozenset[str] = frozenset({"EXTRACTED", "INFERRED", "AMBIGUOUS"})


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
    ) -> list[list[str]]:
        """BFS from each seed, returning every path up to max_depth."""
        paths: list[list[str]] = []
        for seed in seeds:
            if not self.graph.has_node(seed):
                continue
            for target in self.graph.nodes:
                if target == seed:
                    continue
                try:
                    for path in nx.all_simple_paths(
                        self.graph, source=seed, target=target, cutoff=max_depth
                    ):
                        paths.append(list(path))
                except nx.NetworkXNoPath:
                    continue
        return paths

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
