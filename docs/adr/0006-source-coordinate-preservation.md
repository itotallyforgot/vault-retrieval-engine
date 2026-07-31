# ADR 0006 — Retain original artifacts and record their identity

**Status:** Proposed
**Date:** 2026-07-30
**Decision-maker:** project owner

## Context

The engine's wedge claim is auditable retrieval. A citation chain today resolves chunk to page to source page, where every one of those is a markdown file inside the vault. That holds while the vault is the origin of the knowledge, which was the v0.1.0 assumption.

It stops holding when the origin is an external artifact. `add <url>` fetches a document, extracts it, and writes markdown into `raw/`. The original is never persisted: `url_ingester.add_url` holds the fetched body in a local variable and discards it after extraction. So the chain can name a markdown file a claim came from, and nothing more. There is no way back to the thing the markdown was made from, and no way to detect that the derived markdown has drifted from it.

An earlier draft of this ADR proposed solving this with per-chunk page coordinates. Adversarial review killed that version on four grounds, all verified in code:

- **The engine cannot ingest a paged format at all.** `url_ingester._ALLOWED_CONTENT_TYPES` is HTML and plain text, and `application/pdf` is rejected at fetch. `vault_reader.iter_pages` walks `rglob("*.md")`. There is no producer for a page number, so the schema would have specified storage for data nothing can generate.
- **The storage was impossible as written.** `embedding_meta` is a singleton table (`singleton INTEGER PRIMARY KEY CHECK (singleton = 1)`), so per-chunk rows cannot live there. `chunks` is a vec0 virtual table, and vec0 rejects `ALTER TABLE`, so adding a column means dropping and rebuilding, which means a full re-embed.
- **A single `page` field is unsound for the modal chunk.** `chunker.chunk_page` splits on H1/H2 with no size cap. `config.chunk_max_tokens` exists but is referenced nowhere in `src/`, and the module docstring's claim that oversized chunks are split on paragraph boundaries describes code that does not exist. One chunk is one heading section, which in a converted paper or statute routinely spans many printed pages. A scalar page number would be wrong for most of the chunk's text, or null.
- **Chunk identity is destroyed before any citation code runs.** `Router._vector_search` builds `RankedHit(doc_id=hit.page_slug, ...)` and drops `chunk_idx`. Everything downstream, including both transports, is page-keyed. A per-chunk locator would be unreachable from HTTP and MCP regardless of how it was stored.

The legal motivation in that draft was also wrong, and worth correcting rather than quietly dropping. Bluebook Rule 18.2 requires citing the print source when one is available, unless the digital version is authenticated, official, or an exact copy. The consequence of failing that test is that you cite the print source, not that the material becomes uncitable. Star pagination is what lets a researcher read Westlaw and pincite the reporter. Sixteen states have adopted vendor-neutral citation where the pincite is a paragraph number, which survives format conversion intact.

That correction changes the conclusion rather than weakening it. The rule rewards holding the unaltered original, because the original is the thing the rule lets you cite. It does not reward annotating a derived artifact with coordinates, because a conversion is never an exact copy no matter how good its metadata.

## Decision

**Persist the original artifact alongside the derived markdown, and record its identity in the derived page's frontmatter.**

Three fields, written by the ingestion adapter into the `raw/` page it already creates:

| Field | Meaning |
|---|---|
| `source_artifact` | Vault-relative path to the retained original |
| `source_sha256` | Content hash of the original at ingestion time |
| `source_media_type` | `application/pdf`, `text/html`, and so on |

Originals live under `raw/_originals/`, inside the vault, so they travel with it and a chain stays portable across machines.

Page numbers, character spans, and a fidelity enum are deliberately **not** in this decision. They belong in a later ADR, written once a paged-format extractor exists and its real output shape is known. Designing that schema before its only producer exists is how the first draft got the page cardinality wrong.

Frontmatter rather than the vector store, because the vault is the source of truth and this is provenance about a page, not about a chunk. It needs no migration, no `schema_version` column, and no re-embed. `citations.py` already reads frontmatter for `raw_path`, so surfacing these is an additive change to `Citation`.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Per-chunk `SourceLocator` with page and span in the vector store | The previous draft. Specifies storage for data no adapter can produce, requires a vec0 table rebuild and full re-embed, models pages as a scalar when chunks span many, and lands behind a router that has already discarded chunk identity. |
| Do nothing, keep citing the derived markdown | The status quo. Leaves no path back to the original and no way to detect drift, which is the gap blocking every persona beyond the personal-vault case. |
| Record offsets into the converted markdown plus a converter version | Cheaper than the rejected draft and genuinely viable. Still describes a derived artifact, so it never satisfies the exact-copy test, and it buys precision the engine cannot yet use. Revisit alongside the coordinates ADR. |
| Store originals outside the vault | Makes `source_artifact` machine-local, so a chain stops being portable and breaks when the vault opens on another machine. Contradicts the local-first framing this decision exists to serve. |

## Consequences

### Positive

- A citation chain can name the original a claim descends from, and `source_sha256` makes that assertion checkable rather than asserted.
- Drift between an original and its derived markdown becomes detectable, which is the property that makes re-conversion safe later.
- No schema migration, no re-embed, no downtime. The cost that sank the previous draft disappears entirely.
- It is a prerequisite the coordinates work needs anyway, so nothing here is thrown away by a later, better-informed locator design.

### Negative

- Vault size grows by the size of retained originals, and `raw/_originals/` will be the largest thing in a PDF-heavy vault. This lands in Obsidian Sync and in any git remote the vault has.
- `vault_reader.iter_pages` and the watcher are both markdown-only, so a retained original is invisible to the engine. Nothing notices if one is deleted or moved, and `source_sha256` only means something if something re-verifies it. This decision does not add that verification, so a chain can point at a missing file without saying so.
- It delivers nothing until an adapter retains an original. Today `add <url>` is the only ingestion path and it handles HTML, where the value is real but modest.

## Status flags

Blocked on, and should land with or after:

- A paged-format ingestion adapter. Without one this decision only applies to HTML.

Revisit if:

- A paged extractor ships, at which point the deferred coordinates ADR should be written against what it actually emits.
- `Router._vector_search` starts preserving `chunk_idx`, which is the precondition for any chunk-level provenance reaching a transport.
- Retained originals prove too large for vault sync, which would force the machine-local option this ADR rejects, and with it a portability tradeoff worth its own decision.
