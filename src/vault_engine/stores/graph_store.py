"""In-memory NetworkX graph over vault pages.

Nodes  = page slugs (one per page). Aliases are NOT separate nodes; they map
         into the canonical slug via an alias map.
Edges  = wikilink references (source page -> target page). Anchor / display
         portions are stripped before resolution.
"""
from __future__ import annotations

from collections.abc import Iterable

import networkx as nx

from vault_engine.vault_reader import Page, build_alias_map


class GraphStore:
    def __init__(self) -> None:
        self.graph: nx.DiGraph = nx.DiGraph()
        self._alias_map: dict[str, Page] = {}

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
                    self.graph.add_edge(page.slug, target_slug, kind="wikilink")

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
