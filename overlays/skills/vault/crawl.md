---
title: Crawl
last_updated: 2026-05-04
invoked_by: ["skills/vault/adapters/claude-code/vault.md"]
---

# Crawl

**Purpose:** Fetch a URL, extract clean article content, and write a
frontmatter-compliant `raw/<slug>.md` file that the existing
`/vault ingest` skill can consume. This is the auto-producer
counterpart to the Obsidian Web Clipper — same output shape, different
trigger.

**When to run:** A URL is in front of the user (article, blog, post,
gist) and they want it captured for later ingestion without leaving
their session to open the browser clipper.

**Inputs:** A single URL (required). Optional title override.

## Procedure

1. **Validate the URL.** Reject obvious non-articles (homepage roots,
   query-only paths, javascript: URIs). Surface and stop on rejection.
2. **Run the engine's URL scraper.** From the vault root:
   ```
   vault-engine add <url> --vault <vault-root>
   ```
   Optional: `--title "<override>"` if the page title is missing or
   unhelpful. Optional: `--overwrite` if the user explicitly wants to
   replace an existing slug.
3. **Confirm the file was written.** The CLI prints `Wrote raw/<slug>.md`.
   Verify the file exists and read its frontmatter.
4. **Validate frontmatter.** The file MUST contain at least:
   - `ingested: false` (so subsequent `/vault ingest` picks it up)
   - A URL field — engine writes `source:`; Web Clipper writes `url:`.
     Either is acceptable as long as it matches the input URL.
   - `clipped_at` ISO 8601 timestamp
   Other fields (title, author, published, source_type, tags) vary by
   producer; both shapes are consumed cleanly by `/vault ingest`. Do
   not enforce a stricter schema here than the ingest skill itself
   tolerates.
   If `ingested: false` or the URL field is missing, surface the gap
   and stop — do not attempt to repair the file from this skill.
5. **Report.** Print the relative path of the new raw file and the
   suggested next step:
   `Next: /vault ingest raw/<slug>.md` (or batch ingest).
6. **Do not auto-ingest.** Leave the file for the user to review and
   trigger ingest manually. Splitting scrape from synthesis keeps the
   step from auto-modifying the wiki without explicit consent.

## Out of scope

- Content extraction logic. The engine's `url_ingester` (trafilatura
  under the hood) owns that. Do not re-implement.
- Wiki writes. This skill only produces a `raw/<slug>.md` file.
- Multi-URL batch crawl. Run the skill once per URL; batch is a future
  enhancement (Phase 1.5+).
- Authentication / paywall bypass. If the engine returns an
  authentication error or empty extraction, surface and stop — do not
  attempt workarounds.

## Validation

- `raw/<slug>.md` exists after the engine call.
- Frontmatter contains `ingested: false` and at least one URL field
  (`source:` or `url:`).
- Frontmatter contains a `clipped_at` ISO 8601 timestamp.
- File body is non-empty (>50 chars of extracted content).
- Engine printed exit-zero (no `Error:` prefix on stderr).

## Failure modes

| Failure | Recovery |
|---|---|
| Engine missing or not on PATH | Surface install instructions; stop. |
| Network failure / 4xx / 5xx | Surface the engine error verbatim; stop. Do not retry silently. |
| Existing slug collision | Print the conflict; ask the user if they want `--overwrite`. |
| Empty extraction (paywall, JS-rendered) | Surface; stop. Suggest the user use Web Clipper as fallback. |
| Malformed frontmatter post-write | Surface as engine bug; do not attempt repair from skill side. |
