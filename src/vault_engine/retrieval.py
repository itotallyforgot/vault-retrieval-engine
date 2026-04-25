"""Retrieval layer: search / expand / source / graph_walk / consolidation.

Composes vec store + graph store + vault filesystem. Stateless aside from
references to indexer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import Embedder
from vault_engine.indexer import Indexer
from vault_engine.reranker import RankedHit
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecHit
from vault_engine.vault_reader import iter_pages, read_page


@dataclass
class SearchHit:
    page_slug: str
    chunk_idx: int
    content: str
    distance: float


@dataclass
class ConsolidationReport:
    orphan_pages: list[str] = field(default_factory=list)
    duplicate_clusters: list[list[str]] = field(default_factory=list)
    unlinked_mentions: list[tuple[str, str]] = field(default_factory=list)
    # unlinked_mentions = [(page_slug, mentioned_alias)]


class Retrieval:
    def __init__(self, cfg: EngineConfig, indexer: Indexer, embedder: Embedder) -> None:
        self.cfg = cfg
        self.indexer = indexer
        self.embedder = embedder

    # ---- search ----
    def search(self, query: str, k: int | None = None) -> list[SearchHit]:
        k = k or self.cfg.semantic_top_k
        vec = self.embedder.encode([query])[0]
        raw_hits: list[VecHit] = self.indexer.vec.search(vec, top_k=k)
        return [
            SearchHit(
                page_slug=h.page_slug,
                chunk_idx=h.chunk_idx,
                content=h.content,
                distance=h.distance,
            )
            for h in raw_hits
        ]

    # ---- expand ----
    def expand(self, page_slug: str) -> str | None:
        path = self._path_for_slug(page_slug)
        if path is None:
            return None
        return read_page(path).body

    # ---- source ----
    def source(self, page_slug: str) -> str | None:
        """For a wiki/source page, return contents of its `raw_path` if set."""
        path = self._path_for_slug(page_slug)
        if path is None:
            return None
        page = read_page(path)
        raw_rel = page.frontmatter.get("raw_path")
        if not raw_rel:
            return None
        raw_abs = (self.cfg.vault_path / Path(raw_rel)).resolve()
        if not raw_abs.exists():
            return None
        return raw_abs.read_text(encoding="utf-8")

    # ---- consolidation ----
    def consolidation_candidates(self) -> ConsolidationReport:
        report = ConsolidationReport()
        report.orphan_pages = list(self.indexer.graph.orphans())
        # Duplicate clusters: pages whose top-1 semantic neighbor is mutual.
        # Skipped in v1 (placeholder for future enrichment); kept empty.
        # Unlinked mentions: page body contains an alias for another page
        # but no wikilink to it.
        pages = iter_pages(self.cfg.vault_path)
        alias_to_slug: dict[str, str] = {}
        for p in pages:
            for name in p.all_names:
                alias_to_slug.setdefault(name.lower(), p.slug)
        for p in pages:
            body_lower = p.body.lower()
            linked = {l.lower() for l in p.wikilinks}
            for alias, target_slug in alias_to_slug.items():
                if target_slug == p.slug:
                    continue
                if alias in linked:
                    continue
                # Word-boundary match.
                import re
                if re.search(rf"\b{re.escape(alias)}\b", body_lower):
                    report.unlinked_mentions.append((p.slug, alias))
        return report

    # ---- helpers ----
    def _path_for_slug(self, page_slug: str) -> Path | None:
        for page in iter_pages(self.cfg.vault_path):
            if page.slug == page_slug:
                return page.path
        return None


@dataclass
class MultiHopResult:
    seeds: list[str]
    paths: list[list[str]]


class _RetrievalGraphMixin:
    """Inline mixin to keep graph methods grouped — methods attached below."""
    pass


def _retrieval_graph_walk(
    self: "Retrieval",
    seeds: list[str],
    depth: int | None = None,
) -> list[list[str]]:
    depth = depth or self.cfg.graph_max_depth
    return self.indexer.graph.walk(seeds=seeds, max_depth=depth)


def _retrieval_multi_hop(
    self: "Retrieval",
    seed_query: str,
    min_seeds_touched: int = 2,
    depth: int | None = None,
) -> "MultiHopResult":
    """Find seed pages via semantic search, then BFS for paths that touch >= min_seeds."""
    depth = depth or self.cfg.graph_max_depth
    hits = self.search(seed_query, k=self.cfg.semantic_top_k)
    seed_slugs: list[str] = []
    for h in hits:
        if h.page_slug not in seed_slugs:
            seed_slugs.append(h.page_slug)
    all_paths = self.indexer.graph.walk(seeds=seed_slugs, max_depth=depth)
    seed_set = set(seed_slugs)
    filtered = [
        p for p in all_paths
        if len(seed_set.intersection(p)) >= min_seeds_touched
    ]
    return MultiHopResult(seeds=seed_slugs, paths=filtered)


# Attach methods to Retrieval after class definition (keeps single-class import clean).
Retrieval.graph_walk = _retrieval_graph_walk  # type: ignore[attr-defined]
Retrieval.multi_hop = _retrieval_multi_hop    # type: ignore[attr-defined]


def topology_walk(
    graph_store: "GraphStore", seed: str, depth: int = 3
) -> list[RankedHit]:
    """BFS from seed over outbound edges; closer nodes rank higher.

    Follows wikilink direction (page-mentions -> page-mentioned), so the walk
    explores what the seed page references, not what references it. For
    bidirectional reachability, callers should call this twice (once with the
    graph reversed) and merge.

    Score is 1/(distance+1). Filters out the seed itself. Returns RankedHit
    list ordered best-first.
    """
    G = graph_store.graph
    if seed not in G:
        return []
    distances: dict[str, int] = {seed: 0}
    frontier: list[str] = [seed]
    for d in range(1, depth + 1):
        next_frontier: list[str] = []
        for node in frontier:
            for nbr in G.neighbors(node):
                if nbr not in distances:
                    distances[nbr] = d
                    next_frontier.append(nbr)
        frontier = next_frontier
        if not frontier:
            break
    hits = [
        RankedHit(doc_id=node, score=1.0 / (dist + 1), channel="topology")
        for node, dist in distances.items()
        if node != seed
    ]
    hits.sort(key=lambda h: h.score, reverse=True)
    return hits
