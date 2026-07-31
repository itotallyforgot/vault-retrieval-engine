# Known Issues

What the engine does not do, or does badly, as of the latest tagged release.
Listed so consumers can decide whether it fits their use case before
installing it. Every entry below was re-verified against the code on the
date in the header; entries that the code showed were already fixed have
been deleted rather than left to rot.

Last updated: 2026-07-31 (v0.2.0, plus the merged but unreleased PDF adapter)

## Capability gaps

### PDF ingestion is local-file, text-layer only; no other non-markdown format

Unreleased on `main`: `vault-engine add ./paper.pdf --vault <path>` extracts
a local PDF's text layer with `pypdf` and writes `raw/<slug>.md` with one
`## p. N` section per text-bearing page, retaining the original at
`raw/_originals/<slug>.pdf`. What that still does not give you:

- **No OCR.** A page with no text layer is skipped, counted, and reported
  (`pages skipped (unreadable): N`, same convention as `status` /
  `reindex`); a PDF where *every* page is image-only is refused outright.
  Scanned documents need an OCR tool first.
- **No remote PDF fetch.** `url_ingester._ALLOWED_CONTENT_TYPES` is still
  `("text/html", "application/xhtml+xml", "text/plain")`, so
  `vault-engine add <pdf-url>` is refused with `FetchError: unsupported
  content-type`. Deliberate: fetching PDFs would reopen the SSRF surface
  `url_ingester` closes, and `curl` first costs nothing.
- **No docx, epub, html-on-disk, or plain `.txt`.** There is no extraction
  path for any of them and none is planned in the repo.
- **The retained original stays invisible to the engine.**
  `vault_reader.iter_pages` still globs `vault_path.rglob("*.md")`, so
  nothing indexes `raw/_originals/` and nothing re-verifies the recorded
  `source_sha256`. ADR 0006 records that as a known negative.
- **The page coordinate is carried in band.** `## p. N` is an ordinary H2 in
  the same markdown body the document contributes text to. Extracted lines
  that open with `#` are escaped, so a document cannot author its own
  `## p. 1` and pick which page its content appears to cite, but the
  coordinate still shares a channel with the content. Out-of-band per-chunk
  page metadata is the durable fix and is deferred to the coordinates ADR.
- **Extraction is capped.** The input file is capped at 10 MiB and the
  extracted text at just under `vault_reader._MAX_PAGE_BYTES`, so a
  compression bomb is refused rather than written as a page the indexer
  would silently skip forever.

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

The retrieval half of this is fixed, unreleased. `cli.search` builds a
`Router` from the already-open `Indexer` and calls `dispatch`, the same
three-channel RRF path `Service.query` uses, so `vault-engine search "..."`
and `POST /query` now answer from the same evidence. `EvalRunner` already
built its Router the same way, which is why the eval rig was measuring the
HTTP path rather than the CLI one.

What remains is object-graph duplication rather than a behavior gap.
`status`, `reindex`, `search`, `expand`, `source`, and `eval` still construct an
`Indexer` (and, for `expand` / `source`, a `Retrieval`) directly instead of
going through `Service`. Making `Service` the single assembler needs a
lifecycle that skips the rebuild for commands that do not need one, and a
naive `Service.start(rebuild=False)` will not do: nothing but
`Indexer.rebuild()` populates the graph, so a no-rebuild start leaves it
empty and silently breaks `Router._classify`, `topology_walk`, and
`_infer_seed`. `Retrieval` also cannot simply retire, because `citations.py`
and `eval.py` both depend on `Retrieval.expand` / `.source`. Not done.

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
four remain open and are described above. No date is attached to anything
below, because the last estimate was wrong by about three months.

Ordering, and the constraint that forces it:

1. **CLI uses the Router.** Landed on `main`, unreleased. The user-visible
   half of what this item was for: `vault-engine search` now dispatches
   through `Router` instead of `Retrieval`, so it answers from the same three
   channels as `POST /query`. It does *not* route through `Service`, and
   deliberately so. A `Service` lifecycle that skips the rebuild would leave
   the graph empty and break the very topology channel it exists to add.
   The remaining Service consolidation is object-graph cleanup with no
   retrieval-quality payoff, and is described above.
2. **`schema_version` on `embedding_meta`, alone.** One additive column plus
   the migration ladder. Landing it by itself is what lets every later
   migration be a single-variable change, which is the opposite of the
   bundling an earlier draft of ADR 0006 proposed.
3. **Kind-prefixed slugs.** Blocked on a design question that has no answer
   yet: wikilinks in a vault are written `[[foo]]`, not `[[topic-foo]]`, so
   prefixed slugs need a bare-target resolution rule, and if both `topic-foo`
   and `raw-foo` exist the collision reappears at link-resolution time. A
   loud `SlugCollisionError` is a defensible state; a half-designed
   resolution rule is not.
4. **Source coordinates.** ADR 0006 deliberately stopped at artifact
   retention. The coordinates ADR should be written against what the PDF
   extractor actually emits, now that one exists. The first draft of 0006 was
   refuted precisely because it designed the storage before the producer.
   Its prerequisite (chunk identity, item 5) is no longer in the way.
5. **Chunk identity through the router.** Landed on `main`, unreleased.
   `RankedHit` and `FusedHit` now carry `chunk_idx` / `content`, and
   `FusedHit` carries `per_channel_chunks` for the case where channels
   disagree about which chunk matched a page. Fusion still accumulates on the
   page slug, so no ranking moved. What this does *not* yet do: the chunk
   identity is an index and the chunk text, not a source coordinate — there
   is still no page number, byte offset, or line range on a chunk, because
   nothing stores one (see item 4). `mcp_server.py` also does not surface the
   new fields yet; `POST /query` does.

Not scheduled, with reasons rather than vague deferral:

- **ANN vector index.** ADR 0001's own revisit trigger is sustained usage
  above roughly 50k chunks. No such usage exists, and the README commits to
  personal-vault scale. Building it now would contradict a decision this repo
  already wrote down.
- **Zotero bridge.** The clearest unmet need in the surrounding tooling, and
  the least coupled to everything above. It reads another application's live
  SQLite, which is a new trust boundary and wants its own security review
  before any code.
- **OCR.** The PDF adapter refuses a scanned document rather than guessing.
  Adding OCR means deciding what confidence an OCR'd citation carries, which
  is a citation-integrity question and not a dependency question.

The deferred items in this file are real, and none are hidden.
