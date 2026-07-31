"""Local-file PDF → raw/ ingestion adapter (ADR 0006).

`vault-engine add ./paper.pdf` reads a PDF off local disk, extracts its
text layer page by page, and writes `<vault>/raw/<slug>.md` with one
`## p. N` section per page. Page markers are ordinary H2 headings, so
`chunker.chunk_page` (which splits on H1/H2 and keeps the heading line in
the chunk text) carries them into the vector store and the FTS index with
no schema change.

Implements the ADR 0006 decision: the original PDF is retained at
`<vault>/raw/_originals/<slug>.pdf` and identified in the generated page's
frontmatter by `source_artifact`, `source_sha256`, and `source_media_type`.

Deliberately local-file only. Fetching a PDF over HTTP would reopen the
SSRF surface `url_ingester` closes, and buys nothing a `curl` first does
not already give you.

No OCR. A PDF whose pages carry no text layer is **refused**, not ingested
as an empty page: a silent empty ingest is indistinguishable from a
successful one at query time, which is the failure class this project
treats as a bug.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader
from pypdf.errors import PdfReadError

from vault_engine.url_ingester import (
    ExtractedArticle,
    _strip_unsafe_chars,
    slugify_for_raw,
    write_raw_file,
)

# Same 10 MiB ceiling `vault_reader._MAX_PAGE_BYTES` and `url_ingester`'s
# fetch cap use. A larger PDF is nearly always an accident, and the whole
# file is read into memory to hash it.
_MAX_PDF_BYTES = 10 * 1024 * 1024

MEDIA_TYPE = "application/pdf"


class PdfIngestError(Exception):
    """Raised when a local PDF cannot be ingested (unreadable, oversize, no text)."""


def extract_pdf_markdown(pdf_path: Path) -> str:
    """Return markdown with one `## p. N` section per text-bearing page.

    Page numbers are 1-based positions in the document, so a page whose text
    layer is empty is omitted rather than renumbering the pages after it.

    Raises:
        PdfIngestError: the file is not a readable PDF, or no page in it has
            an extractable text layer (a scanned/image-only PDF).
    """
    try:
        reader = PdfReader(pdf_path)
        pages = [(n, page.extract_text() or "") for n, page in enumerate(reader.pages, start=1)]
    except (PdfReadError, OSError, ValueError) as e:
        raise PdfIngestError(f"cannot read PDF {pdf_path}: {e}") from e

    sections = [f"## p. {n}\n\n{text.strip()}" for n, text in pages if text.strip()]
    if not sections:
        raise PdfIngestError(
            f"no extractable text layer in {pdf_path} "
            f"({len(pages)} page(s) scanned). This engine does not OCR; "
            f"run the file through an OCR tool first, then ingest the result."
        )
    return "\n\n".join(sections)


def write_original(vault_path: Path, slug: str, data: bytes, *, overwrite: bool = False) -> Path:
    """Retain the source bytes at `<vault>/raw/_originals/<slug>.pdf`.

    Mirrors :func:`url_ingester.write_raw_file`'s traversal guard: the
    resolved destination must stay inside `raw/_originals/`, which also
    refuses a destination that is a symlink pointing out of the vault.

    Raises:
        PdfIngestError: destination escapes `raw/_originals/`.
        FileExistsError: destination exists and ``overwrite`` is False.
            Overwriting silently would invalidate the ``source_sha256``
            already recorded by whichever page retained that original.
    """
    originals_dir = (vault_path.resolve() / "raw" / "_originals").resolve()
    originals_dir.mkdir(parents=True, exist_ok=True)
    dest = (originals_dir / f"{slug}.pdf").resolve()
    try:
        dest.relative_to(originals_dir)
    except ValueError as e:
        raise PdfIngestError(f"refusing write: original {dest} escapes {originals_dir}") from e
    if dest.exists() and not overwrite:
        raise FileExistsError(
            f"retained original already exists: {dest} -- pass overwrite=True to replace."
        )
    dest.write_bytes(data)
    return dest


def add_pdf(
    vault_path: Path,
    pdf_path: Path,
    *,
    overwrite: bool = False,
    title_override: str | None = None,
) -> Path:
    """End-to-end: read a local PDF, retain it, write `raw/<slug>.md`.

    Extraction runs before anything is written, so a scanned PDF is refused
    without leaving a retained original behind.

    Raises:
        FileNotFoundError: ``pdf_path`` does not exist.
        PdfIngestError: oversize, unreadable, or no extractable text layer.
        FileExistsError: the raw page or the retained original already
            exists and ``overwrite`` is False.
    """
    src = pdf_path.resolve(strict=True)
    size = src.stat().st_size
    if size > _MAX_PDF_BYTES:
        raise PdfIngestError(f"PDF too large: {src} ({size} bytes > {_MAX_PDF_BYTES} cap)")

    body = extract_pdf_markdown(src)
    data = src.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    title = _strip_unsafe_chars(title_override or src.stem).strip() or "Untitled"
    now = datetime.now(tz=UTC)
    # write_raw_file derives its slug from the YYYY-MM-DD prefix of clipped_at
    # and the title, so deriving both from `now` keeps the two names in step.
    clipped_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    slug = slugify_for_raw(title, on_date=now.date())

    original = write_original(vault_path, slug, data, overwrite=overwrite)
    rel_original = original.relative_to(vault_path.resolve()).as_posix()

    return write_raw_file(
        vault_path=vault_path,
        article=ExtractedArticle(
            title=title,
            body=body,
            url=rel_original,
            author=None,
            published=None,
        ),
        clipped_at=clipped_at,
        overwrite=overwrite,
        source_type="pdf",
        extra_frontmatter={
            "source_artifact": rel_original,
            "source_sha256": digest,
            "source_media_type": MEDIA_TYPE,
        },
    )
