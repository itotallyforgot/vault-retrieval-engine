"""URL → raw/ ingestion adapter (P3 #5).

Splits the "scrape an article off the web" job from the "synthesize it
into the wiki" job. This module owns the scrape half:

  fetch_url     — HTTP GET with a sane UA and timeout
  extract_article — readability/trafilatura-based content extraction
  slugify_for_raw — date-prefixed kebab-case filename
  write_raw_file — assembles frontmatter + body, writes to <vault>/raw/

The output is a markdown file with `ingested: false` frontmatter, ready
for `/vault ingest` (or batch ingest) to merge into the wiki. The engine
intentionally does NOT decide which topic pages to create or how to link
the source — that is judgment work that belongs to the user + LLM
synthesis pass, not to a scraper.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
import trafilatura


_DEFAULT_USER_AGENT = "vault-engine/0.1 (+https://github.com/itotallyforgot/vault-retrieval-engine)"
_DEFAULT_TIMEOUT_S = 15.0


@dataclass
class ExtractedArticle:
    title: str
    body: str
    url: str
    author: str | None
    published: str | None  # raw string from the page, not parsed


def fetch_url(
    url: str,
    *,
    user_agent: str = _DEFAULT_USER_AGENT,
    timeout: float = _DEFAULT_TIMEOUT_S,
) -> str:
    """HTTP GET, returning decoded body text. Raises on 4xx/5xx."""
    resp = httpx.get(
        url,
        headers={"User-Agent": user_agent, "Accept": "text/html,application/xhtml+xml"},
        timeout=timeout,
        follow_redirects=True,
    )
    resp.raise_for_status()
    return resp.text


def extract_article(html: str, *, url: str) -> ExtractedArticle:
    """Pull title, body, author, and published date out of an article page.

    Uses trafilatura for the body (best-in-class boilerplate removal for
    article pages) and falls back to URL-derived defaults for missing
    metadata so the caller always gets a usable ExtractedArticle.
    """
    if not html.strip():
        raise ValueError("extract_article: empty html input")

    metadata = trafilatura.extract_metadata(html, default_url=url)
    body = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_comments=False,
        include_tables=True,
        favor_precision=True,
    )

    if body is None or not body.strip():
        # Trafilatura couldn't find an article body. Fall back to a
        # crude paragraph-only strip so the user at least has something
        # to ingest, rather than failing.
        body = _fallback_text_only(html)

    title = (metadata.title if metadata else None) or _title_from_url(url)
    author = metadata.author if metadata else None
    published = metadata.date if metadata else None

    return ExtractedArticle(
        title=title.strip(),
        body=(body or "").strip(),
        url=url,
        author=(author.strip() if author else None),
        published=(published.strip() if published else None),
    )


def _title_from_url(url: str) -> str:
    """Last-resort title: the path's last segment, prettified."""
    parsed = urlparse(url)
    last = parsed.path.rstrip("/").split("/")[-1] or parsed.netloc
    last = re.sub(r"\.[a-zA-Z0-9]{1,5}$", "", last)  # strip trailing extension
    return last.replace("-", " ").replace("_", " ").strip().title() or "Untitled"


def _fallback_text_only(html: str) -> str:
    """Strip tags very crudely. Only used when trafilatura returns nothing."""
    text = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def slugify_for_raw(title: str, on_date: date | None = None) -> str:
    """Produce a `YYYY-MM-DD-kebab-case-title` slug suitable for `raw/<slug>.md`.

    Truncates very long titles so the resulting filename stays under common
    filesystem limits (255 chars on most filesystems; we cap the slug at 100
    so callers have room for `.md` and any path prefix).
    """
    on_date = on_date or datetime.now(tz=timezone.utc).date()
    slug = title.lower()
    # Drop in-word punctuation (apostrophes, smart quotes) BEFORE collapsing
    # word separators to hyphens — otherwise "what's" becomes "what-s".
    slug = re.sub(r"['‘’“”]", "", slug)
    # Keep alphanumerics + hyphens; collapse anything else into hyphens.
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    if not slug:
        slug = "untitled"
    full = f"{on_date.isoformat()}-{slug}"
    # Cap length at 100 chars to stay well under filesystem limits.
    if len(full) > 100:
        full = full[:100].rstrip("-")
    return full


def write_raw_file(
    vault_path: Path,
    article: ExtractedArticle,
    *,
    clipped_at: str,
    overwrite: bool = False,
) -> Path:
    """Render frontmatter + body and write to `<vault>/raw/<slug>.md`.

    Raises `FileExistsError` if the target file exists and `overwrite` is
    False. Returns the absolute path written.
    """
    raw_dir = vault_path / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Use the clipped_at date for the filename prefix so the file name lines
    # up with the user's local timezone in clipped_at, not UTC.
    on_date = _date_from_clipped_at(clipped_at)
    slug = slugify_for_raw(article.title, on_date=on_date)
    target = raw_dir / f"{slug}.md"

    if target.exists() and not overwrite:
        raise FileExistsError(
            f"raw file already exists: {target} -- pass overwrite=True to replace."
        )

    parsed = urlparse(article.url)
    domain = parsed.netloc or article.url
    quoted_clipped = clipped_at  # already an ISO 8601 string

    fm_lines = [
        "---",
        f'title: "{_yaml_escape(article.title)}"',
        f'source: "{_yaml_escape(article.url)}"',
        f'author: "{_yaml_escape(article.author or "")}"',
        f'published: "{_yaml_escape(article.published or "")}"',
        f'clipped_at: "{_yaml_escape(quoted_clipped)}"',
        'source_type: "article"',
        "tags:",
        '  - "raw"',
        '  - "unprocessed"',
        "ingested: false",
        "---",
    ]
    body_lines = [
        f"# {article.title}",
        "",
        f"> Clipped from [{domain}]({article.url}) on {clipped_at}",
        "",
        "---",
        "",
        article.body.strip(),
        "",
    ]
    target.write_text("\n".join(fm_lines + [""] + body_lines), encoding="utf-8")
    return target


def _date_from_clipped_at(clipped_at: str) -> date:
    """Parse the YYYY-MM-DD prefix out of an ISO 8601 string. Falls back to
    today (UTC) if the input isn't parseable."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", clipped_at)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.now(tz=timezone.utc).date()


def _yaml_escape(s: str) -> str:
    """Escape a string for use inside a YAML double-quoted scalar."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def add_url(
    vault_path: Path,
    url: str,
    *,
    overwrite: bool = False,
    title_override: str | None = None,
) -> Path:
    """End-to-end: fetch URL, extract, write to raw/. Returns the path written.

    The user then runs `/vault ingest <path>` to merge into the wiki — this
    function intentionally does NOT touch the wiki itself. The split keeps
    the engine deterministic (no LLM judgment) and lets the user review
    every scrape before it lands in topic pages.
    """
    html = fetch_url(url)
    article = extract_article(html, url=url)
    if title_override:
        article = ExtractedArticle(
            title=title_override,
            body=article.body,
            url=article.url,
            author=article.author,
            published=article.published,
        )
    clipped_at = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return write_raw_file(
        vault_path=vault_path,
        article=article,
        clipped_at=clipped_at,
        overwrite=overwrite,
    )
