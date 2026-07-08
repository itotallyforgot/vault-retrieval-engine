# Known Issues

Issues surfaced by the v0.1.0 multi-axis review and the cleanup that followed. Listed honestly so consumers can decide whether the engine fits their use case at current quality.

Last updated: 2026-06-11

## What landed in v0.1.0

The multi-axis review surfaced 12 P0 + 14 critical-P1 findings. As of v0.1.0 ship:

- **All security P0s fixed** — SSRF guards on URL ingestion, request size caps on the HTTP server (query body and `top_k` bounded at validation time; there is no request *rate* limiter), JWT exp-claim required, refuse-to-bind on non-loopback without a secret, blocked-terms scan with case-insensitive matching.
- **All correctness P0s fixed** — service stop-race resolved, watcher rename emits both src and dest, slug collisions surface as SlugCollisionError, vec_store mutations atomic, file-size cap in reads.
- **All documentation P0s fixed** — README CLI and eval-fixture schema match reality; sample vault expanded with multi-hop chains, alias chains, and an orphan; CI eval gate uses real expected_pages so it can fail.
- **Performance P0s fixed** — graph walk replaced with bounded BFS, similarity-edge inference replaced with single-matmul, reindex_page does one disk walk instead of two.
- **5 ADRs landed** — sqlite-vec, NetworkX, INFERRED threshold (0.85), router tiers, and default embedding model (mxbai-embed-large).
- **Vault-overlay plug-in pattern landed** — engine-aware vault skills (synth, crawl) and the post-commit reindex hook moved out of the vault and into `overlays/` here, installable via `scripts/install-vault-overlays.sh`.

## What's deferred to v0.2.0

### Architecture: CLI bypasses Service (P1-3)

The CLI's `status`, `reindex`, `search`, `expand`, `source`, and `eval` commands construct an Indexer + Retrieval directly, while `serve` and `mcp` go through `Service`. Search via CLI uses the legacy `Retrieval` path (vec-only); search via HTTP/MCP uses `Service.router.dispatch` (dual-channel + RRF).

Two result shapes for the same logical query depending on transport. Refactor to make Service the single assembler is v0.2.0 work; a `Service.start(rebuild=False, watch=False)` mode will be needed for CLI commands that don't want a full rebuild on entry.

### Architecture: Transport facade (P1-4, partial)

A small typed surface landed on `Service`: `service.graph` (property), `service.graph_node(slug)`, `service.graph_stats()`. `mcp_server.py` and `http_server.py` no longer reach through `svc.graph_store.graph` for the most-common patterns.

The full GraphQuery facade with all 10+ MCP tool primitives (`get_neighbors`, `get_community`, `god_nodes`, `shortest_path`, `find_topic_page`, `find_unlinked_references`, `get_linked_references`) is v0.2.0. Until then, transport handlers still inline some queries against `svc.graph` directly.

### Observability

- `vault-engine status` does not yet report the engine version, graph node/edge counts, or store fingerprint. Planned for v0.2.0.
- HTTP/JSON request logging landed; per-channel timing on the retrieval hot path is still v0.2.0.

### Slug schema is filename-stem-only

Two pages with the same stem in different directories (`wiki/topics/foo.md` vs `raw/foo.md`) currently raise `SlugCollisionError` at index time. Kind-prefixed slugs (`topic-foo`, `raw-foo`) would resolve cleanly but require a vec-store migration. Planned for v0.2.0 with auto-migration via a schema-version column on `embedding_meta`.

### URL ingestion robustness

`vault-engine add <url>` has SSRF, redirect, content-type, and size protections, but no retry/backoff on transient failures. A 5xx response or single ReadTimeout aborts with a `FetchError`. Retry-with-exponential-backoff planned for v0.2.0.

### SentenceTransformer load

`Service.__init__` loads the embedding model eagerly. The `EmbedderLoadError` wrapping landed (actionable error messages on import / load failures), but lazy-load on first encode is still v0.2.0 — until then, `serve` / `mcp` startup pays the model-load cost up front.

### Test coverage gaps

- `url_ingester.fetch_url` lacks integration tests against mocked HTTP. Existing tests cover the extract / write paths only.
- Watcher tests use timing-sensitive sleeps; may flake on slow CI runners.
- `community.compute_communities` is tested but the reindex_page edge cases (community ID stability across single-page edits) are not.

### Embedder is bag-of-words on word-order and negation

The default embedder (`mxbai-embed-large-v1`) scores near-duplicate text that differs only by word order or a flipped claim as highly similar. Measured on the adversarial fixtures:

- **Word-swap** pairs (same words, reordered to change meaning): cosine **0.96–0.99**.
- **Shuffle** pairs (sentence-order shuffled): cosine **0.94–0.99**.
- **Negation** pairs ("X is safe" vs "X is not safe"): cosine **0.68–0.81** — closer, but still high enough that pure semantic ranking can surface the wrong polarity.

Consequence: semantic-only retrieval (and the INFERRED similarity edges, which use the same vectors) cannot reliably distinguish a statement from its negation or from a reordered variant. This is an inherent property of the model, not a bug in the engine. The router de-rates negation queries from pure `SEMANTIC` to `HYBRID` so a lexical/topology leg can disambiguate, but `HYBRID` today fuses vector + graph topology only — there is **no lexical (BM25/keyword) channel yet**, so the disambiguation is partial. A true lexical RRF channel is tracked for a future release. The adversarial fixtures (negation/word-swap/shuffle) exist as a regression gate so any embedder swap is measured against these axes before it lands.

### Performance at very large vaults

The matmul + BFS rewrites in v0.1.0 take the engine from "unusable above ~500 pages" to "usable at ~10k pages." Beyond ~50k chunks, sqlite-vec's brute-force MATCH becomes the bottleneck (see ADR 0001); ANN structures (faiss / hnswlib) would unblock that. Out of scope for v0.2.0 unless usage demands it.

## Roadmap

v0.2.0 ships when the architecture refactors (CLI uses Service, full GraphQuery facade) and the slug schema migration are done. Likely 2-3 weeks of focused work after v0.1.0 lands.

v0.1.0 is honest-quality on security, correctness, performance at target scale, and the wedge claim (no external API, local-only, citation chains for auditable retrieval). The deferred items in this file are real — none are hidden.
