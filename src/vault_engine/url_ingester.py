"""URL → raw/ ingestion adapter (P3 #5).

Splits the "scrape an article off the web" job from the "synthesize it
into the wiki" job. This module owns the scrape half:

  fetch_url     — HTTP GET with SSRF guard, size cap, redirect cap
  extract_article — readability/trafilatura-based content extraction
  slugify_for_raw — date-prefixed kebab-case filename
  write_raw_file — assembles frontmatter + body, writes to <vault>/raw/

The output is a markdown file with `ingested: false` frontmatter, ready
for `/vault ingest` (or batch ingest) to merge into the wiki. The engine
intentionally does NOT decide which topic pages to create or how to link
the source — that is judgment work that belongs to the user + LLM
synthesis pass, not to a scraper.
"""

import ipaddress
import re
import socket
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx
import trafilatura

_DEFAULT_USER_AGENT = "vault-engine/0.1 (+https://github.com/itotallyforgot/vault-retrieval-engine)"
_DEFAULT_TIMEOUT_S = 15.0
_DEFAULT_MAX_REDIRECTS = 5
_DEFAULT_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
_ALLOWED_SCHEMES = ("http", "https")
_ALLOWED_CONTENT_TYPES = (
    "text/html",
    "application/xhtml+xml",
    "text/plain",
)


class FetchError(Exception):
    """Raised when a URL fetch fails for any reason (network, security, size)."""


@dataclass
class ExtractedArticle:
    title: str
    body: str
    url: str
    author: str | None
    published: str | None  # raw string from the page, not parsed


def _is_unsafe_host(host: str) -> bool:
    """Return True if host resolves to a private/loopback/link-local/reserved IP.

    Fail-closed semantics: unresolvable hosts return True. This blocks SSRF
    via DNS rebinding by checking the resolved IP at fetch time, not just
    parsing the hostname.
    """
    if not host:
        return True
    # Try direct IP literal first (handles "192.168.0.1", "::1", etc.)
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # Hostname — resolve via DNS.
        try:
            ip = ipaddress.ip_address(socket.gethostbyname(host))
        except (OSError, ValueError):
            return True  # fail closed
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _validate_target(url: str) -> str:
    """Validate URL scheme and resolved host. Returns normalized URL.

    Raises:
        FetchError: on disallowed scheme, missing host, or unsafe-host resolution.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"missing host in url: {url!r}")
    if _is_unsafe_host(host):
        raise FetchError(f"refusing fetch: {host!r} resolves to private/loopback/reserved IP")
    return url


def fetch_url(
    url: str,
    *,
    user_agent: str = _DEFAULT_USER_AGENT,
    timeout: float = _DEFAULT_TIMEOUT_S,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> str:
    """HTTP GET with SSRF, redirect, and size protections.

    - Original URL and every redirect target re-validated against an
      RFC1918 / loopback / link-local / reserved-IP denylist.
    - Redirect count capped (default 5).
    - Response size capped (default 10 MiB).
    - Content-Type checked before reading body.

    Raises:
        FetchError: any disallowed condition (private IP, redirect loop,
            oversize body, non-HTML content-type, network error).
    """
    headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.5",
    }
    current = _validate_target(url)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers=headers,
        ) as client:
            for hop in range(max_redirects + 1):
                resp = client.get(current)
                if resp.is_redirect and hop < max_redirects:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise FetchError("redirect without location header")
                    current = _validate_target(urljoin(current, location))
                    continue
                resp.raise_for_status()
                ctype = resp.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if ctype and not any(
                    ctype.startswith(allowed) for allowed in _ALLOWED_CONTENT_TYPES
                ):
                    raise FetchError(f"unsupported content-type: {ctype!r}")
                clen_header = resp.headers.get("content-length")
                if clen_header is not None:
                    try:
                        clen = int(clen_header)
                    except ValueError as e:
                        raise FetchError(f"malformed content-length: {clen_header!r}") from e
                    if clen > max_bytes:
                        raise FetchError(f"response declares {clen} bytes, max is {max_bytes}")
                body = resp.content
                if len(body) > max_bytes:
                    raise FetchError(f"response is {len(body)} bytes, max is {max_bytes}")
                return resp.text
            raise FetchError(f"redirect cap ({max_redirects}) exceeded")
    except httpx.HTTPError as e:
        raise FetchError(f"http error: {e}") from e


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
        title=_strip_unsafe_chars(title.strip()),
        body=(body or "").strip(),
        url=url,
        author=_strip_unsafe_chars(author.strip()) if author else None,
        published=_strip_unsafe_chars(published.strip()) if published else None,
    )


def _strip_unsafe_chars(s: str) -> str:
    """Drop control chars and bidirectional override codepoints from a scalar.

    Prevents YAML frontmatter injection via crafted page metadata containing
    newlines or bidi override marks.
    """
    return re.sub(r"[\x00-\x1f\x7f‪-‮⁦-⁩]", "", s)


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
    on_date = on_date or datetime.now(tz=UTC).date()
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

    Verifies the resolved target stays inside ``vault_path`` to defend
    against symlinked / traversal-style vault roots.

    Raises:
        FileExistsError: target file exists and ``overwrite`` is False.
        FetchError: target would land outside ``vault_path`` (path traversal
            guard).
    """
    vault_root = vault_path.resolve()
    raw_dir = (vault_root / "raw").resolve()
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Use the clipped_at date for the filename prefix so the file name lines
    # up with the user's local timezone in clipped_at, not UTC.
    on_date = _date_from_clipped_at(clipped_at)
    slug = slugify_for_raw(article.title, on_date=on_date)
    target = (raw_dir / f"{slug}.md").resolve()
    try:
        target.relative_to(vault_root)
    except ValueError as e:
        raise FetchError(f"refusing write: target {target} escapes vault root {vault_root}") from e

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
    target.write_text("\n".join([*fm_lines, "", *body_lines]), encoding="utf-8")
    return target


def _date_from_clipped_at(clipped_at: str) -> date:
    """Parse the YYYY-MM-DD prefix out of an ISO 8601 string. Falls back to
    today (UTC) if the input isn't parseable."""
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", clipped_at)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return datetime.now(tz=UTC).date()


def _yaml_escape(s: str) -> str:
    """Escape a string for use inside a YAML double-quoted scalar.

    Strips control chars + bidi overrides defensively, then escapes
    backslashes and double-quotes.
    """
    s = _strip_unsafe_chars(s)
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
            title=_strip_unsafe_chars(title_override),
            body=article.body,
            url=article.url,
            author=article.author,
            published=article.published,
        )
    clipped_at = datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return write_raw_file(
        vault_path=vault_path,
        article=article,
        clipped_at=clipped_at,
        overwrite=overwrite,
    )
