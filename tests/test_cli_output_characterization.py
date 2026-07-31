"""Characterization tests for the `search` / `expand` / `source` CLI output.

These pin what the CLI *does* today, not what it ought to do. Before PR #109
there was no coverage of any of these three commands' stdout at all -- only an
assertion that the word "search" appears in `--help` -- so a change to the
retrieval path behind `search` could silently reshape what a user sees and no
test would notice.

Anything asserted here is a statement of fact about the current build. Where the
current behavior is a defect (rich markup eating `[[wikilinks]]` out of every
excerpt and page body, see the `_wikilink` tests below), the test says so in
place rather than quietly asserting the correct-looking thing.
"""

from pathlib import Path

from typer.testing import CliRunner

from vault_engine.cli import app

runner = CliRunner()

FIXTURE_VAULT = Path(__file__).parent / "fixtures" / "sample_vault"


def _run(tmp_path: Path, *argv: str):
    return runner.invoke(
        app,
        [
            "--vault",
            str(FIXTURE_VAULT),
            "--cache",
            str(tmp_path / "cache"),
            "--mock-embedder",
            *argv,
        ],
    )


# ---------------------------------------------------------------------------
# search
# ---------------------------------------------------------------------------


def test_search_prints_slug_chunk_index_and_distance(tmp_path):
    """One header line per hit: `<slug> #<chunk_idx> dist=<float>`.

    The evidence base is vector-only (`Retrieval.search` -> KNN), so the only
    score a hit can report is the raw embedding distance.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert result.exit_code == 0, result.output
    headers = [ln for ln in result.stdout.splitlines() if " #" in ln and "dist=" in ln]
    assert len(headers) == 3
    for line in headers:
        _slug, chunk, dist = line.split()
        assert chunk.startswith("#")
        assert chunk[1:].isdigit()
        assert dist.startswith("dist=")
        float(dist.removeprefix("dist="))


def test_search_prints_an_excerpt_and_a_separator_per_hit(tmp_path):
    """Each hit is header line, excerpt (first 200 chars, newlines flattened), `---`."""
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("---") == 3
    # The alpha page's own text is reachable from its excerpt.
    assert "described by alpha-thing" in result.stdout


def test_search_reports_no_channels_and_no_citation_chain(tmp_path):
    """`search` runs one channel and emits no citations.

    README currently advertises "Top-k semantic results with citation chains".
    It prints neither a citation chain nor any indication of which channel
    produced a hit, because there is only one channel behind it.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "citation" not in result.stdout.lower()
    for channel in ("vector", "lexical", "topology", "rrf"):
        assert channel not in result.stdout.lower()


def test_search_excerpt_drops_wikilinks_to_rich_markup(tmp_path):
    """DEFECT, pinned: `console.print` treats `[[beta]]` as rich markup.

    The alpha excerpt reads "Alpha references [[beta]] and ..." on disk and
    "Alpha references [] and ..." on screen. Not introduced by this work; pinned
    so a future fix is a visible diff instead of an accident.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "Alpha references [] and is described by alpha-thing" in result.stdout
    assert "[[beta]]" not in result.stdout


def test_search_honors_k(tmp_path):
    result = _run(tmp_path, "search", "alpha protocol", "-k", "1")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("dist=") == 1


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_prints_the_page_body_not_a_graph_walk(tmp_path):
    """`expand <slug>` prints one page's body. It walks nothing.

    README currently advertises "Multi-hop graph walk from a seed page".
    """
    result = _run(tmp_path, "expand", "alpha")
    assert result.exit_code == 0, result.output
    assert "# Alpha" in result.stdout
    assert "More detail about alpha." in result.stdout
    # No other page's body is reached -- gamma links to alpha, and does not appear.
    assert "Gamma" not in result.stdout
    # Frontmatter is stripped; only the body is printed.
    assert "last_updated" not in result.stdout


def test_expand_body_drops_wikilinks_to_rich_markup(tmp_path):
    """DEFECT, pinned: same rich-markup swallow as the search excerpt."""
    result = _run(tmp_path, "expand", "alpha")
    assert "Alpha references [] and is described by alpha-thing" in result.stdout
    assert "[[beta]]" not in result.stdout


def test_expand_unknown_slug_exits_1(tmp_path):
    result = _run(tmp_path, "expand", "nonexistent-page")
    assert result.exit_code == 1
    assert "not found" in result.stdout


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


def test_source_prints_one_raw_file_not_a_list_of_source_pages(tmp_path):
    """`source <slug>` prints the contents of that page's `raw_path` file.

    README currently advertises "Resolve `wiki/topics/<page>` -> its source
    pages". It takes a *source* page slug, resolves the single `raw_path` in its
    frontmatter, and dumps that file verbatim -- frontmatter included.
    """
    result = _run(tmp_path, "source", "2026-01-01-alpha-source")
    assert result.exit_code == 0, result.output
    assert "Raw text body." in result.stdout
    # Verbatim: the raw file's own frontmatter is printed too.
    assert "ingested: true" in result.stdout


def test_source_on_a_topic_page_exits_1(tmp_path):
    """A `wiki/topics/` page has no `raw_path`, so the documented input fails."""
    result = _run(tmp_path, "source", "alpha")
    assert result.exit_code == 1
    assert "no raw source for" in result.stdout
