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
