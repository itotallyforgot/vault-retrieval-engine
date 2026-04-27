"""Citation chain assembler.

Given retrieval hits, walks chunk -> page -> sources[] frontmatter -> raw_path
to produce structured citations. Silently drops missing pages so a partial
chain still surfaces.

Also provides graph-based citation chain building via build_citation_chain.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import networkx as nx

from vault_engine.config import EngineConfig
from vault_engine.retrieval import Retrieval, SearchHit
from vault_engine.vault_reader import iter_pages, read_page

if TYPE_CHECKING:
    from vault_engine.stores.graph_store import GraphStore


@dataclass
class Citation:
    page_slug: str
    page_path: str
    title: str
    excerpt: str | None
    raw_path: str | None  # absolute path on disk, if frontmatter declares it


@dataclass
class CitationHop:
    src: str
    dst: str
    relation: str
    src_path: str | None
    dst_path: str | None
    edge_type: str = "EXTRACTED"
    confidence: float | None = None


@dataclass
class CitationChain:
    anchor: str
    target: str
    hops: list[CitationHop]


class CitationAssembler:
    def __init__(self, cfg: EngineConfig, retrieval: Retrieval) -> None:
        self.cfg = cfg
        self.retrieval = retrieval

    def assemble(self, hits: list[SearchHit]) -> list[Citation]:
        path_by_slug = {p.slug: p.path for p in iter_pages(self.cfg.vault_path)}
        out: list[Citation] = []
        seen: set[str] = set()
        for hit in hits:
            self._walk(hit.page_slug, hit.content, path_by_slug, out, seen)
        return out

    def _walk(
        self,
        slug: str,
        excerpt: str | None,
        path_by_slug: dict[str, Path],
        out: list[Citation],
        seen: set[str],
    ) -> None:
        if slug in seen:
            return
        path = path_by_slug.get(slug)
        if path is None:
            return
        seen.add(slug)
        page = read_page(path)
        raw_rel = page.frontmatter.get("raw_path")
        raw_abs = str((self.cfg.vault_path / Path(str(raw_rel))).resolve()) if raw_rel else None
        out.append(
            Citation(
                page_slug=slug,
                page_path=str(path),
                title=page.title,
                excerpt=excerpt,
                raw_path=raw_abs,
            )
        )
        # Walk into source frontmatter references like "[[2026-01-01-alpha-source]]".
        sources = page.frontmatter.get("sources") or []
        if isinstance(sources, list):
            for entry in sources:
                token = str(entry).strip()
                if token.startswith("[[") and token.endswith("]]"):
                    target = token[2:-2]
                    if "|" in target:
                        target = target.split("|", 1)[0]
                    self._walk(target, None, path_by_slug, out, seen)


def build_citation_chain(
    graph_store: "GraphStore",
    anchor: str,
    target: str,
    *,
    max_hops: int = 6,
) -> CitationChain | None:
    """Build a citation chain from anchor to target node using shortest path.

    Returns None if anchor or target doesn't exist, or if no path exists.
    Returns None if path exceeds max_hops.
    """
    G = graph_store.graph
    if anchor not in G or target not in G:
        return None
    try:
        path_nodes = nx.shortest_path(G, anchor, target)
    except (nx.NetworkXNoPath, nx.NodeNotFound):
        return None
    if len(path_nodes) - 1 > max_hops:
        return None
    hops: list[CitationHop] = []
    for u, v in zip(path_nodes, path_nodes[1:]):
        edata = G.edges[u, v]
        hops.append(
            CitationHop(
                src=u,
                dst=v,
                relation=edata.get("relation", ""),
                src_path=G.nodes[u].get("path"),
                dst_path=G.nodes[v].get("path"),
                edge_type=edata.get("edge_type", "EXTRACTED"),
                confidence=edata.get("confidence"),
            )
        )
    return CitationChain(anchor=anchor, target=target, hops=hops)
