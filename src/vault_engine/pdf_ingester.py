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
treats as a bug. A page with no text layer inside an otherwise readable PDF
is skipped, but counted and reported (see ``skipped``), never dropped.

Extracted text is untrusted input. Every line it emits at column 0 that
opens with `#` is escaped, so the document cannot author its own `## p. N`
marker and choose which page its content appears to come from. The page
coordinate is still carried **in band**, inside the markdown the document
also contributes to; escaping closes today's forgery, and moving the
coordinate out of band (per-chunk metadata) is the durable fix, deferred to
the coordinates ADR.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path

from pypdf import PdfReader

from vault_engine.url_ingester import (
    ExtractedArticle,
    _strip_unsafe_chars,
    slugify_for_raw,
    write_raw_file,
)
from vault_engine.vault_reader import _MAX_PAGE_BYTES, SkippedPage

# Same 10 MiB ceiling `vault_reader._MAX_PAGE_BYTES` and `url_ingester`'s
# fetch cap use. A larger PDF is nearly always an accident, and the whole
# file is read into memory to hash it.
_MAX_PDF_BYTES = 10 * 1024 * 1024

# Capping the input is not enough: a 222 KB Flate-compressed PDF expands to a
# 24 MB text layer, and at the input cap that extrapolates to ~1 GB. A page
# over `vault_reader._MAX_PAGE_BYTES` is skipped by the indexer forever while
# `add` reports success, so the extracted body is capped below that ceiling —
# with headroom for the frontmatter block `write_raw_file` prepends — and
# exceeding it is a refusal. Anything written is therefore indexable.
_MAX_BODY_BYTES = _MAX_PAGE_BYTES - 8192

MEDIA_TYPE = "application/pdf"

# Any line the extractor emits at column 0 that opens with `#` is escaped, so
# extracted text can never open a markdown heading. `chunker.chunk_page`
# splits on `^#{1,2}\s+` and labels the chunk with the heading text, so an
# unescaped `## p. 1` in a page-2 text layer produces a second chunk labelled
# `p. 1` — a document choosing its own citation. The accidental variant needs
# no attacker: any `# ` line (a code comment, a shell transcript) silently
# swallows the page it came from.
_LEADING_HASH = re.compile(r"^#", re.MULTILINE)


class PdfIngestError(Exception):
    """Raised when a local PDF cannot be ingested (unreadable, oversize, no text)."""


def extract_pdf_markdown(pdf_path: Path, skipped: list[SkippedPage] | None = None) -> str:
    """Return markdown with one `## p. N` section per text-bearing page.

    Page numbers are 1-based positions in the document, so a page whose text
    layer is empty is omitted rather than renumbering the pages after it.

    Args:
        pdf_path: Local PDF to read.
        skipped: Optional list, same contract as
            :func:`vault_reader.iter_pages`. Every page dropped for having no
            extractable text layer is appended as a :class:`SkippedPage`
            (``path`` = the PDF, ``reason`` = ``"page N: ..."``) instead of
            vanishing. The CLI passes a list so it can report the count.

    Raises:
        PdfIngestError: the file is not a readable PDF, no page in it has an
            extractable text layer (a scanned/image-only PDF), or the text
            extracted out of it exceeds ``_MAX_BODY_BYTES``.
    """
    sections: list[str] = []
    total = 0
    page_count = 0
    try:
        reader = PdfReader(pdf_path)
        # Extraction is interleaved with the size check rather than collected
        # first, so a decompression bomb is refused after one oversize page
        # instead of after the whole document is in memory.
        for n, page in enumerate(reader.pages, start=1):
            page_count = n
            text = (page.extract_text() or "").strip()
            if not text:
                if skipped is not None:
                    skipped.append(
                        SkippedPage(path=pdf_path, reason=f"page {n}: no extractable text layer")
                    )
                continue
            section = f"## p. {n}\n\n{_LEADING_HASH.sub(r'\\#', text)}"
            sections.append(section)
            total += len(section.encode("utf-8")) + 2  # + the "\n\n" join separator
            if total > _MAX_BODY_BYTES:
                break
    except Exception as e:  # pypdf raises KeyError/RecursionError/struct.error on hostile input
        raise PdfIngestError(f"cannot read PDF {pdf_path}: {type(e).__name__}: {e}") from e

    if total > _MAX_BODY_BYTES:
        raise PdfIngestError(
            f"extracted text too large: {pdf_path} (>{total} bytes from page {page_count} on, "
            f"cap {_MAX_BODY_BYTES}). A page over that cap would be written but never indexed."
        )
    if not sections:
        raise PdfIngestError(
            f"no extractable text layer in {pdf_path} "
            f"({page_count} page(s) scanned). This engine does not OCR; "
            f"run the file through an OCR tool first, then ingest the result."
        )
    return "\n\n".join(sections)


def write_original(vault_path: Path, slug: str, data: bytes, *, overwrite: bool = False) -> Path:
    """Retain the source bytes at `<vault>/raw/_originals/<slug>.pdf`.

    Mirrors :func:`url_ingester.write_raw_file`'s traversal guard: the
    resolved destination must stay inside the resolved vault root, which
    refuses both a destination symlink pointing out of the vault and a
    symlinked `raw/_originals` directory.

    Raises:
        PdfIngestError: destination escapes the vault root.
        FileExistsError: destination exists and ``overwrite`` is False.
            Overwriting silently would invalidate the ``source_sha256``
            already recorded by whichever page retained that original.
    """
    vault_root = vault_path.resolve()
    originals_dir = vault_root / "raw" / "_originals"
    originals_dir.mkdir(parents=True, exist_ok=True)
    dest = (originals_dir / f"{slug}.pdf").resolve()
    try:
        # Anchored to the vault root, not to a resolved originals_dir: if
        # `raw/_originals` is itself a symlink out of the vault, resolving it
        # first makes this check a tautology that always passes.
        dest.relative_to(vault_root)
    except ValueError as e:
        raise PdfIngestError(
            f"refusing write: original {dest} escapes vault root {vault_root}"
        ) from e
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
    skipped: list[SkippedPage] | None = None,
) -> Path:
    """End-to-end: read a local PDF, retain it, write `raw/<slug>.md`.

    Extraction runs before anything is written, so a scanned PDF is refused
    without leaving a retained original behind.

    ``skipped`` is forwarded to :func:`extract_pdf_markdown`; see there.

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

    body = extract_pdf_markdown(src, skipped=skipped)
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

    try:
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
    except BaseException:
        # The original is retained first so its own guards run before anything
        # is written, but a retained PDF no page references is an orphan.
        original.unlink(missing_ok=True)
        raise
