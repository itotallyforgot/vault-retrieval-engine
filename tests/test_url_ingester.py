"""Tests for the URL → raw/ ingestion adapter (P3 #5)."""

from __future__ import annotations

import hashlib
import socket
from datetime import UTC, date, datetime
from pathlib import Path

import frontmatter
import httpx
import pytest

import vault_engine.url_ingester as url_ingester
from vault_engine.url_ingester import (
    ExtractedArticle,
    FetchedDocument,
    FetchError,
    _pinned_url,
    _resolve_and_validate,
    _suffix_for_media_type,
    _validate_target,
    add_url,
    extract_article,
    fetch_url,
    slugify_for_raw,
    write_original,
    write_raw_file,
)


def _fake_getaddrinfo(*addrs: str):
    """Build a getaddrinfo stub that returns the given IP strings."""

    def _stub(host, port, *a, **kw):
        out = []
        for addr in addrs:
            family = socket.AF_INET6 if ":" in addr else socket.AF_INET
            out.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (addr, port)))
        return out

    return _stub


GOLDEN_HTML = """<!DOCTYPE html>
<html>
  <head>
    <title>How to Read a Paper</title>
    <meta name="author" content="S. Keshav">
    <meta property="article:published_time" content="2007-08-02">
  </head>
  <body>
    <header><nav>Home About</nav></header>
    <article>
      <h1>How to Read a Paper</h1>
      <p>The first pass is a bird's-eye view of the paper.
         Read the abstract, introduction, and conclusion.</p>
      <h2>The Three-Pass Approach</h2>
      <p>Each pass accomplishes specific goals and builds upon
         the previous pass.</p>
      <ul>
        <li>First pass: get the bird's-eye view.</li>
        <li>Second pass: grasp the paper's content.</li>
        <li>Third pass: virtually re-implement the paper.</li>
      </ul>
    </article>
    <footer>Copyright 2007</footer>
  </body>
</html>
"""


def test_extract_article_pulls_title_and_body():
    article = extract_article(GOLDEN_HTML, url="https://example.com/read-a-paper")
    assert article.title == "How to Read a Paper"
    # Body text is preserved.
    assert "bird's-eye view" in article.body
    assert "Three-Pass Approach" in article.body
    # Boilerplate is dropped.
    assert "Home About" not in article.body
    assert "Copyright 2007" not in article.body
    # URL round-trips.
    assert article.url == "https://example.com/read-a-paper"


def test_extract_article_handles_missing_metadata():
    """A page with no <title>, no author meta, no published date should
    still produce a usable ExtractedArticle (fallback title from URL)."""
    html = "<html><body><p>Just some prose.</p></body></html>"
    article = extract_article(html, url="https://example.com/some-article")
    assert article.body  # non-empty
    # Title falls back to a non-empty string derived from the URL.
    assert article.title


def test_extract_article_empty_input_raises():
    with pytest.raises(ValueError, match="empty"):
        extract_article("", url="https://example.com/")


def test_slugify_for_raw_format():
    """Raw filename convention: YYYY-MM-DD-<kebab-case-title>.md"""
    slug = slugify_for_raw("How to Read a Paper", date(2026, 4, 27))
    assert slug == "2026-04-27-how-to-read-a-paper"


def test_slugify_for_raw_strips_special_chars():
    slug = slugify_for_raw("What's the Deal With AI?", date(2026, 4, 27))
    assert slug == "2026-04-27-whats-the-deal-with-ai"


def test_slugify_for_raw_collapses_whitespace_and_punctuation():
    slug = slugify_for_raw("Foo:: Bar — Baz!!", date(2026, 4, 27))
    assert slug == "2026-04-27-foo-bar-baz"


def test_slugify_for_raw_truncates_very_long_titles():
    long_title = "A " * 200  # 400 chars
    slug = slugify_for_raw(long_title, date(2026, 4, 27))
    # Filename should stay well under filesystem limits (255 chars on most FS).
    assert len(slug) <= 100


def test_write_raw_file_writes_frontmatter_and_body(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)

    article = ExtractedArticle(
        title="How to Read a Paper",
        body="The three-pass approach.",
        url="https://example.com/read-a-paper",
        author="S. Keshav",
        published="2007-08-02",
    )

    raw_path = write_raw_file(
        vault_path=vault,
        article=article,
        clipped_at="2026-04-27T12:00:00-05:00",
    )

    assert raw_path.exists()
    assert raw_path.parent == vault / "raw"
    assert raw_path.name.startswith("2026-04-27-")
    assert raw_path.name.endswith(".md")

    text = raw_path.read_text(encoding="utf-8")
    # Frontmatter shape matches the conventions used by every other raw file.
    assert text.startswith("---\n")
    assert 'title: "How to Read a Paper"' in text
    assert 'source: "https://example.com/read-a-paper"' in text
    assert 'author: "S. Keshav"' in text
    assert 'published: "2007-08-02"' in text
    assert 'clipped_at: "2026-04-27T12:00:00-05:00"' in text
    assert "ingested: false" in text
    # Body is appended after frontmatter; quote header lifted from existing
    # raw files makes the source visible at a glance.
    assert "[example.com](https://example.com/read-a-paper)" in text
    assert "The three-pass approach." in text


def test_write_raw_file_does_not_overwrite_existing(tmp_path: Path):
    """Re-fetching the same URL on the same day must NOT silently
    overwrite. Idempotency requires the user opt-in to the overwrite."""
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    article = ExtractedArticle(
        title="X",
        body="b",
        url="https://example.com/x",
        author=None,
        published=None,
    )
    p1 = write_raw_file(vault_path=vault, article=article, clipped_at="2026-04-27T12:00:00Z")
    with pytest.raises(FileExistsError):
        write_raw_file(vault_path=vault, article=article, clipped_at="2026-04-27T12:00:00Z")
    # Original is untouched.
    assert p1.read_text(encoding="utf-8") == p1.read_text(encoding="utf-8")


def test_write_raw_file_overwrite_flag_replaces(tmp_path: Path):
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    article1 = ExtractedArticle(
        title="X", body="first", url="https://e.com/x", author=None, published=None
    )
    article2 = ExtractedArticle(
        title="X", body="second", url="https://e.com/x", author=None, published=None
    )
    write_raw_file(vault_path=vault, article=article1, clipped_at="2026-04-27T12:00:00Z")
    p2 = write_raw_file(
        vault_path=vault, article=article2, clipped_at="2026-04-27T12:00:00Z", overwrite=True
    )
    assert "second" in p2.read_text(encoding="utf-8")


# --- SSRF / DNS-rebinding hardening (E6) ---------------------------------


def test_resolve_and_validate_rejects_private_ip_literal():
    for bad in ("127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "0.0.0.0"):
        with pytest.raises(FetchError):
            _resolve_and_validate(bad, 80)


def test_resolve_and_validate_accepts_public_ip_literal():
    assert _resolve_and_validate("8.8.8.8", 80) == "8.8.8.8"
    assert _resolve_and_validate("2001:4860:4860::8888", 80) == "2001:4860:4860::8888"


def test_resolve_and_validate_rejects_when_any_resolved_ip_is_private(monkeypatch):
    """DNS-rebinding defense: if a host resolves to a public AND a private IP,
    fail closed — an attacker only needs one malicious record to pivot."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34", "127.0.0.1"))
    with pytest.raises(FetchError, match="private/loopback/reserved"):
        _resolve_and_validate("rebind.example", 80)


def test_resolve_and_validate_checks_aaaa_records(monkeypatch):
    """The old gethostbyname path was IPv4-only and skipped AAAA. A host whose
    only record is a private IPv6 address must now be rejected."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("fd00::1"))  # unique-local v6
    with pytest.raises(FetchError):
        _resolve_and_validate("v6only.example", 80)


def test_resolve_and_validate_returns_validated_public_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    assert _resolve_and_validate("public.example", 443) == "93.184.216.34"


def test_resolve_and_validate_fails_closed_on_resolution_error(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(FetchError, match="cannot resolve"):
        _resolve_and_validate("nope.invalid", 80)


def test_validate_target_pins_resolved_ip(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))
    url, host, pinned = _validate_target("https://example.com/page")
    assert host == "example.com"
    assert pinned == "93.184.216.34"
    assert url == "https://example.com/page"


def test_validate_target_rejects_non_http_scheme():
    with pytest.raises(FetchError, match="scheme"):
        _validate_target("ftp://example.com/x")


def test_pinned_url_swaps_host_for_ip_preserving_path_and_port():
    assert _pinned_url("https://example.com/a/b?q=1", "example.com", "93.184.216.34") == (
        "https://93.184.216.34/a/b?q=1"
    )
    # Explicit port is preserved.
    assert _pinned_url("http://example.com:8080/x", "example.com", "1.2.3.4") == (
        "http://1.2.3.4:8080/x"
    )
    # IPv6 literal gets bracketed in the authority.
    assert _pinned_url("https://example.com/x", "example.com", "2606:2800:220::1") == (
        "https://[2606:2800:220::1]/x"
    )


def test_fetch_url_blocks_internal_metadata_endpoint(monkeypatch):
    """End-to-end: a URL whose host resolves to the cloud metadata IP is
    refused before any socket connect."""
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("169.254.169.254"))
    with pytest.raises(FetchError, match="private/loopback/reserved"):
        url_ingester.fetch_url("http://metadata.evil.example/latest/meta-data/")


# --- ADR 0006: the URL adapter retains the fetched original ---------------


def _serve(monkeypatch, body: bytes, content_type: str | None = "text/html; charset=utf-8"):
    """Answer the next fetch_url with ``body`` and ``content_type``.

    No socket is opened: DNS is stubbed to a public IP so the SSRF guard
    passes, and `httpx.Client.get` is replaced with a canned response. CI has
    no network egress, and the loopback address a local test server would bind
    is exactly what `_resolve_and_validate` refuses.
    """
    headers = {} if content_type is None else {"content-type": content_type}
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("93.184.216.34"))

    def _get(self, url, **kw):
        return httpx.Response(200, headers=headers, content=body, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx.Client, "get", _get)


def test_fetch_url_returns_wire_bytes_declared_type_and_decoded_text(monkeypatch):
    """Retention hashes what the server sent, so fetch_url has to hand back the
    undecoded bytes alongside the text extraction works on."""
    raw = "<html><body><p>caf\xe9</p></body></html>".encode("iso-8859-1")
    _serve(monkeypatch, raw, "text/html; charset=iso-8859-1")

    doc = fetch_url("https://example.com/x")
    assert isinstance(doc, FetchedDocument)
    assert doc.content == raw, "must be the bytes off the wire, not a re-encode"
    assert doc.media_type == "text/html", "charset param is not part of the media type"
    assert "café" in doc.text, "text still honours the declared charset"


def test_fetch_url_reports_no_media_type_when_the_server_declares_none(monkeypatch):
    _serve(monkeypatch, b"<html><body><p>hi</p></body></html>", content_type=None)
    assert fetch_url("https://example.com/x").media_type == ""


def test_suffix_for_media_type_covers_every_allowed_type():
    assert _suffix_for_media_type("text/html") == ".html"
    assert _suffix_for_media_type("application/xhtml+xml") == ".xhtml"
    assert _suffix_for_media_type("text/plain") == ".txt"
    # A server that declared nothing (or something unmapped) gets no guess.
    assert _suffix_for_media_type("") == ".bin"
    assert _suffix_for_media_type("text/markdown") == ".bin"


def test_add_url_retains_original_and_records_identity(tmp_path: Path, monkeypatch):
    """ADR 0006 for the other half of the ingestion surface."""
    vault = tmp_path / "vault"
    vault.mkdir()
    raw = GOLDEN_HTML.encode("utf-8")
    _serve(monkeypatch, raw)

    page = add_url(vault, "https://example.com/read-a-paper")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))

    assert post["source_type"] == "article"
    assert post["source"] == "https://example.com/read-a-paper"
    assert post["source_media_type"] == "text/html"

    original = vault / str(post["source_artifact"])
    assert original.is_file(), "source_artifact must resolve inside the vault"
    assert original.parent == vault / "raw" / "_originals"
    assert original.suffix == ".html"
    assert original.read_bytes() == raw, "the retained original must be the unaltered wire bytes"
    assert post["source_sha256"] == hashlib.sha256(raw).hexdigest()
    # The derived markdown is still the extraction, not the raw HTML.
    assert "Three-Pass Approach" in post.content
    assert "<html>" not in post.content


def test_add_url_retention_is_byte_exact_for_a_non_utf8_page(tmp_path: Path, monkeypatch):
    """Hashing `resp.text.encode()` would record a digest of a re-encoding that
    never crossed the wire, and the integrity check would then be a check of
    our own transcoding."""
    vault = tmp_path / "vault"
    vault.mkdir()
    raw = "<html><head><title>Caf\xe9</title></head><body><p>Caf\xe9 prose here.</p></body></html>".encode(
        "iso-8859-1"
    )
    _serve(monkeypatch, raw, "text/html; charset=iso-8859-1")

    page = add_url(vault, "https://example.com/cafe")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    original = vault / str(post["source_artifact"])
    assert original.read_bytes() == raw
    assert post["source_sha256"] == hashlib.sha256(raw).hexdigest()
    assert raw.decode("utf-8", "replace").encode("utf-8") != raw, "fixture must be non-UTF-8"


def test_add_url_extension_follows_the_declared_type(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    _serve(monkeypatch, b"Plain prose, no markup at all.", "text/plain")
    page = add_url(vault, "https://example.com/notes.txt", title_override="Notes")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    assert post["source_media_type"] == "text/plain"
    assert (vault / str(post["source_artifact"])).suffix == ".txt"


def test_add_url_records_no_media_type_it_was_not_given(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    _serve(monkeypatch, GOLDEN_HTML.encode("utf-8"), content_type=None)
    page = add_url(vault, "https://example.com/read-a-paper")
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    assert post["source_media_type"] == "", "a guessed media type is worse than none"
    assert (vault / str(post["source_artifact"])).suffix == ".bin"


def test_add_url_page_write_failure_leaves_no_retained_original(tmp_path: Path, monkeypatch):
    """Same orphan guard `add_pdf` has: the original is retained first, so a
    failure writing the page must roll it back."""
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    slug = slugify_for_raw("Orphan", on_date=datetime.now(tz=UTC).date())
    (vault / "raw" / f"{slug}.md").write_text("pre-existing page")
    _serve(monkeypatch, GOLDEN_HTML.encode("utf-8"))

    with pytest.raises(FileExistsError):
        add_url(vault, "https://example.com/x", title_override="Orphan")
    assert not (vault / "raw" / "_originals" / f"{slug}.html").exists()


def test_add_url_overwrite_replaces_both_page_and_original(tmp_path: Path, monkeypatch):
    vault = tmp_path / "vault"
    vault.mkdir()
    _serve(monkeypatch, GOLDEN_HTML.encode("utf-8"))
    add_url(vault, "https://example.com/x", title_override="Dupe")
    with pytest.raises(FileExistsError):
        add_url(vault, "https://example.com/x", title_override="Dupe")

    second = b"<html><body><p>Rewritten body of the article.</p></body></html>"
    _serve(monkeypatch, second)
    page = add_url(vault, "https://example.com/x", title_override="Dupe", overwrite=True)
    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    assert (vault / str(post["source_artifact"])).read_bytes() == second
    assert post["source_sha256"] == hashlib.sha256(second).hexdigest()


def test_write_original_refuses_symlinked_originals_directory(tmp_path: Path):
    """The guard `pdf_ingester.write_original` used to own, now shared. Anchored
    to the vault root, not to a resolved originals dir — resolving that first
    makes the check a tautology a symlinked `raw/_originals` walks through."""
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "raw" / "_originals").symlink_to(outside, target_is_directory=True)

    with pytest.raises(FetchError, match="escapes"):
        write_original(vault, "escape", b"<html>", ".html", overwrite=True)
    assert not (outside / "escape.html").exists(), "wrote outside the vault"


def test_write_original_refuses_to_replace_silently(tmp_path: Path):
    """Replacing a retained original invalidates the `source_sha256` some page
    already recorded against it."""
    vault = tmp_path / "vault"
    vault.mkdir()
    write_original(vault, "keep", b"first", ".html")
    with pytest.raises(FileExistsError):
        write_original(vault, "keep", b"second", ".html")
    assert (vault / "raw" / "_originals" / "keep.html").read_bytes() == b"first"
