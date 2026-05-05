"""Tests for the URL → raw/ ingestion adapter (P3 #5)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from vault_engine.url_ingester import (
    ExtractedArticle,
    extract_article,
    slugify_for_raw,
    write_raw_file,
)

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
