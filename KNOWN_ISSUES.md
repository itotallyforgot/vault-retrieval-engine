# Known Issues

Issues surfaced by the v0.1.0 multi-axis review that are deferred to a later
release. Listed honestly so consumers can decide whether the engine fits
their use case at current quality.

Last updated: 2026-05-04

## Performance ceiling at 10k+ pages

Several hot paths are O(N²) in vault size. The engine has been tested
against vaults up to ~340 pages (the operator's Second-Brain vault) and
runs comfortably there. Behavior at 10k+ pages is **not yet verified**.

Specifically:

- **`stores.graph_store.walk()`** uses `nx.all_simple_paths` per seed.
  Worst-case combinatorial; OK at ~340 pages, slow at 10k+ on dense
  graphs. Replacement with bounded BFS planned for v0.2.0.

- **`inference.add_similarity_edges`** runs O(N²) page-pair cosine on
  every reindex AND every single-page edit. At 340 pages, single-edit
  reindex is sub-second. At 10k pages, expect multi-second pauses on
  every save. Vector-matmul rewrite + incremental scoring planned for
  v0.2.0.

- **`vault_reader.iter_pages`** walks the disk and re-parses
  frontmatter on every reindex. Page-index cache planned for v0.2.0.

If your vault is under ~1000 pages, none of this should bite. If
larger, treat the engine as a beta and benchmark before relying on it.

## Architecture: CLI bypasses Service

The CLI's `status`, `reindex`, `search`, `expand`, `source`, and `eval`
commands construct an Indexer + Retrieval directly, while `serve` and
`mcp` go through `Service`. Search via CLI uses the legacy `Retrieval`
path (vec-only); search via HTTP/MCP uses `Service.router.dispatch`
(dual-channel + RRF).

Two result shapes for the same logical query depending on transport.
Refactor to make Service the single assembler is planned for v0.2.0.

## Architecture: Transport reaches into Service internals

`http_server.py` and `mcp_server.py` access `svc.graph_store.graph`
directly. Promotion to a typed `GraphQuery` facade planned for v0.2.0.

## Observability gaps

- No request-level logging on the retrieval hot path. No HTTP
  correlation ID. `--verbose` / `--quiet` flags missing on the CLI;
  log calls silently dropped without `logging.basicConfig`.
- `vault-engine status` does not yet report the engine version, graph
  node/edge counts, or store fingerprint. Planned for v0.2.0.

## Slug schema is filename-stem-only

Two pages with the same stem in different directories
(`wiki/topics/foo.md` vs `raw/foo.md`) currently raise
`SlugCollisionError` at index time. Kind-prefixed slugs
(`topic-foo`, `raw-foo`) would resolve cleanly but require a vec-store
migration. Planned for v0.2.0 with auto-migration.

## URL ingestion robustness

`vault-engine add <url>` has SSRF, redirect, and size protections, but
no retry/backoff on transient failures. A 5xx response or single
ReadTimeout aborts with a `FetchError`. Retry-with-exponential-backoff
planned for v0.2.0.

## SentenceTransformer load

`Service.__init__` loads the embedding model eagerly. If the
HuggingFace cache is missing or corrupt, or the network is offline at
first run, `serve` / `mcp` exits with a stack trace. Lazy-load on
first encode planned for v0.2.0.

## Test coverage gaps

- `url_ingester.fetch_url` lacks integration tests against mocked HTTP.
  All current tests cover the extract / write paths only.
- Watcher tests use timing-sensitive sleeps; may flake on slow CI
  runners.
- `community.compute_communities` is tested but the reindex_page edge
  cases (community ID stability across single-page edits) are not.

## Documentation

- No ADRs for the four non-obvious architectural decisions: sqlite-vec
  selection, NetworkX over alternatives, the 0.85 INFERRED edge
  threshold, and the LOOKUP/SEMANTIC/MULTI_HOP/HYBRID router boundaries.
  ADRs planned for v0.2.0.
- Class-level and public-method docstrings are sparse outside the CLI
  and core stores. Module docstrings are present everywhere.

## Roadmap

Items above will land in v0.2.0 with a follow-up multi-axis review
before that release. v0.1.0 is honest-quality on security and
correctness; performance and architecture refactors are next.
