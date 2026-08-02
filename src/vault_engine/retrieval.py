"""Retrieval layer: search / expand / source / graph_walk / consolidation.

Composes vec store + graph store + vault filesystem. Stateless aside from
references to indexer.
"""

import hashlib
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


def resolve_in_vault(vault_path: Path, rel: object) -> Path | None:
    """Resolve a vault-relative frontmatter path, refusing escapes.

    Frontmatter path fields (``raw_path``, ``source_artifact``) are
    attacker-influenced — they come from page content, which may be scraped or
    otherwise untrusted — so a crafted ``../../etc/passwd`` must not escape the
    vault root. Returns ``None`` on escape. Existence is the caller's business:
    a missing target is a different fact from an out-of-vault one.
    """
    vault_root = vault_path.resolve()
    target = (vault_root / Path(str(rel))).resolve()
    try:
        target.relative_to(vault_root)
    except ValueError:
        return None
    return target


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
        """Return a page's raw source: its `raw_path` text, or a report on its
        retained original.

        Two different hops, so two different answers:

        - ``raw_path`` points from a derived wiki page at the raw *markdown* it
          was made from, and its text is returned verbatim.
        - ``source_artifact`` (ADR 0006) points from a raw markdown page at a
          *binary* original under ``raw/_originals/``. Those bytes are not text
          and are not dumped; what comes back is where the original is, what it
          is, and whether it still hashes to the ``source_sha256`` recorded at
          ingestion.

        Both keys are attacker-influenced frontmatter, so both are confined to
        the vault root by ``resolve_in_vault`` before anything is touched.
        """
        path = self._path_for_slug(page_slug)
        if path is None:
            return None
        page = read_page(path)
        raw_rel = page.frontmatter.get("raw_path")
        if not raw_rel:
            return self._retained_original_report(page.frontmatter)
        raw_abs = resolve_in_vault(self.cfg.vault_path, raw_rel)
        if raw_abs is None or not raw_abs.exists():
            return None
        return raw_abs.read_text(encoding="utf-8")

    def _retained_original_report(self, fm: dict) -> str | None:
        """Describe the binary original a raw page was extracted from.

        The recorded ``source_sha256`` only means something if something
        re-verifies it (ADR 0006 lists that as a known negative), so this
        re-hashes the retained bytes and reports the verdict rather than
        echoing the frontmatter back.
        """
        artifact_rel = fm.get("source_artifact")
        if not artifact_rel:
            return None
        target = resolve_in_vault(self.cfg.vault_path, artifact_rel)
        if target is None:
            # source_artifact escapes the vault root (path traversal); refuse.
            return None
        recorded = str(fm.get("source_sha256") or "")
        if not target.exists():
            integrity = "MISSING (nothing at that path)"
        elif not recorded:
            integrity = "unverifiable (no source_sha256 recorded)"
        else:
            actual = hashlib.sha256(target.read_bytes()).hexdigest()
            integrity = "ok" if actual == recorded else f"MISMATCH (on disk: {actual})"
        rel = target.relative_to(self.cfg.vault_path.resolve()).as_posix()
        return (
            f"retained original: {rel}\n"
            f"media type: {fm.get('source_media_type') or 'unknown'}\n"
            f"source_sha256: {recorded or 'unrecorded'}\n"
            f"integrity: {integrity}\n"
        )

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
