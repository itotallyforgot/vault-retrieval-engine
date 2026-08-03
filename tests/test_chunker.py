import re

from vault_engine.chunker import chunk_page


def test_chunk_page_splits_on_h1_h2():
    body = "# Top\nIntro.\n\n## A\nA body.\n\n## B\nB body.\n"
    chunks = chunk_page("alpha", body)
    headings = [c.heading for c in chunks]
    assert headings == ["Top", "A", "B"]
    assert chunks[0].text.startswith("# Top")
    assert "A body." in chunks[1].text
    assert "B body." in chunks[2].text


def test_chunk_page_preserves_chunk_index():
    body = "# Top\nx\n\n## A\ny\n"
    chunks = chunk_page("p", body)
    assert [c.idx for c in chunks] == [0, 1]
    assert all(c.page_slug == "p" for c in chunks)


def test_chunk_page_handles_no_headings():
    body = "Just prose.\n"
    chunks = chunk_page("p", body)
    assert len(chunks) == 1
    assert chunks[0].heading == ""
    assert chunks[0].text.strip() == "Just prose."


def test_chunk_page_strips_empty_chunks():
    body = "# Top\n\n\n## A\nbody\n"
    chunks = chunk_page("p", body)
    assert all(c.text.strip() for c in chunks)


def test_chunk_emits_checksum():
    chunks = chunk_page("p", "# H\nbody\n")
    assert chunks[0].checksum  # non-empty hex digest


# --- size cap -------------------------------------------------------------


def _para(words: int, token: str = "word") -> str:
    return " ".join([token] * words)


def test_oversized_section_is_split():
    """A single-H1 body over the cap must not stay one chunk."""
    body = "# Top\n\n" + "\n\n".join(_para(40) for _ in range(10))
    chunks = chunk_page("p", body, max_tokens=100, min_tokens=0)
    assert len(chunks) > 1
    assert all(len(c.text.split()) <= 100 for c in chunks)


def test_split_subchunks_inherit_parent_heading():
    """The PDF page coordinate lives in Chunk.heading. Splitting must not lose it."""
    body = "# Top\n\n" + "\n\n".join(_para(40) for _ in range(10))
    chunks = chunk_page("p", body, max_tokens=100, min_tokens=0)
    assert len(chunks) > 1
    assert {c.heading for c in chunks} == {"Top"}


def test_split_keeps_chunk_indices_contiguous():
    body = "# Top\n\n" + "\n\n".join(_para(40) for _ in range(10))
    chunks = chunk_page("p", body, max_tokens=100, min_tokens=0)
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_single_paragraph_over_cap_is_hard_split():
    """No paragraph boundary to split on: split on words rather than emit oversize."""
    chunks = chunk_page("p", "# Top\n\n" + _para(500), max_tokens=100, min_tokens=0)
    assert len(chunks) > 1
    assert all(len(c.text.split()) <= 100 for c in chunks)


def test_undersized_remainder_is_merged_not_emitted_alone():
    """The tail of a split section, below the min, folds back instead of
    becoming a sliver chunk of its own."""
    body = "# Top\n\n" + _para(100) + "\n\n" + _para(5, "tail")
    chunks = chunk_page("p", body, max_tokens=100, min_tokens=32)
    assert len(chunks) == 1, [c.text.split()[:3] for c in chunks]
    assert "tail" in chunks[0].text
    # The fold is allowed to exceed the max rather than emit a 5-word chunk.
    assert len(chunks[0].text.split()) > 100


def test_undersized_section_is_not_merged_across_a_heading():
    """Merging across sections would relabel the merged-in text with the
    earlier section's heading. On a PDF that silently moves page 2's content
    under `p. 1`, so it is deliberately not done."""
    body = "## p. 1\n\nshort.\n\n## p. 2\n\n" + _para(60)
    chunks = chunk_page("paper", body, max_tokens=200, min_tokens=32)
    assert [c.heading for c in chunks] == ["p. 1", "p. 2"]
    assert "short." in chunks[0].text and "short." not in chunks[1].text


def test_pdf_shaped_body_keeps_page_markers_when_split():
    """One `## p. N` per printed page, one of them oversized. Every chunk must
    still name a page, or the PDF coordinate is gone."""
    body = (
        "## p. 1\n\n" + _para(60) + "\n\n## p. 2\n\n" + _para(600) + "\n\n## p. 3\n\n" + _para(60)
    )
    chunks = chunk_page("paper", body, max_tokens=100, min_tokens=32)
    headings = [c.heading for c in chunks]
    assert all(re.fullmatch(r"p\. \d+", h) for h in headings), headings
    assert headings.count("p. 2") > 1, "the oversized page did not split"
    assert headings == sorted(headings, key=lambda h: int(h.split(". ")[1])), headings
