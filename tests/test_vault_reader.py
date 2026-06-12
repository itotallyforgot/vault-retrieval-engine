from pathlib import Path

import vault_engine.vault_reader as vault_reader
from vault_engine.vault_reader import (
    SkippedPage,
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


def test_iter_pages_allows_vault_under_dot_prefixed_parent(tmp_path: Path):
    vault = tmp_path / ".worktrees" / "branch" / "vault"
    topic_dir = vault / "wiki" / "topics"
    topic_dir.mkdir(parents=True)
    (topic_dir / "alpha.md").write_text("---\ntitle: Alpha\n---\n\n# Alpha\n", encoding="utf-8")

    pages = list(iter_pages(vault))

    assert [p.slug for p in pages] == ["alpha"]


def test_build_alias_map_maps_titles_aliases_slugs(sample_vault):
    pages = list(iter_pages(sample_vault))
    alias_map = build_alias_map(pages)
    assert alias_map["alpha"].slug == "alpha"
    assert alias_map["alpha-thing"].slug == "alpha"
    assert alias_map["beta"].slug == "beta"


def test_iter_pages_reports_oversize_skip(sample_vault, monkeypatch):
    """E4: oversize pages are surfaced via the ``skipped`` out-param, not dropped.

    The good pages still come back; the oversize one lands in ``skipped`` with a
    reason, and is absent from the returned page list.
    """
    # Shrink the cap (well above the ~230-byte fixtures, below the 3 KB page
    # below) so one file trips it without writing a real 10 MiB file.
    monkeypatch.setattr(vault_reader, "_MAX_PAGE_BYTES", 2000)
    big = sample_vault / "wiki" / "topics" / "huge.md"
    big.write_text("---\ntitle: Huge\n---\n\n" + ("x" * 3000) + "\n", encoding="utf-8")

    skipped: list[SkippedPage] = []
    pages = iter_pages(sample_vault, skipped=skipped)

    slugs = {p.slug for p in pages}
    assert "huge" not in slugs  # dropped from index
    assert "alpha" in slugs  # good pages still indexed
    assert [s.path for s in skipped] == [big]
    assert "too large" in skipped[0].reason


def test_iter_pages_without_skipped_arg_stays_silent(sample_vault, monkeypatch):
    """Back-compat: callers that omit ``skipped`` get plain ``list[Page]`` and no error."""
    monkeypatch.setattr(vault_reader, "_MAX_PAGE_BYTES", 2000)
    (sample_vault / "wiki" / "topics" / "huge.md").write_text(
        "---\ntitle: Huge\n---\n\n" + ("x" * 3000) + "\n", encoding="utf-8"
    )
    pages = iter_pages(sample_vault)  # no skipped arg
    assert "huge" not in {p.slug for p in pages}
