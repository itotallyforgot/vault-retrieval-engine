# ADR 0006 — Chunks carry source coordinates into the citation chain

**Status:** Proposed
**Date:** 2026-07-30
**Decision-maker:** project owner

## Context

The engine's wedge claim is auditable retrieval. Today a citation chain resolves chunk → page → source page, where every one of those is a markdown file inside the vault. That is sufficient when the vault is the origin of the knowledge, which was the v0.1.0 assumption.

It stops being sufficient the moment the origin is an external artifact. The `add <url>` adapter fetches a document, converts it, and writes markdown into `raw/`. Conversion is lossy in a specific way that matters: it discards the coordinate system of the original. The resulting chain can name the markdown file a claim came from. It cannot name the page.

Three consequences, in ascending severity:

- A security researcher citing a converted whitepaper gets a soft cite. Survivable.
- An academic citing a converted PDF cannot produce a locator a reviewer can check against the published article.
- A legal analyst cannot cite at all. Bluebook Rule 3.2 requires a pinpoint to the exact page on effectively every citation, and material from a database that does not preserve original pagination is explicitly not citable as the print source. This is the rule that makes Westlaw output non-citable as hardcopy.

So the current design produces citations that look auditable and are not. That failure mode is worse than declining to cite, because the chain renders with the same confidence either way. The engine currently has no way to say "I know which document, not which page."

This blocks the substrate positioning (local-first retrieval and citation for source material that cannot leave the building, where citations have to survive scrutiny). Format coverage is not the gap. Coordinate fidelity is.

## Decision

**Every chunk carries a `SourceLocator` describing where it came from in the original artifact, and the citation chain surfaces it or explicitly declares its absence.**

A `SourceLocator` records:

| Field | Meaning |
|---|---|
| `artifact_path` | Path to the original file as ingested, not the derived markdown |
| `artifact_sha256` | Content hash of the original, so a chain can be revalidated against the file it cites |
| `media_type` | `application/pdf`, `text/markdown`, `text/html`, and so on |
| `page` | 1-indexed page in the original, when the format has pages |
| `span` | Character offsets into the extracted text of that page |
| `fidelity` | `exact`, `derived`, or `none` |

`fidelity` is the honesty field and is not optional. `exact` means the coordinates came from the source format itself, such as a PDF text layer with page boxes. `derived` means they were reconstructed by the converter and may drift. `none` means the format has no coordinate system, which is the correct and expected value for a hand-written markdown note.

A chain that renders a pinpoint cite from a `none` locator is a bug, and the assembler must refuse rather than approximate. Citation output degrades visibly: a `none` locator produces a document-level cite that is plainly document-level.

Storage lands on `embedding_meta` behind a `schema_version` column. That column is already a prerequisite for the kind-prefixed slug migration tracked in `KNOWN_ISSUES.md`, so the two migrations ship together rather than paying the cost twice.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Keep citing the derived markdown | The status quo. Fails the stated purpose for two of three target users, and fails silently, which is the worst property a citation system can have. |
| Store offsets into the converted markdown only | Offsets into a derived artifact. Re-running the converter, or bumping its version, invalidates every stored cite with no signal that it happened. |
| Keep originals and re-derive coordinates at query time | Moves a parser onto the hot path and makes citation latency depend on document size. Also can't revalidate a cite if the original moved. |
| Index PDFs natively, skip markdown entirely | Strongest fidelity, and worth revisiting. Rejected now because it abandons the wikilink graph for external sources, which is the other half of the value, and because layout-aware extraction is a much larger dependency than this decision needs to justify. |
| Treat it as per-adapter concern, no shared schema | Every adapter invents its own provenance shape, and the assembler can't reason about fidelity uniformly. The `none` case in particular needs to be expressible once. |

## Consequences

### Positive

- Pinpoint citation becomes possible where the format supports it, which is the precondition for the legal and academic personas.
- `artifact_sha256` makes a citation chain revalidatable. A cite can be checked against the file it names, and detect that the file changed underneath it.
- The `fidelity` field converts a silent failure into a visible one. This is the same property the adversarial embedder fixtures provide: the weakness is measured and surfaced rather than assumed away.
- The original artifact becomes the durable referent, so re-converting or swapping converters no longer invalidates stored citations.

### Negative

- Schema migration on `embedding_meta`, with the auto-migration burden that implies. Bundling with the slug migration contains but does not eliminate this.
- Every ingestion adapter must supply a locator, including the ones that can only supply `none`. New adapters cannot skip the question.
- Storage grows per chunk. Small next to the vectors, but not zero.
- Originals must be retained to keep `artifact_sha256` meaningful, which changes the engine's disk footprint story for PDF-heavy corpora.
- Markdown-only vaults gain a field that is `none` on every row and buys them nothing. The cost is real and falls on the current user to benefit a future one.

## Status flags

Revisit if:

- Layout-aware PDF extraction becomes cheap enough that native indexing beats convert-then-locate.
- A target persona needs a coordinate system this schema can't express, such as timecodes for audio or cell references for spreadsheets. The `span` field is deliberately text-oriented and will not stretch to those.
- Citation output needs to satisfy a specific style authority end to end, at which point a formatter sits above this layer and may demand fields it does not yet carry.
