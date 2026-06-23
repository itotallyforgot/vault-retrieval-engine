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


def _ip_is_unsafe(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for any IP we must never connect to (SSRF denylist)."""
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def _resolve_and_validate(host: str, port: int) -> str:
    """Resolve ``host`` and return a single safe IP literal to connect to.

    Resolves **all** A and AAAA records via ``getaddrinfo`` (the previous
    implementation used ``gethostbyname``, which is IPv4-only and silently
    skipped every AAAA record). EVERY resolved address must pass the denylist:
    if any one is private/loopback/link-local/reserved/etc. we fail closed,
    because a DNS-rebinding attacker only needs one malicious record in the
    set to pivot. The returned IP is what the caller pins the connection to,
    so httpx connects to the exact address we validated instead of
    re-resolving the hostname (which is the TOCTOU window this closes).

    Fail-closed: an unresolvable or empty host raises.

    Raises:
        FetchError: empty host, resolution failure, or any unsafe resolved IP.
    """
    if not host:
        raise FetchError("missing host")
    # IP literal? Validate it directly; no DNS involved.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        if _ip_is_unsafe(literal):
            raise FetchError(f"refusing fetch: {host!r} is a private/loopback/reserved IP")
        return host

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as e:
        raise FetchError(f"cannot resolve host {host!r}: {e}") from e
    resolved: list[str] = []
    for info in infos:
        # sockaddr[0] is the address string for both AF_INET and AF_INET6;
        # str() satisfies the union type the stubs give getaddrinfo.
        addr = str(info[4][0])
        # Strip any IPv6 scope id ("fe80::1%eth0") before parsing.
        addr = addr.split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            continue
        if _ip_is_unsafe(ip):
            raise FetchError(
                f"refusing fetch: {host!r} resolves to private/loopback/reserved IP {addr}"
            )
        resolved.append(addr)
    if not resolved:
        raise FetchError(f"no usable address for host {host!r}")
    # Prefer IPv4 for connection stability; any address is already validated.
    resolved.sort(key=lambda a: ":" in a)
    return resolved[0]


def _validate_target(url: str) -> tuple[str, str, str]:
    """Validate scheme + host and resolve+pin a safe IP for ``url``.

    Returns ``(url, host, pinned_ip)``: the (unchanged) URL, its hostname, and
    the validated IP literal the connection must be pinned to.

    Raises:
        FetchError: disallowed scheme, missing host, or unsafe-host resolution.
    """
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise FetchError(f"unsupported scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise FetchError(f"missing host in url: {url!r}")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    pinned_ip = _resolve_and_validate(host, port)
    return url, host, pinned_ip


def _pinned_url(url: str, host: str, pinned_ip: str) -> str:
    """Rewrite ``url`` so the connection targets ``pinned_ip`` instead of
    re-resolving ``host``. The original host travels in the ``Host`` header and
    TLS SNI (set by the caller), so routing and cert verification still use the
    real hostname; only the socket destination is pinned."""
    parsed = urlparse(url)
    # Bracket IPv6 literals for the authority component.
    host_part = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = f"{host_part}:{parsed.port}" if parsed.port else host_part
    return parsed._replace(netloc=netloc).geturl()


def fetch_url(
    url: str,
    *,
    user_agent: str = _DEFAULT_USER_AGENT,
    timeout: float = _DEFAULT_TIMEOUT_S,
    max_redirects: int = _DEFAULT_MAX_REDIRECTS,
    max_bytes: int = _DEFAULT_MAX_BYTES,
) -> str:
    """HTTP GET with SSRF, redirect, and size protections.

    - The original URL and every redirect target are resolved and validated
      against an RFC1918 / loopback / link-local / reserved-IP denylist
      (all A *and* AAAA records; fail closed if any is unsafe).
    - The connection is **pinned to the validated IP** — httpx connects to the
      exact address that passed the check rather than re-resolving the
      hostname, closing the DNS-rebinding TOCTOU window. The real hostname
      still rides in the ``Host`` header and TLS SNI, so HTTP routing and
      certificate verification are unchanged.
    - Redirect count capped (default 5); each hop is re-validated and re-pinned.
    - Response size capped (default 10 MiB).
    - Content-Type checked before reading body.

    Raises:
        FetchError: any disallowed condition (private IP, redirect loop,
            oversize body, non-HTML content-type, network error).
    """
    base_headers = {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.5",
    }
    current, host, pinned_ip = _validate_target(url)
    try:
        with httpx.Client(
            timeout=timeout,
            follow_redirects=False,
            headers=base_headers,
        ) as client:
            for hop in range(max_redirects + 1):
                # Connect to the validated IP; preserve the real host for
                # routing (Host header) and TLS (sni_hostname → SNI + cert
                # hostname verification).
                resp = client.get(
                    _pinned_url(current, host, pinned_ip),
                    headers={"Host": host},
                    extensions={"sni_hostname": host},
                )
                if resp.is_redirect and hop < max_redirects:
                    location = resp.headers.get("location", "")
                    if not location:
                        raise FetchError("redirect without location header")
                    # Resolve Location against the *real* URL, then re-validate
                    # and re-pin the new target before following it.
                    current, host, pinned_ip = _validate_target(urljoin(current, location))
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
    text = re.sub(r"<script[\s\S]*?</script\s*>", " ", html, flags=re.IGNORECASE)
    text = re.sub(r"<style[\s\S]*?</style\s*>", " ", text, flags=re.IGNORECASE)
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
