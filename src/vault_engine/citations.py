"""Citation chain assembler.

Given retrieval hits, walks chunk -> page -> sources[] frontmatter -> raw_path
to produce structured citations. Silently drops missing pages so a partial
chain still surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.retrieval import Retrieval, SearchHit
from vault_engine.vault_reader import iter_pages, read_page


@dataclass
class Citation:
    page_slug: str
    page_path: str
    title: str
    excerpt: str | None
    raw_path: str | None  # absolute path on disk, if frontmatter declares it


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
        raw_abs = (
            str((self.cfg.vault_path / Path(str(raw_rel))).resolve())
            if raw_rel
            else None
        )
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
