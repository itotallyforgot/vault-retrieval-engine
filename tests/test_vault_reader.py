from pathlib import Path

import pytest

from vault_engine.vault_reader import Page, read_page, slug_for_path


def test_read_page_parses_frontmatter_and_body(sample_vault: Path):
    page = read_page(sample_vault / "wiki" / "topics" / "alpha.md")
    assert page.slug == "alpha"
    assert page.title == "Alpha"
    assert page.aliases == ["alpha-thing"]
    assert "alpha references" in page.body.lower()
    assert page.kind == "topic"
    assert page.frontmatter["last_updated"] == "2026-01-01"


def test_read_page_classifies_source_kind(sample_vault: Path):
    page = read_page(sample_vault / "wiki" / "sources" / "2026-01-01-alpha-source.md")
    assert page.kind == "source"
    assert page.slug == "2026-01-01-alpha-source"


def test_read_page_handles_missing_aliases(sample_vault: Path):
    page = read_page(sample_vault / "wiki" / "topics" / "beta.md")
    assert page.aliases == []


def test_slug_for_path_kebab_case(tmp_path: Path):
    p = tmp_path / "wiki" / "topics" / "Some-Page.md"
    p.parent.mkdir(parents=True)
    p.touch()
    assert slug_for_path(p) == "Some-Page"
