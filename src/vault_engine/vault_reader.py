"""Read pages out of the vault. Parses frontmatter, body, classifies kind."""

import datetime
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import frontmatter

# Hard cap on individual page size to bound memory + embedder cost. Pages
# larger than this are skipped at iter_pages time with a warning, NOT
# silently truncated. Real vault pages are kilobytes; multi-megabyte files
# are usually accidents (binary file mis-renamed, log dump, etc.).
_MAX_PAGE_BYTES = 10 * 1024 * 1024  # 10 MiB


class SlugCollisionError(RuntimeError):
    """Raised when two pages in the vault share the same slug.

    Slug = filename stem, used as the primary key in the vec store and
    the graph. Two pages with the same stem (e.g. ``wiki/topics/foo.md``
    and ``raw/foo.md``) would clobber each other on writes and conflate
    chunks on reads. We refuse to start rather than silently corrupt.
    """


@dataclass
class SkippedPage:
    """A markdown file ``iter_pages`` could not turn into a :class:`Page`.

    Surfaced (not swallowed) so the indexer can log a warning per skip and
    expose a count in the index report / status output. ``reason`` is a short
    human-readable cause, e.g. ``"page too large: ... (12345678 bytes > ...)"``.
    """

    path: Path
    reason: str


@dataclass
class Page:
    path: Path  # absolute path on disk
    slug: str  # filename stem
    kind: str  # "topic" | "source" | "raw" | "other"
    title: str
    aliases: list[str]
    body: str  # markdown body, no frontmatter
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

    Rejects files larger than ``_MAX_PAGE_BYTES`` (10 MiB) — those are almost
    always accidents (binary mis-rename, log dump). Reads with
    ``errors="replace"`` so non-UTF-8 bytes don't crash a full vault walk.
    """
    size = path.stat().st_size
    if size > _MAX_PAGE_BYTES:
        raise ValueError(f"page too large: {path} ({size} bytes > {_MAX_PAGE_BYTES} cap)")
    text = path.read_text(encoding="utf-8", errors="replace")
    fm = frontmatter.loads(text)
    fm_dict: dict[str, Any] = {
        k: v.isoformat() if isinstance(v, datetime.date | datetime.datetime) else v
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


# Inline-code-fence-aware wikilink regex.
# Strips ` ... ` inline code spans before extracting [[...]] tokens.
_INLINE_CODE = re.compile(r"`[^`]*`")
_WIKILINK = re.compile(r"\[\[([^\]\n]+?)\]\]")


def parse_wikilinks(body: str) -> list[str]:
    """Return target slugs of [[wikilinks]] in body, in order, deduped.

    Strips inline `code` spans before matching so backtick-wrapped links don't count.
    Handles `[[target]]`, `[[target|display]]`, `[[target#anchor]]`.
    """
    cleaned = _INLINE_CODE.sub("", body)
    out: list[str] = []
    seen: set[str] = set()
    for match in _WIKILINK.finditer(cleaned):
        token = match.group(1).strip()
        # strip alias-display suffix: target|display
        if "|" in token:
            token = token.split("|", 1)[0].strip()
        # strip anchor suffix: target#section
        if "#" in token:
            token = token.split("#", 1)[0].strip()
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def iter_pages(vault_path: Path, skipped: list[SkippedPage] | None = None) -> list[Page]:
    """Walk the vault for markdown pages and read each.

    Includes wiki/topics/, wiki/sources/, raw/. Skips _ops/, _templates/,
    skills/, and dotfile directories. Populates Page.wikilinks for every
    page. Skips symlinks pointing outside the vault root.

    Args:
        vault_path: Vault root to walk.
        skipped: Optional list. When provided, every markdown file that
            :func:`read_page` rejects (oversize / unreadable, raised as
            ``ValueError``) is appended as a :class:`SkippedPage` instead of
            being silently dropped. The indexer passes a list here so it can
            log a warning per skip and report the count; other callers can
            omit it and keep the plain ``list[Page]`` behaviour. Symlinks that
            escape the vault root are excluded by design (a security filter,
            not a read failure) and are not reported here.

    Raises:
        SlugCollisionError: two pages in the vault share the same stem
            (which would silently clobber each other in the vec store).
    """
    vault_root = vault_path.resolve()
    out: list[Page] = []
    seen_slugs: dict[str, Path] = {}
    for md_path in sorted(vault_path.rglob("*.md")):
        parts = md_path.relative_to(vault_path).parts
        if any(p.startswith(".") for p in parts):
            continue
        if any(p in {"_ops", "_templates", "skills"} for p in parts):
            continue
        # Skip symlinks that escape the vault root (e.g. pointing at /etc/passwd).
        try:
            md_path.resolve().relative_to(vault_root)
        except ValueError:
            continue
        try:
            page = read_page(md_path)
        except ValueError as e:
            # Oversize or otherwise unreadable: skip with no rebuild crash.
            # The size cap is enforced inside read_page. Record the skip so
            # the indexer can log + count it rather than dropping it silently.
            if skipped is not None:
                skipped.append(SkippedPage(path=md_path, reason=str(e)))
            continue
        if page.slug in seen_slugs:
            other = seen_slugs[page.slug]
            raise SlugCollisionError(
                f"slug collision: {md_path} and {other} share stem "
                f"{page.slug!r}; rename one to disambiguate."
            )
        seen_slugs[page.slug] = md_path
        page.wikilinks = parse_wikilinks(page.body)
        out.append(page)
    return out


def build_alias_map(pages: list[Page]) -> dict[str, Page]:
    """Map every alias / title / slug -> Page (lowercased keys for case-insensitive lookup).

    Last-write-wins on conflicts. Duplicate aliases are surfaced separately by
    the `/vault lint` skill, not raised here, so the engine keeps working when
    the vault has imperfect frontmatter.
    """
    alias_map: dict[str, Page] = {}
    for page in pages:
        for name in page.all_names:
            alias_map[name.lower()] = page
    return alias_map
