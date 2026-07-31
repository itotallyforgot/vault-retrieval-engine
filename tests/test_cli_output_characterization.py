"""Characterization tests for the `search` / `expand` / `source` CLI output.

These pin what the CLI *does* today, not what it ought to do. Before PR #109
there was no coverage of any of these three commands' stdout at all -- only an
assertion that the word "search" appears in `--help` -- so a change to the
retrieval path behind `search` could silently reshape what a user sees and no
test would notice.

PR #110 moved `search` off `Retrieval.search` (vector only) onto
`Router.dispatch` (vector + lexical + topology, fused by RRF). The `search`
assertions below were rewritten in that PR; the `expand` and `source`
assertions were not touched, which is the diff saying that only `search`
changed for a user.

Anything asserted here is a statement of fact about the current build. Where a
test pinned a defect rather than desired behavior, it said so in place rather
than quietly asserting the correct-looking thing. The rich-markup swallow that
ate `[[wikilinks]]` out of excerpts and page bodies was pinned that way and has
since been fixed; those tests now assert the links survive.
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


def test_search_prints_slug_chunk_index_rrf_score_and_channels(tmp_path):
    """One header line per hit: `<slug> #<chunk_idx> rrf=<float> channels=<csv>`.

    The evidence base is three channels fused by RRF, so the score a hit
    reports is its fused RRF score, not any one channel's raw distance. The
    channel list is what makes the fusion inspectable from a terminal.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert result.exit_code == 0, result.output
    headers = [ln for ln in result.stdout.splitlines() if " #" in ln and "rrf=" in ln]
    assert len(headers) == 3
    for line in headers:
        _slug, chunk, rrf, channels = line.split()
        assert chunk.startswith("#")
        assert chunk[1:].isdigit()
        assert rrf.startswith("rrf=")
        float(rrf.removeprefix("rrf="))
        named = channels.removeprefix("channels=").split(",")
        assert named  # never blank
        assert set(named) <= {"vector", "lexical", "topology"}
        assert len(named) == len(set(named))  # deduped, one entry per channel


def test_search_no_longer_reports_a_raw_embedding_distance(tmp_path):
    """`dist=` is gone. It was a vector-channel artifact and there are three now."""
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "dist=" not in result.stdout


def test_search_reports_the_lexical_channel_alongside_the_vector_one(tmp_path):
    """The BM25 leg is visible from the CLI, which is the point of the change.

    Fails if `search` is wired back to a single channel: `Retrieval.search`
    cannot name a channel at all, and a vector-only Router run would never
    print `lexical`.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "channels=vector,lexical" in result.stdout


def test_search_reports_topology_on_a_relational_query(tmp_path):
    """A MULTI_HOP/HYBRID query pulls the graph leg in, and says so.

    This is the assertion that distinguishes "renamed the score field" from
    "actually dispatches through the Router": topology only ever runs on the
    Router path, and only for a query the classifier routes there.
    """
    result = _run(tmp_path, "search", "how is alpha connected to gamma across the vault", "-k", "4")
    assert result.exit_code == 0, result.output
    assert "topology" in result.stdout


def test_search_prints_an_excerpt_and_a_separator_per_hit(tmp_path):
    """Each hit is header line, excerpt (first 200 chars, newlines flattened), `---`."""
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("---") == 3
    # The alpha page's own text is reachable from its excerpt.
    assert "described by alpha-thing" in result.stdout


def test_search_still_emits_no_citation_chain(tmp_path):
    """Unchanged: `search` prints no citation chain.

    `Router.dispatch` returns ranked hits, not assembled chains -- only
    `CitationAssembler` does that, and nothing on the CLI path calls it. The
    README row claiming "citation chains" is corrected in this PR rather than
    the behavior being invented to match it.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "citation" not in result.stdout.lower()


def test_search_excerpt_keeps_wikilinks(tmp_path):
    """Excerpts print verbatim, wikilinks included.

    `console.print` parses `[[beta]]` as a rich markup tag and deletes it, so
    the alpha excerpt used to read "Alpha references [] and ..." on screen
    while reading "Alpha references [[beta]] and ..." on disk. The links are
    the relational structure this engine exists to expose, so eating them in
    the one surface a human reads was the wrong default. Printed with
    markup=False now.
    """
    result = _run(tmp_path, "search", "alpha protocol", "-k", "3")
    assert "Alpha references [[beta]] and is described by alpha-thing" in result.stdout


def test_search_honors_k(tmp_path):
    result = _run(tmp_path, "search", "alpha protocol", "-k", "1")
    assert result.exit_code == 0, result.output
    assert result.stdout.count("rrf=") == 1


# ---------------------------------------------------------------------------
# expand
# ---------------------------------------------------------------------------


def test_expand_prints_the_page_body_not_a_graph_walk(tmp_path):
    """`expand <slug>` prints one page's body. It walks nothing.

    Behavior unchanged by this PR. README used to advertise "Multi-hop graph
    walk from a seed page"; that row was corrected to match this test.
    """
    result = _run(tmp_path, "expand", "alpha")
    assert result.exit_code == 0, result.output
    assert "# Alpha" in result.stdout
    assert "More detail about alpha." in result.stdout
    # No other page's body is reached -- gamma links to alpha, and does not appear.
    assert "Gamma" not in result.stdout
    # Frontmatter is stripped; only the body is printed.
    assert "last_updated" not in result.stdout


def test_expand_body_keeps_wikilinks(tmp_path):
    """Page bodies print verbatim. Same fix as the search excerpt."""
    result = _run(tmp_path, "expand", "alpha")
    assert "Alpha references [[beta]] and is described by alpha-thing" in result.stdout


def test_expand_unknown_slug_exits_1(tmp_path):
    result = _run(tmp_path, "expand", "nonexistent-page")
    assert result.exit_code == 1
    assert "not found" in result.stdout


# ---------------------------------------------------------------------------
# source
# ---------------------------------------------------------------------------


def test_source_prints_one_raw_file_not_a_list_of_source_pages(tmp_path):
    """`source <slug>` prints the contents of that page's `raw_path` file.

    Behavior unchanged by this PR. README used to advertise "Resolve
    `wiki/topics/<page>` -> its source pages"; that row was corrected to match
    this test. `source` takes a *source* page slug, resolves the single
    `raw_path` in its frontmatter, and dumps that file verbatim -- frontmatter
    included.
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
