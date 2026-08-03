"""Header-aware markdown chunking with a size cap.

Splits a page on H1/H2 boundaries, then enforces a size band on the
resulting sections:

- A section over ``max_tokens`` is split on paragraph boundaries (greedy
  packing). A single paragraph that alone exceeds the cap is hard-split on
  word boundaries rather than emitted oversize. **Every sub-chunk carries
  the parent section's heading**, which is what keeps the PDF page
  coordinate (``pdf_ingester`` emits one ``## p. N`` H2 per printed page)
  recoverable from ``Chunk.heading``.
- A trailing sub-chunk under ``min_tokens`` is folded back into the
  previous sub-chunk of the *same* section rather than embedded as a
  sliver. Merging never crosses a heading: two short sections stay two
  chunks. An earlier draft merged across sections, per an older version of
  this docstring, and it put page 2's text in a chunk labelled ``p. 1``
  (see ``tests/test_pdf_ingester.py``) — a coordinate the document did not
  choose and the reader cannot see. So an undersized *section* is emitted
  alone; only an undersized *remainder* is folded.

"Tokens" here means **whitespace-separated words, not the embedder's
tokens** — the chunker has no tokenizer and does not load the model.
English prose runs roughly 1.3 model tokens per word, so a section at
``chunk_max_tokens = 512`` is about 670 tokens to mxbai-embed-large and
will still be truncated by its 512-token window. Set ``chunk_max_tokens``
to ~380 if you want the cap to respect that window. The count is an
approximation chosen for being dependency-free and predictable, not for
being exact.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

from vault_engine.config import EngineConfig


@dataclass
class Chunk:
    page_slug: str
    idx: int
    heading: str
    text: str
    checksum: str


# Match lines starting with 1-2 # followed by a space (H1 + H2).
_HEADER = re.compile(r"^(#{1,2})\s+(.*)$", re.MULTILINE)
# Blank-line paragraph break.
_PARA = re.compile(r"\n\s*\n")


def _words(text: str) -> int:
    return len(text.split())


def _split_section(
    heading: str, text: str, max_tokens: int, min_tokens: int
) -> list[tuple[str, str]]:
    """Greedily pack a section's paragraphs into chunks of at most max_tokens.

    Sub-chunks all carry ``heading``: a split section is still the same
    section, and the heading is the only coordinate a chunk has.

    A trailing sub-chunk under ``min_tokens`` is folded back into its
    previous sibling rather than embedded as a sliver, which is allowed to
    push that chunk over ``max_tokens`` by up to ``min_tokens - 1`` words.
    Deliberate: a 20-word chunk is a worse vector than a 530-word one, and
    the fold never crosses a heading, so it cannot move text under another
    section's coordinate.
    """
    if _words(text) <= max_tokens:
        return [(heading, text)]

    pieces: list[str] = []
    for para in _PARA.split(text):
        para = para.strip()
        if not para:
            continue
        words = para.split()
        if len(words) <= max_tokens:
            pieces.append(para)
        else:
            # No paragraph boundary to split on (a PDF page's text layer is
            # often one blob). Hard-split on words: an oversize chunk is the
            # bug this exists to fix, so mid-sentence beats leaving it whole.
            pieces.extend(
                " ".join(words[i : i + max_tokens]) for i in range(0, len(words), max_tokens)
            )

    out: list[tuple[str, str]] = []
    buf: list[str] = []
    size = 0
    for piece in pieces:
        n = _words(piece)
        # `size >= min_tokens` keeps a short leading paragraph (a heading line
        # on its own, typically) from being flushed as a sliver of its own.
        if buf and size >= min_tokens and size + n > max_tokens:
            out.append((heading, "\n\n".join(buf)))
            buf, size = [], 0
        buf.append(piece)
        size += n
    if buf:
        tail = "\n\n".join(buf)
        if out and size < min_tokens:
            out[-1] = (heading, f"{out[-1][1]}\n\n{tail}")
        else:
            out.append((heading, tail))
    return out


def chunk_page(
    page_slug: str,
    body: str,
    *,
    max_tokens: int = EngineConfig.chunk_max_tokens,
    min_tokens: int = EngineConfig.chunk_min_tokens,
) -> list[Chunk]:
    """Split body into header-section chunks inside a size band.

    A chunk = the heading line + every line until the next H1 or H2, split
    on paragraph boundaries if over ``max_tokens``, with an undersized
    trailing piece folded back into its previous sibling (see the module
    docstring; both sizes are word counts, an approximation of the
    embedder's tokens). Every sub-chunk of a split section carries that
    section's heading. Pages with no headings produce a single chunk with
    empty heading. Empty chunks (heading with no body) are dropped.
    """
    matches = list(_HEADER.finditer(body))
    raw_chunks: list[tuple[str, str]] = []  # (heading, text)

    if not matches:
        text = body.strip()
        if text:
            raw_chunks.append(("", text))
    else:
        # Prelude: anything before the first heading.
        prelude = body[: matches[0].start()].strip()
        if prelude:
            raw_chunks.append(("", prelude))
        for i, m in enumerate(matches):
            heading = m.group(2).strip()
            start = m.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            text = body[start:end].strip()
            if text:
                raw_chunks.append((heading, text))

    sized: list[tuple[str, str]] = []
    for heading, text in raw_chunks:
        sized.extend(_split_section(heading, text, max_tokens, min_tokens))

    chunks: list[Chunk] = []
    for idx, (heading, text) in enumerate(sized):
        if not text.strip():
            continue
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        chunks.append(
            Chunk(
                page_slug=page_slug,
                idx=idx,
                heading=heading,
                text=text,
                checksum=digest,
            )
        )
    return chunks
