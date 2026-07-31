# Known Issues

What the engine does not do, or does badly, as of the latest tagged release.
Listed so consumers can decide whether it fits their use case before
installing it. Every entry below was re-verified against the code on the
date in the header; entries that the code showed were already fixed have
been deleted rather than left to rot.

Last updated: 2026-07-30 (v0.2.0)

## Capability gaps

### No PDF ingestion, and no non-markdown ingestion at all

The engine cannot read a PDF. Two independent places enforce this:

- `url_ingester._ALLOWED_CONTENT_TYPES` is `("text/html",
  "application/xhtml+xml", "text/plain")`. A URL that serves
  `application/pdf` is refused with `FetchError: unsupported content-type`,
  so `vault-engine add <pdf-url>` cannot bring one in.
- `vault_reader.iter_pages` globs `vault_path.rglob("*.md")`. A PDF sitting
  in the vault directory is invisible to the indexer even if you put it
  there by hand.

There is no extraction path, no OCR, and no plan encoded anywhere in the
repo. If your knowledge lives in PDFs, this engine does not retrieve over
it today. The same applies to any other non-markdown format: docx, epub,
html files on disk, plain `.txt`.

### The chunker has no size cap, and its docstring says otherwise

`chunker.py` opens with:

> Chunks below a min size are merged into the next chunk; chunks above the
> max size are split on paragraph boundaries.

Neither behavior exists. `chunk_page` matches `^#{1,2}\s` (H1 and H2 only),
slices the body between those matches, drops empty slices, and returns. No
merge, no split, no length check anywhere in the function. `EngineConfig`
does define `chunk_max_tokens: int = 512`, but the only reference to it in
the entire repository outside that definition is a test asserting it is
greater than zero. Nothing reads it.

The practical consequence: a page with a single H1 and 8,000 words of body
becomes one chunk. That chunk gets one embedding, so retrieval either
returns the whole thing or none of it, and the vector is the mean of eight
thousand words of unrelated material. Deeply-nested pages that use H3 and
below for their real structure chunk as though that structure were not
there. Header discipline in the vault is doing load-bearing work that the
engine's own docstring implies it does not need to.

The docstring has been corrected to describe actual behavior. The missing
size cap is a real gap, not just a documentation bug.

## Architecture

### CLI bypasses Service

`status`, `reindex`, `search`, `expand`, `source`, and `eval` construct an
`Indexer` plus `Retrieval` directly. Only `serve` and `mcp` go through
`Service`.

This matters because the two paths do different retrieval.
`Retrieval.search` encodes the query and calls `vec.search`, which is the
vector channel and nothing else. `Service.query` goes through
`Router.dispatch`, which fans out to vector, lexical (BM25), and topology
and fuses with RRF. So `vault-engine search "..."` and `POST /query` with
the same string return results built from different evidence. The CLI is
the weaker of the two.

Making `Service` the single assembler needs a
`Service.start(rebuild=False, watch=False)` mode so CLI commands do not pay
for a full rebuild on entry. Not done.

### Transport facade is partial

`Service` exposes a small typed surface: `graph` (property), `graph_node`,
`graph_stats`, and `graph_lock`. `http_server.py` is clean against it, using
`svc.query()` and `svc.graph_stats()` only.

`mcp_server.py` is not. It reaches through `svc.graph` directly in roughly a
dozen places to implement `get_neighbors`, `get_community`, `god_nodes`,
`shortest_path`, `find_topic_page`, `find_unlinked_references`, and
`get_linked_references`, and reaches `svc.graph_store` for one more. Those
primitives still live in the transport layer instead of behind a query
facade, so a second transport would have to reimplement them.

## Correctness and robustness

### Slug schema is filename-stem-only

Two pages with the same stem in different directories (`wiki/topics/foo.md`
and `raw/foo.md`) raise `SlugCollisionError` at index time
(`vault_reader.py`). Kind-prefixed slugs (`topic-foo`, `raw-foo`) would
resolve this but require a vec-store migration, and there is no
schema-version column on `embedding_meta` to migrate against yet.

### URL ingestion has no retry

`vault-engine add <url>` has SSRF, DNS-rebinding, redirect, content-type,
and size protections, all verified in `url_ingester.py`. What it does not
have is any retry or backoff: `grep` for retry, backoff, or sleep in that
module returns nothing. A single 5xx or one `ReadTimeout` aborts the
ingestion with a `FetchError`.

### Embedding model loads eagerly

`Service.__init__` constructs `SentenceTransformerEmbedder` unless a
caller passes one in, so `serve` and `mcp` pay the model-load cost at
startup rather than on first encode. `EmbedderLoadError` wrapping gives an
actionable message when the load fails, but the load still happens up
front.

### Observability

`vault-engine status` reports vault path, cache dir, page count, embedding
model, and skipped-page count with reasons. It does not report the engine
version, graph node or edge counts, or a store fingerprint. HTTP request
logging exists; per-channel timing on the retrieval hot path does not.

## Retrieval quality

### The embedder is bag-of-words on word order and negation

The default embedder (`mxbai-embed-large-v1`) scores near-duplicate text
that differs only by word order or a flipped claim as highly similar.
Measured on `tests/fixtures/adversarial_bow.jsonl`:

- **Word-swap** pairs (same words, reordered to change meaning): cosine
  0.96 to 0.99.
- **Shuffle** pairs (sentence-order shuffled): cosine 0.94 to 0.99.
- **Negation** pairs ("X is safe" against "X is not safe"): cosine 0.68 to
  0.81, lower, but still high enough that pure semantic ranking can surface
  the wrong polarity.

Semantic-only retrieval, and the INFERRED similarity edges that use the
same vectors, therefore cannot reliably distinguish a statement from its
negation or from a reordered variant. This is a property of the model, not
a bug in the engine.

Two mitigations are in the code. The router de-rates negation queries from
`SEMANTIC` to `HYBRID`, and the BM25 lexical channel runs on every dispatch
so a keyword leg can disambiguate. The mitigation is bounded by what
keyword overlap can resolve: a negation pair shares nearly all its tokens,
so BM25 helps most on word-order and word-choice cases and least on a bare
polarity flip. Similarity edges in the `[threshold, 0.95)` band are
annotated `AMBIGUOUS` rather than `INFERRED` so consumers can down-weight
the range where these failures cluster.

The adversarial fixtures are a regression gate: word-swap and shuffle are
tracked as xfail, so any embedder swap is measured on these axes before it
lands.

### Performance at very large vaults

The matmul and BFS rewrites in v0.1.0, plus the page-vector cache in
v0.2.0, take the engine from unusable above roughly 500 pages to usable at
roughly 10k. Beyond roughly 50k chunks, sqlite-vec's brute-force MATCH
becomes the bottleneck (see ADR 0001). ANN structures (faiss, hnswlib)
would unblock that. Out of scope unless usage demands it.

## Test coverage gaps

- `url_ingester.fetch_url` has exactly one end-to-end test, covering the
  cloud-metadata-IP refusal via a monkeypatched `getaddrinfo`. The success
  path, the redirect chain, the content-type refusal, and the size cap are
  covered only at the level of their helper functions, never through
  `fetch_url` against a mocked HTTP transport.
- Watcher tests use timing-sensitive sleeps (up to 0.3s) and may flake on
  slow CI runners.
- `community.compute_communities` is tested directly, and
  `test_indexer_edge_type.py` asserts every node carries a community after
  `reindex_page`. Community ID *stability* across a single-page edit is
  still untested.

## Documentation state

Seven ADRs exist. Five are on `main` and accepted: sqlite-vec (0001),
NetworkX (0002), the 0.85 INFERRED threshold (0003), router tiers (0004),
and the mxbai default model (0005). Two are in flight on branches and not
yet merged: 0006 source-coordinate preservation (Proposed) and 0007 the
lexical RRF channel (Accepted). `docs/adr/README.md` indexes the five on
`main`.

## Roadmap

The four items v0.1.0 named as v0.2.0 work (slug-schema migration, the
Service-CLI refactor, the full GraphQuery facade, and observability polish)
did not land in v0.2.0. v0.2.0 shipped the lexical channel, the reindex
performance fix, concurrency hygiene, and a security backlog instead. Those
four remain open and are described above. No date is attached to them,
because the last estimate was wrong by about three months.

The deferred items in this file are real, and none are hidden.
