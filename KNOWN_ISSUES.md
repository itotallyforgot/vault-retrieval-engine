# Known Issues

What the engine does not do, or does badly, as of the latest tagged release.
Listed so consumers can decide whether it fits their use case before
installing it. Every entry below was re-verified against the code on the
date in the header; entries that the code showed were already fixed have
been deleted rather than left to rot.

Last updated: 2026-08-03. The retained-original, citation-chain, and
`fetch_url` test-coverage entries were re-verified against `add <url>`
retaining its original (Unreleased); the retained-original entries were last
re-verified for the `vault-engine source` fix in v0.3.1, and the rest carry
the v0.3.0 verification date.

## Capability gaps

### PDF ingestion is local-file, text-layer only; no other non-markdown format

Shipped in v0.3.0: `vault-engine add ./paper.pdf --vault <path>` extracts
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
- **The retained original stays unindexed, and nothing re-verifies it in the
  background.** Applies to both adapters: since Unreleased `add <url>` also
  retains its original, so `raw/_originals/` now holds `.html` / `.xhtml` /
  `.txt` / `.bin` files alongside the PDFs, and the vault-growth cost ADR 0006
  lists as a known negative now applies to every ingest rather than only to
  PDF-heavy vaults. `vault_reader.iter_pages` still globs
  `vault_path.rglob("*.md")`, so nothing indexes `raw/_originals/`. Since
  v0.3.1, `vault-engine source <slug>` re-hashes the retained original
  and reports `integrity: ok` / `MISMATCH` / `MISSING` against the recorded
  `source_sha256` — but only when you ask for that one page. Nothing sweeps
  the vault, and neither `status` nor `reindex` notices a deleted or altered
  original. ADR 0006 records the absence of verification as a known negative;
  this narrows it to on-demand rather than closing it.
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

### The chunker's size cap counts words, not the embedder's tokens

Fixed since Unreleased: `chunk_page` splits a section over
`chunk_max_tokens` on paragraph boundaries (hard-splitting on words when a
single paragraph exceeds the cap on its own), so a page with one H1 and
8,000 words is no longer one chunk with one blended embedding. Both
`chunk_max_tokens` and `chunk_min_tokens` are now read — the indexer passes
them on both index paths. What that still does not give you:

- **"Tokens" are whitespace words.** The chunker has no tokenizer and does
  not load the model. English prose runs roughly 1.3 model tokens per word,
  so a section packed to the default 512 is about 670 tokens to
  mxbai-embed-large and is still truncated by its 512-token window. Set
  `chunk_max_tokens` to ~380 if you want the cap to respect that window.
  The gap between the cap and the model's real count is an approximation,
  not an exactness claim.
- **Undersized *sections* are still emitted alone.** Only an undersized
  *remainder* of a split section folds back into its previous sibling.
  Merging across a heading was implemented and then removed: on a
  PDF-ingested page it put page 2's text into a chunk labelled `p. 1`
  (`pdf_ingester` emits one `## p. N` H2 per printed page, and
  `Chunk.heading` is the only coordinate a chunk carries), and it dropped
  the mock eval rig from 6/6 to 5/6. So `chunk_min_tokens` bounds sliver
  chunks produced by splitting, and nothing else. A 4-word section is still
  a 4-word chunk.
- **Folding a remainder can exceed the cap** by up to `chunk_min_tokens - 1`
  words. Deliberate: a 20-word chunk is a worse vector than a 530-word one.
- **H3 and below still do not chunk.** Deeply-nested pages whose real
  structure lives at H3 are chunked as though that structure were not
  there, capped by size alone.
- **Existing caches converge on the next index, not automatically.** A
  store built before the cap re-chunks on any `vault-engine reindex` or on
  `Service.start()`; `--force` is not needed and no stale rows survive
  (`_index_page_chunks` drops indices the new chunk set does not have).
  The cost is real, though: splitting renumbers every chunk after the split
  point, and the checksum-skip is keyed on `(chunk_idx, checksum)`, so
  every chunk of a page that re-chunks is re-embedded even where its text
  did not change.

## Architecture

### The citation chain reaches no user-facing surface

`CitationAssembler` is imported by exactly one non-test module: `eval.py`. Not
`service.py`, not `http_server.py`, not `mcp_server.py`, not `cli.py`. So
`vault-engine search`, `POST /query`, and the MCP `query_graph` tool all return
hits and no chain. The assembler works and the eval harness asserts on it
(`min_citation_depth`, `expected_citations`); nothing a user touches calls it.

Two things have to change together, because fixing one alone ships an empty
chain:

- No transport calls `assemble`. `Service.query` returns `Router.dispatch`
  verbatim.
- Nothing the engine ingests is chainable. `CitationAssembler._walk` follows a
  `sources:` frontmatter list and resolves originals through `raw_path`, and
  no adapter writes either field. Both `add_pdf` and (since Unreleased)
  `add_url` write ADR 0006's `source_artifact` / `source_sha256` /
  `source_media_type`, which only `retrieval.source` reads —
  `vault-engine source <slug>` reports the retained original and its integrity
  for a page ingested either way, but the assembler still knows nothing about
  `source_artifact`, so citation depth on an ingested page is still zero.

The eval fixtures pass because `tests/fixtures/sample_vault` is hand-authored
to the `raw_path` convention the adapters do not emit. On a vault built with
`vault-engine add`, citation depth is zero for every hit.


### CLI bypasses Service

`status`, `reindex`, `search`, `expand`, `source`, and `eval` construct an
`Indexer` directly, and `expand`, `source`, and `eval` construct a
`Retrieval` on top of it. Only `serve` and `mcp` go through `Service`.

The retrieval half of this shipped in v0.3.0. `cli.search` builds a
`Router` from the already-open `Indexer` and calls `dispatch`, the same
three-channel RRF path `Service.query` uses, so `vault-engine search "..."`
and `POST /query` now answer from the same evidence. `EvalRunner` already
built its Router the same way, which is why the eval rig was measuring the
HTTP path rather than the CLI one.

What remains is object-graph duplication rather than a behavior gap.
All six commands still assemble their own `Indexer` instead of going
through `Service`. Making `Service` the single assembler needs a
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

### A cache built before the vault stamp is not audited, only adopted

Shipped in v0.3.0: `VecStore.open()` records the vault a store was built
from and refuses to open it from another (`VaultPathMismatch`). A store built
*before* that stamp existed carries no record of its origin, so it adopts
whichever vault opens it first. That is deliberate — the alternative is
forcing every existing user to re-embed on upgrade — but it means a cache
already contaminated by two vaults stays contaminated, silently, until
someone runs `vault-engine reindex --force`. The engine cannot tell a clean
pre-stamp store from a mixed one, because nothing recorded the difference at
the time. If you have ever run two vaults without `--cache`, assume yours is
mixed and force a rebuild once.

### The vault stamp compares path strings, so a moved vault costs a re-embed

The stamp is the resolved `vault_path` as text. Move or rename the vault
directory and the next open fails closed with `VaultPathMismatch`; the
documented recovery is `reindex --force`, which wipes the store and re-embeds
from scratch. On a large vault against the real model, that is minutes to
an hour. Content-addressing the vault instead of path-addressing it would
avoid the cost, and nothing here is designed to prevent that later; a symlink
farm or a bind mount will also trip it. Fail-closed-and-loud was chosen over
silently re-embedding, on the grounds that a retrieval tool that has already
mixed two corpora once should not fix that by adding a second silent
behavior.

### Pruning happens on `rebuild()`, not on every write path

`Indexer.rebuild()` now deletes slugs the vault no longer has. `reindex_page`
still handles only the file it was handed (drop-on-delete, drop-on-oversize);
it does not scan for other slugs that went missing. A long-lived `serve` /
`mcp` process that only ever receives watcher events therefore does not prune
until something calls `rebuild()` — which `Service.start()` does at startup,
so the window closes on restart rather than staying open forever.

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

- `url_ingester.fetch_url` is tested end to end for the cloud-metadata-IP
  refusal (monkeypatched `getaddrinfo`) and, since Unreleased, for the success
  path against a canned response: that it returns the wire bytes undecoded,
  the charset-decoded text, and the declared media type. The redirect chain,
  the content-type refusal, and the size cap are still covered only at the
  level of their helper functions, never through `fetch_url` itself.
- Watcher tests use timing-sensitive sleeps (up to 0.3s) and may flake on
  slow CI runners.
- `community.compute_communities` is tested directly, and
  `test_indexer_edge_type.py` asserts every node carries a community after
  `reindex_page`. Community ID *stability* across a single-page edit is
  still untested.

## Documentation state

Seven ADRs exist and all seven are on `main`, indexed in
`docs/adr/README.md`: sqlite-vec (0001), NetworkX (0002), the 0.85 INFERRED
threshold (0003), router tiers (0004), the mxbai default model (0005),
source-coordinate preservation (0006), and the lexical RRF channel (0007).
Six are Accepted. 0006 is still Proposed: it decided artifact retention and
deliberately stopped short of deciding how a coordinate is stored, which is
the open half described under Roadmap below.

## Roadmap

The four items v0.1.0 named as v0.2.0 work (slug-schema migration, the
Service-CLI refactor, the full GraphQuery facade, and observability polish)
did not land in v0.2.0. v0.2.0 shipped the lexical channel, the reindex
performance fix, concurrency hygiene, and a security backlog instead. Those
four remain open and are described above. No date is attached to anything
below, because the last estimate was wrong by about three months.

Two items this list carried are closed. **CLI uses the Router** and **chunk
identity through the router** both shipped in v0.3.0; what each one left
behind is folded into the items below rather than kept as a done entry.

Ordering, and the constraint that forces it:

1. **`schema_version` on `embedding_meta`, alone.** One additive column plus
   the migration ladder. Landing it by itself is what lets every later
   migration be a single-variable change, which is the opposite of the
   bundling an earlier draft of ADR 0006 proposed.
2. **Kind-prefixed slugs.** Blocked on a design question that has no answer
   yet: wikilinks in a vault are written `[[foo]]`, not `[[topic-foo]]`, so
   prefixed slugs need a bare-target resolution rule, and if both `topic-foo`
   and `raw-foo` exist the collision reappears at link-resolution time. A
   loud `SlugCollisionError` is a defensible state; a half-designed
   resolution rule is not.
3. **Source coordinates.** ADR 0006 deliberately stopped at artifact
   retention. The coordinates ADR should be written against what the PDF
   extractor actually emits, now that one exists. The first draft of 0006 was
   refuted precisely because it designed the storage before the producer.
   Its prerequisite, chunk identity through the router, is no longer in the
   way: `RankedHit` and `FusedHit` carry `chunk_idx` / `content` and
   `per_channel_chunks` as of v0.3.0. What that gives is an index and the
   chunk text, not a coordinate — there is still no page number, byte offset,
   or line range on a chunk, because nothing stores one. `POST /query`
   returns `chunk_idx` and `per_channel_chunks`; `mcp_server.py` does not
   surface either field yet.

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
