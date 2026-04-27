from pathlib import Path

from vault_engine.vault_reader import (
    build_alias_map,
    iter_pages,
    parse_wikilinks,
    read_page,
    slug_for_path,
)


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


def test_parse_wikilinks_finds_targets():
    body = "See [[alpha]], [[beta|the beta page]], and [[gamma#section]]."
    links = parse_wikilinks(body)
    assert links == ["alpha", "beta", "gamma"]


def test_parse_wikilinks_ignores_code_fences():
    body = "Inline `[[not-a-link]]`. Then real [[real-link]]."
    links = parse_wikilinks(body)
    assert "real-link" in links
    assert "not-a-link" not in links


def test_iter_pages_walks_wiki_and_raw(sample_vault):
    pages = list(iter_pages(sample_vault))
    slugs = sorted(p.slug for p in pages)
    assert slugs == [
        "2026-01-01-alpha-raw",
        "2026-01-01-alpha-source",
        "alpha",
        "beta",
    ]


def test_build_alias_map_maps_titles_aliases_slugs(sample_vault):
    pages = list(iter_pages(sample_vault))
    alias_map = build_alias_map(pages)
    assert alias_map["alpha"].slug == "alpha"
    assert alias_map["alpha-thing"].slug == "alpha"
    assert alias_map["beta"].slug == "beta"
