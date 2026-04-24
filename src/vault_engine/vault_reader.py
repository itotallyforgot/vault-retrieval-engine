"""Read pages out of the vault. Parses frontmatter, body, classifies kind."""
from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter


@dataclass
class Page:
    path: Path                       # absolute path on disk
    slug: str                        # filename stem
    kind: str                        # "topic" | "source" | "raw" | "other"
    title: str
    aliases: list[str]
    body: str                        # markdown body, no frontmatter
    frontmatter: dict[str, Any] = field(default_factory=dict)
    wikilinks: list[str] = field(default_factory=list)  # filled by parse_wikilinks

    @property
    def all_names(self) -> list[str]:
        """Title + aliases, lowercased, deduped — used for entity resolution."""
        seen: set[str] = set()
        out: list[str] = []
        for name in [self.title, self.slug, *self.aliases]:
            key = name.lower()
            if key and key not in seen:
                seen.add(key)
                out.append(name)
        return out


def slug_for_path(path: Path) -> str:
    return path.stem


def kind_for_path(path: Path) -> str:
    parts = path.parts
    if "wiki" in parts and "topics" in parts:
        return "topic"
    if "wiki" in parts and "sources" in parts:
        return "source"
    if "raw" in parts:
        return "raw"
    return "other"


def read_page(path: Path) -> Page:
    """Parse a single markdown page from disk.

    YAML dates (`last_updated: 2026-01-01`) are parsed by python-frontmatter
    as `datetime.date` objects. We normalize all date/datetime values to ISO
    strings so downstream consumers see consistent string types.
    """
    text = path.read_text(encoding="utf-8")
    fm = frontmatter.loads(text)
    fm_dict: dict[str, Any] = {
        k: v.isoformat() if isinstance(v, (datetime.date, datetime.datetime)) else v
        for k, v in (fm.metadata or {}).items()
    }
    title = str(fm_dict.get("title") or slug_for_path(path))
    raw_aliases = fm_dict.get("aliases") or []
    aliases = [str(a) for a in raw_aliases] if isinstance(raw_aliases, list) else []
    return Page(
        path=path,
        slug=slug_for_path(path),
        kind=kind_for_path(path),
        title=title,
        aliases=aliases,
        body=fm.content,
        frontmatter=fm_dict,
    )
