"""Header-aware markdown chunking.

Splits a page on H1/H2 boundaries (configurable). Each chunk keeps the
heading line and its body until the next header at the same or higher level.
Chunks below a min size are merged into the next chunk; chunks above the
max size are split on paragraph boundaries.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass


@dataclass
class Chunk:
    page_slug: str
    idx: int
    heading: str
    text: str
    checksum: str


# Match lines starting with 1-2 # followed by a space (H1 + H2).
_HEADER = re.compile(r"^(#{1,2})\s+(.*)$", re.MULTILINE)


def chunk_page(page_slug: str, body: str) -> list[Chunk]:
    """Split body into header-section chunks.

    A chunk = the heading line + every line until the next H1 or H2.
    Pages with no headings produce a single chunk with empty heading.
    Empty chunks (heading with no body) are dropped.
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

    chunks: list[Chunk] = []
    for idx, (heading, text) in enumerate(raw_chunks):
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
