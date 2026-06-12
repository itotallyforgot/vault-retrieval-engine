"""Retrieval layer: search / expand / source / graph_walk / consolidation.

Composes vec store + graph store + vault filesystem. Stateless aside from
references to indexer.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import Embedder
from vault_engine.indexer import Indexer
from vault_engine.reranker import RankedHit
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecHit
from vault_engine.vault_reader import iter_pages, read_page

# Aliases shorter than this are skipped from unlinked-mention detection
# (would otherwise produce a flood of false positives on common words).
_MIN_ALIAS_LEN = 3


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


@dataclass
class MultiHopResult:
    seeds: list[str]
    paths: list[list[str]]


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
        """For a wiki/source page, return contents of its `raw_path` if set.

        ``raw_path`` is attacker-influenced frontmatter (it comes from page
        content, which may be scraped or otherwise untrusted), so the resolved
        target is confined to the vault root before reading — a crafted
        ``raw_path: ../../etc/passwd`` must not escape. This mirrors the
        containment guard on the write path in ``url_ingester.write_raw_file``.
        """
        path = self._path_for_slug(page_slug)
        if path is None:
            return None
        page = read_page(path)
        raw_rel = page.frontmatter.get("raw_path")
        if not raw_rel:
            return None
        vault_root = self.cfg.vault_path.resolve()
        raw_abs = (vault_root / Path(str(raw_rel))).resolve()
        try:
            raw_abs.relative_to(vault_root)
        except ValueError:
            # raw_path escapes the vault root (path traversal); refuse.
            return None
        if not raw_abs.exists():
            return None
        return raw_abs.read_text(encoding="utf-8")

    # ---- consolidation ----
    def consolidation_candidates(self) -> ConsolidationReport:
        """Detect orphan pages and unlinked alias mentions across the vault.

        Performance: builds a single compiled alternation regex over all
        eligible aliases (>= ``_MIN_ALIAS_LEN`` chars) and scans each page
        body in one pass. Replaces an earlier O(P^2 * M) per-alias regex
        loop that compiled inside the inner loop.
        """
        report = ConsolidationReport()
        report.orphan_pages = list(self.indexer.graph.orphans())

        pages = iter_pages(self.cfg.vault_path)
        alias_to_slug: dict[str, str] = {}
        for p in pages:
            for name in p.all_names:
                key = name.lower()
                if len(key) >= _MIN_ALIAS_LEN:
                    alias_to_slug.setdefault(key, p.slug)

        if not alias_to_slug:
            return report

        # Compile one alternation regex over all aliases. Sort longest-first
        # so "foo-bar" is preferred over "foo" when both match.
        pattern = re.compile(
            r"\b("
            + "|".join(re.escape(a) for a in sorted(alias_to_slug, key=len, reverse=True))
            + r")\b"
        )
        for p in pages:
            body_lower = p.body.lower()
            linked = {wl.lower() for wl in p.wikilinks}
            seen_pairs: set[tuple[str, str]] = set()
            for match in pattern.finditer(body_lower):
                alias = match.group(1)
                target_slug = alias_to_slug[alias]
                if target_slug == p.slug or alias in linked:
                    continue
                pair = (p.slug, alias)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                report.unlinked_mentions.append(pair)
        return report

    # ---- graph methods (folded in from former _retrieval_graph_walk / _multi_hop) ----
    def graph_walk(self, seeds: list[str], depth: int | None = None) -> list[list[str]]:
        depth = depth or self.cfg.graph_max_depth
        return self.indexer.graph.walk(seeds=seeds, max_depth=depth)

    def multi_hop(
        self,
        seed_query: str,
        min_seeds_touched: int = 2,
        depth: int | None = None,
    ) -> MultiHopResult:
        """Find seed pages via semantic search, then BFS for paths that touch >= min_seeds."""
        depth = depth or self.cfg.graph_max_depth
        hits = self.search(seed_query, k=self.cfg.semantic_top_k)
        seed_slugs: list[str] = []
        for h in hits:
            if h.page_slug not in seed_slugs:
                seed_slugs.append(h.page_slug)
        all_paths = self.indexer.graph.walk(seeds=seed_slugs, max_depth=depth)
        seed_set = set(seed_slugs)
        filtered = [p for p in all_paths if len(seed_set.intersection(p)) >= min_seeds_touched]
        return MultiHopResult(seeds=seed_slugs, paths=filtered)

    # ---- helpers ----
    def _path_for_slug(self, page_slug: str) -> Path | None:
        for page in iter_pages(self.cfg.vault_path):
            if page.slug == page_slug:
                return page.path
        return None


def topology_walk(graph_store: GraphStore, seed: str, depth: int = 3) -> list[RankedHit]:
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
