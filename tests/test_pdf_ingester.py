"""Tests for the local-file PDF → raw/ ingestion adapter (ADR 0006)."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

import frontmatter
import pytest
from pypdf import PdfWriter

from vault_engine.chunker import chunk_page
from vault_engine.pdf_ingester import (
    _MAX_BODY_BYTES,
    MEDIA_TYPE,
    PdfIngestError,
    add_pdf,
    extract_pdf_markdown,
    write_original,
)
from vault_engine.url_ingester import slugify_for_raw
from vault_engine.vault_reader import _MAX_PAGE_BYTES, SkippedPage

FIXTURE = Path(__file__).parent / "fixtures" / "two_page.pdf"


def _pdf_bytes(pages: list[list[str]]) -> bytes:
    """Build a PDF whose page N carries exactly ``pages[N-1]`` as text lines.

    pypdf has no text-drawing API, so hostile text layers are authored as raw
    PDF bytes (same technique as `tests/fixtures/two_page.pdf`, which is a
    hand-written PDF). An empty line list produces a page with no text layer.
    """

    def esc(s: str) -> str:
        return s.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")

    n = len(pages)
    page_ids = [3 + 2 * i for i in range(n)]
    content_ids = [4 + 2 * i for i in range(n)]
    font_id = 3 + 2 * n

    objects: dict[int, bytes] = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids = " ".join(f"{o} 0 R" for o in page_ids)
    objects[2] = f"<< /Type /Pages /Kids [{kids}] /Count {n} >>".encode()
    for i, lines in enumerate(pages):
        objects[page_ids[i]] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 400 400] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> "
            f"/Contents {content_ids[i]} 0 R >>"
        ).encode()
        drawn = "".join(f"({esc(line)}) Tj T*\n" for line in lines)
        stream = f"BT /F1 12 Tf 20 360 Td 14 TL\n{drawn}ET".encode()
        objects[content_ids[i]] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"
        )
    objects[font_id] = b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"

    out = bytearray(b"%PDF-1.4\n")
    offsets: dict[int, int] = {}
    for num in sorted(objects):
        offsets[num] = len(out)
        out += f"{num} 0 obj\n".encode() + objects[num] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for num in range(1, size):
        out += f"{offsets[num]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{xref_at}\n%%EOF\n".encode()
    return bytes(out)


def _pdf_file(tmp_path: Path, pages: list[list[str]], name: str = "crafted.pdf") -> Path:
    dest = tmp_path / name
    dest.write_bytes(_pdf_bytes(pages))
    return dest


def _blank_pdf(tmp_path: Path, name: str = "scanned.pdf") -> Path:
    """A PDF with pages but no text layer — the scanned-document shape."""
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    dest = tmp_path / name
    with dest.open("wb") as fh:
        writer.write(fh)
    return dest


def test_extract_emits_one_section_per_page():
    md = extract_pdf_markdown(FIXTURE)
    assert re.findall(r"^## p\. \d+$", md, flags=re.MULTILINE) == ["## p. 1", "## p. 2"]
    assert "Alpha page one text." in md
    assert "Beta page two text." in md


def test_page_markers_survive_the_chunker():
    """The whole point of `## p. N`: chunk_page keeps the heading in the text."""
    chunks = chunk_page("paper", extract_pdf_markdown(FIXTURE))
    assert [c.heading for c in chunks] == ["p. 1", "p. 2"]
    assert chunks[0].text.startswith("## p. 1")
    assert "Alpha page one text." in chunks[0].text


def test_no_text_layer_is_refused(tmp_path: Path):
    scanned = _blank_pdf(tmp_path)
    with pytest.raises(PdfIngestError) as exc:
        extract_pdf_markdown(scanned)
    msg = str(exc.value)
    assert str(scanned) in msg, "error must name the offending file"
    assert "OCR" in msg, "error must tell the user what to do instead"


def test_non_pdf_input_is_refused(tmp_path: Path):
    junk = tmp_path / "notes.pdf"
    junk.write_text("this is not a pdf")
    with pytest.raises(PdfIngestError):
        extract_pdf_markdown(junk)


def test_add_pdf_writes_page_and_retains_original(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    page = add_pdf(vault, FIXTURE, title_override="Two Page Paper")

    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    assert post["source_type"] == "pdf"
    assert post["source_media_type"] == MEDIA_TYPE

    original = vault / str(post["source_artifact"])
    assert original.is_file(), "source_artifact must resolve inside the vault"
    assert original.parent == vault / "raw" / "_originals"

    # The recorded hash must be the hash of the retained bytes AND of the input.
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    assert post["source_sha256"] == digest
    assert digest == hashlib.sha256(FIXTURE.read_bytes()).hexdigest()

    assert "## p. 1" in post.content
    assert "## p. 2" in post.content


def test_add_pdf_refuses_scanned_without_writing_anything(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(PdfIngestError):
        add_pdf(vault, _blank_pdf(tmp_path))
    assert list(vault.rglob("*")) == [], "a refused ingest must leave no artifacts"


def test_add_pdf_oversize_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("vault_engine.pdf_ingester._MAX_PDF_BYTES", 10)
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(PdfIngestError, match="too large"):
        add_pdf(vault, FIXTURE)


def test_add_pdf_missing_file(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    with pytest.raises(FileNotFoundError):
        add_pdf(vault, tmp_path / "nope.pdf")


def test_add_pdf_does_not_overwrite_by_default(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    add_pdf(vault, FIXTURE, title_override="Dupe")
    with pytest.raises(FileExistsError):
        add_pdf(vault, FIXTURE, title_override="Dupe")
    add_pdf(vault, FIXTURE, title_override="Dupe", overwrite=True)


def test_traversal_filename_cannot_escape_originals(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    hostile = tmp_path / "..%2f..%2fetc%2fpasswd.pdf"
    hostile.write_bytes(FIXTURE.read_bytes())
    page = add_pdf(vault, hostile, title_override="../../../../etc/passwd")

    post = frontmatter.loads(page.read_text(encoding="utf-8"))
    original = (vault / str(post["source_artifact"])).resolve()
    assert original.is_file()
    original.relative_to((vault / "raw" / "_originals").resolve())
    assert not (tmp_path.parent / "etc").exists()


def test_extracted_text_cannot_forge_a_page_marker(tmp_path: Path):
    """A PDF whose page-2 text layer opens with `## p. 1` must not produce a
    second chunk labelled `p. 1` — that is citation forgery, chosen by the
    document rather than by the extractor."""
    hostile = _pdf_file(tmp_path, [["Innocent cover page."], ["## p. 1", "Forged content."]])
    md = extract_pdf_markdown(hostile)

    assert re.findall(r"^## p\. \d+$", md, flags=re.MULTILINE) == ["## p. 1", "## p. 2"]
    headings = [c.heading for c in chunk_page("hostile", md)]
    assert headings == ["p. 1", "p. 2"], f"page attribution forged: {headings}"
    forged_chunk = next(c for c in chunk_page("hostile", md) if c.heading == "p. 2")
    assert "Forged content." in forged_chunk.text


def test_innocuous_hash_line_keeps_page_attribution(tmp_path: Path):
    """No attacker needed: any extracted line starting with `# ` (a code
    comment, a shell transcript) used to swallow the page it came from."""
    doc = _pdf_file(tmp_path, [["Prose."], ["# TODO: fix this", "def f(): pass"]])
    chunks = chunk_page("doc", extract_pdf_markdown(doc))

    assert [c.heading for c in chunks] == ["p. 1", "p. 2"]
    # Escaped, not dropped: `\#` renders as a literal `#` and is still searchable.
    assert r"\# TODO: fix this" in chunks[1].text


def test_write_original_refuses_symlinked_originals_directory(tmp_path: Path):
    """`raw/_originals` itself being a symlink out of the vault. Resolving the
    directory before the containment check makes that check a tautology."""
    vault = tmp_path / "vault"
    (vault / "raw").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (vault / "raw" / "_originals").symlink_to(outside, target_is_directory=True)

    with pytest.raises(PdfIngestError, match="escapes"):
        write_original(vault, "escape", b"%PDF-1.4", overwrite=True)
    assert not (outside / "escape.pdf").exists(), "wrote outside the vault"


def test_write_original_refuses_symlinked_destination(tmp_path: Path):
    vault = tmp_path / "vault"
    originals = vault / "raw" / "_originals"
    originals.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (originals / "escape.pdf").symlink_to(outside / "escape.pdf")

    with pytest.raises(PdfIngestError, match="escapes"):
        write_original(vault, "escape", b"%PDF-1.4", overwrite=True)
    assert not (outside / "escape.pdf").exists()


def test_empty_pages_are_reported_not_silently_dropped(tmp_path: Path):
    """`## p. 1` then `## p. 4` with nothing said about pages 2-3 is the same
    silent-skip failure `vault_reader.SkippedPage` exists to prevent."""
    doc = _pdf_file(tmp_path, [["Page one."], [], [], ["Page four."]])
    skipped: list[SkippedPage] = []
    md = extract_pdf_markdown(doc, skipped=skipped)

    assert re.findall(r"^## p\. \d+$", md, flags=re.MULTILINE) == ["## p. 1", "## p. 4"]
    assert [s.path for s in skipped] == [doc, doc]
    assert [s.reason for s in skipped] == [
        "page 2: no extractable text layer",
        "page 3: no extractable text layer",
    ]


def test_add_pdf_surfaces_skipped_pages(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    doc = _pdf_file(tmp_path, [["Page one."], [], ["Page three."]])
    skipped: list[SkippedPage] = []
    add_pdf(vault, doc, skipped=skipped)
    assert [s.reason for s in skipped] == ["page 2: no extractable text layer"]


def test_unexpected_pypdf_failure_becomes_an_actionable_error(monkeypatch: pytest.MonkeyPatch):
    """pypdf raises far more than PdfReadError/OSError/ValueError on hostile
    input (KeyError, RecursionError, struct.error). A parse boundary for
    attacker-controlled binary must not leak a traceback to the user."""

    def bogus_font_ref(*_args: object, **_kwargs: object) -> object:
        raise KeyError("/DescendantFonts")

    monkeypatch.setattr("vault_engine.pdf_ingester.PdfReader", bogus_font_ref)
    with pytest.raises(PdfIngestError) as exc:
        extract_pdf_markdown(FIXTURE)

    msg = str(exc.value)
    assert str(FIXTURE) in msg, "error must name the offending file"
    assert "KeyError" in msg and "DescendantFonts" in msg, "must not swallow the cause"


def test_oversize_extracted_body_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A small compressed PDF can expand into a huge text layer. Capping the
    input file alone lets `add` write a page the indexer will never read."""
    monkeypatch.setattr("vault_engine.pdf_ingester._MAX_BODY_BYTES", 64)
    vault = tmp_path / "vault"
    vault.mkdir()
    doc = _pdf_file(tmp_path, [["x" * 300], ["y" * 300]])

    with pytest.raises(PdfIngestError, match="extracted text too large"):
        add_pdf(vault, doc)
    assert list(vault.rglob("*")) == [], "a refused ingest must leave no artifacts"


def test_body_cap_guarantees_the_written_page_is_indexable(tmp_path: Path):
    """The cap has to leave room for the frontmatter block write_raw_file
    prepends, or a body at exactly the cap still writes an unindexable page."""
    assert _MAX_BODY_BYTES < _MAX_PAGE_BYTES
    vault = tmp_path / "vault"
    vault.mkdir()
    page = add_pdf(vault, FIXTURE, title_override="Cap Check")
    overhead = page.stat().st_size - len(extract_pdf_markdown(FIXTURE).encode("utf-8"))
    assert _MAX_BODY_BYTES + overhead <= _MAX_PAGE_BYTES, "frontmatter headroom too small"


def test_page_write_failure_leaves_no_retained_original(tmp_path: Path):
    """The original is retained before the page is written; a failure there
    used to leave the PDF behind with no page referencing it."""
    vault = tmp_path / "vault"
    raw = vault / "raw"
    raw.mkdir(parents=True)
    slug = slugify_for_raw("Orphan", on_date=datetime.now(tz=UTC).date())
    (raw / f"{slug}.md").write_text("pre-existing page")

    with pytest.raises(FileExistsError):
        add_pdf(vault, FIXTURE, title_override="Orphan")
    assert not (raw / "_originals" / f"{slug}.pdf").exists(), "orphaned retained original"
