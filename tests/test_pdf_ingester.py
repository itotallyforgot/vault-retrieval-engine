"""Tests for the local-file PDF → raw/ ingestion adapter (ADR 0006)."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import frontmatter
import pytest
from pypdf import PdfWriter

from vault_engine.chunker import chunk_page
from vault_engine.pdf_ingester import (
    MEDIA_TYPE,
    PdfIngestError,
    add_pdf,
    extract_pdf_markdown,
    write_original,
)

FIXTURE = Path(__file__).parent / "fixtures" / "two_page.pdf"


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
