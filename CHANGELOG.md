# Changelog

All notable changes to `vault-retrieval-engine` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org/).

Entries under `Unreleased` may still slip. Carried-over gaps are tracked
in [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md).

## [Unreleased]

### Changed
- **`vault-engine search` now runs all three retrieval channels, and its
  output changed to say so.** This is a user-visible change to a released
  command. The project is 0.x, so SemVer permits it; the reasoning is below
  rather than left implicit.
  - *What changed in the evidence base.* `cli.search` built a `Retrieval` and
    called `Retrieval.search`, which encodes the query and calls `vec.search`,
    the vector channel and nothing else. It now builds a `Router` from the
    already-open `Indexer` and calls `Router.dispatch`, which fans out to
    vector, lexical (BM25), and topology, and fuses with RRF. That is the
    same path `POST /query` has always used, and the same path `EvalRunner`
    has always measured. Three surfaces, one answer.
  - *What changed on screen.* The per-hit header was
    `<slug> #<chunk_idx> dist=<float>`. It is now
    `<slug> #<chunk_idx> rrf=<float> channels=<csv>`. `dist=` is gone: a raw
    embedding distance describes one channel, and there are three now, so
    reporting it as *the* score would misstate what produced the hit. `rrf=`
    is the fused score; `channels=` names which of `vector`, `lexical`, and
    `topology` contributed, deduped and in fusion order. The chunk index and
    the chunk excerpt both survive unchanged, because the excerpt is what
    makes `search` readable in a terminal. A page that only the topology
    channel found has no chunk to name, and prints `#-` with an explicit
    `(no chunk text: topology hit)` in place of a fabricated excerpt.
  - *Why now.* The CLI was quietly the weakest of the three surfaces while
    the README described the engine as three-channel. Chunk identity through
    the router (below) is what made the move lossless: `Router.dispatch` can
    now supply the chunk index and chunk text that `search` was already
    printing, so nothing had to be dropped to gain the extra channels.
  - Ranking moves, because the evidence base moved. On the fixture vault,
    `search "alpha protocol"` returned `zeta, alpha, gamma` and now returns
    `alpha, zeta, gamma`. The eval rig is unaffected (still 6/6): it was
    already calling `Router.dispatch` and was never measuring the CLI path.
  - No `Service` method was added, no lifecycle mode was added, and
    `Retrieval` is untouched. `status`, `reindex`, `expand`, and `source` are
    untouched.

### Fixed
- `search`, `expand`, and `source` no longer lose `[[wikilinks]]` from printed
  vault content. `console.print` parsed them as rich markup tags and deleted
  them, so an excerpt reading `Alpha references [[beta]]` on disk printed as
  `Alpha references []`. Those three sites now print with `markup=False`.
- Three rows of the README CLI table described commands that do not exist as
  written. Drift predating this change, corrected alongside it: `search` was
  documented as returning "citation chains" (it returns none; `POST /query`
  is the surface that assembles them), `expand` as a "Multi-hop graph walk
  from a seed page" (it prints one page's body and walks nothing), and
  `source` as resolving `wiki/topics/<page>` "to its source pages" (it takes
  a `wiki/sources/` slug and prints the single raw file named by that page's
  `raw_path`, verbatim; a topics page has no `raw_path` and exits 1).
- `vault-engine add` no longer crashes when `--vault` points at a symlinked
  root (`/tmp` resolves to `/private/tmp` on macOS). Both adapters return a
  resolved path, so the closing `path.relative_to(vault)` raised
  `ValueError` before printing anything. Pre-existing on the URL path too;
  found by running the CLI rather than only the tests.
- PDF pages with no extractable text layer are counted and reported rather
  than silently dropped. `extract_pdf_markdown` / `add_pdf` take the same
  optional `skipped: list[SkippedPage]` out-parameter `vault_reader.iter_pages`
  takes, and `vault-engine add` prints `pages skipped (unreadable): N`
  followed by one line per page, matching `status` and `reindex`. Before
  this, a 4-page PDF with two image-only pages wrote `## p. 1` then
  `## p. 4` and said nothing.
- A failed page write no longer leaves the retained original behind. The
  original is still written first so its own guards run before anything
  lands, but it is removed if `write_raw_file` then fails.

### Added
- Chunk identity survives the router and RRF (roadmap item 5). `RankedHit`
  and `FusedHit` gained `chunk_idx` / `content`, and `FusedHit` gained
  `per_channel_chunks`. `Router._vector_search` was discarding the
  `chunk_idx` and `content` that `VecHit` already carried, and
  `_lexical_search` was collapsing its best-chunk-per-page dedupe to a bare
  slug, so nothing downstream of the router could say which chunk matched —
  only which page. This is the prerequisite the roadmap named for
  chunk-level provenance (source coordinates) reaching a transport, and for
  the CLI moving onto `Service` (`vault-engine search` prints a chunk index
  and chunk text that `Service.query` could not supply).
  - Fusion is unchanged: RRF still accumulates on the page slug. Chunk
    identity is carried on the hit and never used as a fusion key, so the
    ranking is byte-identical (verified by diffing the fused ranking for
    every eval fixture query before and after; the eval rig stays 6/6).
  - When channels disagree about which chunk matched a page, the fused hit
    reports the chunk from the single best-ranked channel contribution, and
    `per_channel_chunks` keeps every channel's pick so the disagreement is
    inspectable rather than discarded. Rank ties break on channel order
    (vector, lexical, topology), which makes the pick deterministic.
  - Topology hits are page-level and report `chunk_idx = None` rather than a
    fabricated 0.
  - `POST /query` gained two additive keys per fused hit, `chunk_idx` and
    `per_channel_chunks`. No existing key changed; `content` is deliberately
    not returned over HTTP.
- Local-file PDF ingestion. `vault-engine add ./paper.pdf --vault <path>`
  extracts a PDF's text layer with `pypdf` and writes `raw/<slug>.md` with
  one `## p. N` section per text-bearing page. Page markers are ordinary H2
  headings, so `chunker.chunk_page` (which splits on H1/H2 and keeps the
  heading line in the chunk text) carries them into the vector store and the
  FTS index with no schema change. `add` routes an `http(s)` argument to the
  existing URL adapter and anything else to the PDF adapter; there is no
  remote PDF fetch, which keeps the SSRF surface `url_ingester` closes shut.
- ADR 0006 artifact retention, implemented for PDFs. The source file is
  copied to `raw/_originals/<slug>.pdf` and the generated page records
  `source_artifact`, `source_sha256`, and `source_media_type` in
  frontmatter, so a citation chain can name the original a claim descends
  from and that claim is checkable. `url_ingester.write_raw_file` grew
  `source_type` and `extra_frontmatter` parameters to render these; both
  default to today's behaviour.
- `pypdf` dependency (BSD-3-Clause, compatible with this repo's MIT
  licence). PyMuPDF was rejected: it is AGPL-3.0.

### Security
- A PDF with no extractable text layer is refused with an error naming the
  file, rather than ingested as an empty page. There is no OCR.
- **Extracted text can no longer forge a page marker.** `chunker.chunk_page`
  splits on `^#{1,2}\s+` and labels the chunk with the heading, so a PDF
  whose page-2 text layer contained a line `## p. 1` produced a second chunk
  labelled `p. 1` — a document choosing its own citation in a tool built for
  trustworthy citation. The accidental variant needed no attacker: any
  extracted line opening with `# ` swallowed the page it came from. Every
  extracted line that opens with `#` at column 0 is now escaped. The page
  coordinate is still carried in band; moving it out of band (per-chunk
  metadata) is the durable fix, deferred to the coordinates ADR.
- **The retained-original traversal guard is anchored to the vault root**,
  not to an already-resolved `raw/_originals/`. Resolving the originals
  directory first made the containment check a tautology: with
  `raw/_originals` a *symlinked directory*, the destination resolved to the
  link target and the check passed, writing outside the vault. This matches
  what `url_ingester.write_raw_file` already did.
- **The extracted body is capped, not just the input file.** A 222 KB
  Flate-compressed PDF expands to a 24 MB text layer; at the 10 MiB input
  cap that extrapolates to roughly 1 GB. Such a page also exceeded
  `vault_reader._MAX_PAGE_BYTES`, so the indexer skipped it forever while
  `add` printed success and exited 0. Extraction now stops and refuses once
  the accumulated body passes `_MAX_BODY_BYTES` (`_MAX_PAGE_BYTES` less
  headroom for frontmatter), so anything written is guaranteed indexable.
- **The PDF parse boundary catches broadly.** pypdf raises `KeyError`,
  `RecursionError`, `struct.error`, and `AttributeError` on malformed input
  — a bogus font reference raised `KeyError: '/DescendantFonts'` straight
  through a three-exception handler and printed a Rich traceback exposing
  local absolute paths. Any exception out of the parser is now re-raised as
  `PdfIngestError` naming the file and the underlying exception type.
- Retaining an original never silently overwrites an existing one, because
  that would invalidate the `source_sha256` already recorded by whichever
  page retained it. Pass `--overwrite` to replace.

## [0.2.0] - 2026-07-30

100 commits since `v0.1.0`. The headline is a third retrieval channel
(BM25 lexical over FTS5, fused into RRF alongside vector and topology),
a per-save reindex that no longer stalls, and a large batch of security
work that had been sitting untagged.

### Added
- Lexical retrieval channel. `VecStore` now maintains a `chunks_fts` FTS5
  virtual table in lock-step with `chunks` across upsert / `delete_page` /
  `delete_chunk` (shared rowid), and `VecStore.search_lexical` runs BM25
  over it. `Router.dispatch` runs the lexical leg on every query and folds
  it into reciprocal rank fusion as a third ranking; hits report their
  originating channels (for example `channels=lexical,vector`). `_fts_query`
  quotes each token so user or page input cannot inject FTS5 operators.
  This is the leg that disambiguates the bag-of-words embedder weakness
  documented in `KNOWN_ISSUES.md`. (#35, `dc6d253`)
- `AMBIGUOUS` similarity-edge band in `inference.py`. Edges with cosine in
  `[inferred_threshold, 0.95)` are annotated `AMBIGUOUS` instead of
  `INFERRED`, so downstream consumers can down-weight the weak band that
  the bag-of-words failure modes live in. `graph_stats` reports the split.
  (#35, `dc6d253`)
- Bag-of-words adversarial eval as an embedder-swap regression gate:
  `src/vault_engine/bow_adversarial.py` (JSONL loader plus cosine pair
  scorer), `tests/fixtures/adversarial_bow.jsonl` (6 negation, 6 word-swap,
  4 shuffle pairs), and `tests/test_bow_adversarial.py`. Word-swap and
  shuffle classes are tracked as xfail because the default embedder really
  does fail them. (#30, `85fed88`)
- The adversarial gate now runs as a named step in the `eval-rig-mock` CI
  job rather than relying on generic pytest collection. (#36, `2fab32a`)
- Decision-trace graph node prototype: `graph_store.add_decision_trace` plus
  the `DECISION_TRACE_KIND` node kind, gated off by default behind
  `EngineConfig.decision_trace_enabled`. (#30, `85fed88`)
- macOS service path. `overlays/launchd/com.vault-retrieval.engine.plist`
  template, `scripts/install-launchd-service.sh` and its uninstall
  counterpart, and `docs/ios-shortcut.md` for hitting `POST /query` from a
  phone over Tailscale. The plist is rendered through Python so XML-escaping
  survives tokens containing `<>&`, and `ProgramArguments` invokes the engine
  without a shell. (#27, `513bc58`)
- `VAULT_ENGINE_*` env-var fallbacks across bind address, port, token, and
  cache dir, so both the launchd plist and the NSSM service can configure
  the engine fully without a config file. (#27, `513bc58`)
- `--embedder default|mock` on `vault-engine mcp` and `vault-engine serve`,
  matching the option `eval` already had. (#99, #100)
- Real stdio/JSON-RPC wire test in `tests/test_mcp_server.py`, spawning
  `vault-engine mcp` as a child process. Every prior test called handlers
  in-process, leaving `serve_stdio()` uncovered. (#99)
- `overlays/githooks/post-commit` dispatcher that walks `post-commit.d/*` in
  lexical order, plus the engine's own `10-vault-engine.sh` reindex piece
  split out of the legacy monolithic hook. (#13, #24)
- Shell smoke harnesses `tests/smoke_install_vault_overlays_dispatcher.sh`,
  `tests/smoke_post_commit_dispatcher.sh`, and
  `tests/smoke_check_blocked_terms.sh`, plus the CI `smoke` job that runs
  all three on every PR. (#15, #16)
- Deeper eval fixture coverage reporting. (#23)
- `scripts/demo-worktree-concurrency.sh`, a self-contained demo of two
  disjoint worktrees merging clean. (#20)
- `CHANGELOG.md` itself. (#17)

### Changed
- CI migrated to the central reusable paved path. `ci.yml` now calls the
  shared `ci-python-uv.yml` workflow (ruff, pyright, pytest, emitting the
  aggregate `ci-passed` check) and keeps only the two repo-specific jobs
  the paved path does not cover: `smoke` and `eval-rig-mock`. The bespoke
  gitleaks and zizmor jobs moved to the security baseline. (#42)
- Watcher throttling replaced with a trailing-edge debounce. The old
  leading-edge throttle fired on the first write of a burst and dropped
  every trailing write, leaving the index stale until an unrelated later
  edit. Each path now gets a `threading.Timer` reset on every event, and
  `VaultWatcher.stop()` cancels pending timers so a late callback cannot
  hit a torn-down service. (#31, `cf69a8d`)
- HTTP `/query` and `/graph/stats` are plain `def` rather than `async def`.
  They call synchronous, potentially multi-second service methods; declared
  sync, FastAPI runs them in its threadpool so a slow reindex or encode no
  longer blocks the event loop and `/health` stays responsive. (#31)
- Licensing standardized on MIT. (`797396e`)
- Dependabot moved to a daily cadence with grouped updates, and onto the
  `uv` ecosystem so `uv.lock` stays in sync with `pyproject.toml`.
  (`4a9084f`, `96c150a`)
- CodeQL scans daily instead of weekly. (`9d8dab4`)
- `scripts/install-vault-overlays.sh` installs the dispatcher only when
  absent or matching the legacy monolithic engine SHA. Custom user hooks
  are never overwritten; legacy hooks are auto-migrated with a
  `.legacy.bak` beside the new dispatcher. (#13)
- Public-repo genericization: owner names, private vault names, local
  filesystem paths, issue-tracker references, and tailnet IPs replaced with
  neutral template language across docs, scripts, overlays, and fixtures.
  Behavior and public interfaces unchanged. (`86b8c3e`)
- README reframed to lead with what the engine is and is not, stating the
  roughly-10k-page scale ceiling and the embedder's negation weakness up
  front, and documenting the lexical and RRF-fusion behavior that had
  shipped without docs. (#101)
- Pre-commit `ruff` pin aligned to `uv.lock` (0.7.4 to 0.15.12), so local
  lint matches CI. (#29)
- 48 grouped dependency and GitHub Actions bumps, including
  `actions/checkout` 4 to 6, `actions/setup-python` 5 to 6, and
  `ossf/scorecard-action` 2.4.1 to 2.4.3. (#10, #11, #12, and the
  Dependabot group PRs)

### Fixed
- Per-save reindex outage. `reindex_page` called `add_similarity_edges` on
  every file change, which re-fetched every page's chunk vectors from SQLite
  to rebuild the page-vector matrix from scratch. On a 1.5k-page synthetic
  vault that was about 2.3s of a roughly 2.5s reindex. The Indexer now
  caches the slug-to-vector map, `rebuild()` repopulates it wholesale, and
  `reindex_page()` refreshes only the changed slug. A cold-cache fallback
  repopulates fully. The bottleneck was the DB re-fetch, not the O(N^2)
  edge loop. (#32, `ea53951`)
- Silently-dropped pages are now surfaced. Unreadable and oversize pages
  were discarded without a word; `iter_pages` reports them as `SkippedPage`
  and `vault-engine status` prints the count and each path with its reason.
  (#31, `cf69a8d`)
- `--mock-embedder` was a silent no-op for `vault-engine mcp` and
  `vault-engine serve`. The Typer callback returns early for those commands
  before the embedder is built, so both constructed a bare `Service(cfg)`
  and downloaded roughly 670MB of SentenceTransformer while the flag claimed
  otherwise. Both now take their own `--embedder`. `hook` and `add` also
  short-circuit the callback but never construct a `Service`, so they were
  unaffected. (#99, #100)
- Router misclassified lookups that arrived via a vault alias rather than
  the canonical slug. (`80a5499`)
- `tests/smoke_install_vault_overlays_dispatcher.sh` now asserts what it
  claimed to assert: skill-overlay content, absence of a spurious
  `.legacy.bak`, byte-equality of `.legacy.bak` with the migrated hook,
  byte-equality of a refused custom hook (a `grep` had been passing on an
  append), and a genuine re-run idempotence scenario. (#99)
- False configuration and security claims in the README corrected: there is
  no `vault.toml` config layer, the graph is in-memory and never persisted,
  and `cache_dir` holds `embeddings.db` rather than a graph pickle.
  (#33, `b37e628`)
- Legacy hook migration smoke test stabilized. (#25)
- OGR-78 through OGR-81 hardening batch: blocked-term substring
  false-matches in hooks, a green pyright baseline, enforcement of the
  declared eval fixture contract, and a refreshed README status. (#22)
- Ruff drift on main (UP038 and a format wrap). (#28)

### Removed
- `EngineConfig.graph_pickle` and the `graph.pkl` reference in
  `install-windows-service.ps1`. Nothing ever read or wrote that file.
  (#33, `b37e628`)

### Security
- SSRF / DNS rebinding. `_is_unsafe_host` resolved via `gethostbyname`
  (IPv4 only, skipping every AAAA record) and returned a URL string, after
  which httpx re-resolved the hostname to connect, leaving a TOCTOU
  rebinding window that the docstring claimed was closed. Replaced with
  `_resolve_and_validate`, which uses `getaddrinfo` (A and AAAA), fails
  closed if any resolved address is private, loopback, link-local, or
  reserved, and returns a pinned IP. `fetch_url` connects to that exact IP
  while carrying the real hostname in the `Host` header and TLS SNI, so
  routing and certificate verification are unchanged, and every redirect
  hop is re-validated and re-pinned. The same commit confined `source()`
  reads and documented secret rotation. (#34, `2c1d48f`)
- CodeQL `py/bad-tag-filter` (high). The HTML fallback extractor missed
  `script` and `style` end tags carrying whitespace (`</script >`), letting
  script and style text leak into extracted note bodies. A follow-up
  extended the match to end tags with trailing junk, which browsers also
  treat as closing the element. (`0aa1c51`, `c0e1d65`)
- Dependency security advisories resolved by raising direct and transitive
  packages to patched versions in the lockfile: cryptography 47.0.0 to
  49.0.0, idna 3.13 to 3.18, mistune 3.2.0 to 3.2.1, pyjwt 2.12.1 to
  2.13.0, python-multipart 0.0.26 to 0.0.32, starlette 1.0.0 to 1.3.1,
  urllib3 2.6.3 to 2.7.0, and torch. No application code or `pyproject`
  pins changed. (#41, `3e3592e`)
- torch advisories unblocked by moving the CUDA wheel index. The
  `pytorch-cu124` index capped torch at 2.6.0, leaving open advisories with
  no reachable patched version; the index moved to cu128 (torch 2.11.0) and
  then to cu130 to clear a later group bump that raised the floor past what
  cu128 carried. A Dependabot ignore rule now holds torch major and minor
  bumps so future PRs cannot outrun the pinned CUDA index; patch and
  security updates still flow. (#47, `5949cb4`; #59, `0306613`)
- Blocked-term literals moved out of the repository. The inline
  `BLOCKED_TERMS` array committed the exact client and employer names the
  scanner exists to keep out of a public repo. Literals now load from a
  gitignored file and history was rewritten to purge the terms from every
  past commit. (`c872fa7`)
- Central security workflows pinned. (#83, `03301ae`)
- `dependabot-sweep` token scoped to job level. Contents and pull-requests
  write moved out of top-level `permissions`, which now defaults to
  `contents: read`. Resolves the Scorecard Token-Permissions finding.
  (#80, `64f5128`)
- `persist-credentials: false` on the CodeQL checkout, clearing the zizmor
  artipacked finding. (`414b6a6`)
- CI supply-chain hardening: the unpinned `curl | sh` uv install replaced
  with SHA-pinned `astral-sh/setup-uv`, `uv sync` switched to
  `uv sync --locked` so a stale lockfile fails the build instead of being
  silently resolved around, and `timeout-minutes` added to every job that
  supports the key. (#78, `dd50e4a`)
- New serverless security automation: `codeql.yml` and
  `dependabot-sweep.yml`. (`4f4e9d7`, `61763c2`)

## [0.1.0] — 2026-05-04

First public-quality release. Local semantic retrieval engine over
personal markdown vaults — no external API, citation chains for
auditable retrieval, eval harness with latency SLOs.

### Added
- Header-aware chunking + sentence-transformers embeddings
  (mxbai-embed-large default; nomic + MiniLM also supported).
- sqlite-vec vector store with checksum-based encode-skip.
- NetworkX graph store with EXTRACTED + INFERRED edges
  (cosine threshold 0.85 calibrated for vault topology).
- Heuristic router (LOOKUP / SEMANTIC / MULTI_HOP / HYBRID).
- Citation chains: chunk → page → sources[] → raw chain assembler.
- Three transport surfaces:
  - Typer CLI (`vault-engine status`, `reindex`, `search`, `expand`,
    `source`, `eval`, `add`, `mcp`, `serve`, `hook`).
  - MCP stdio with 10 tools (Graphify-compatible).
  - FastAPI HTTP/JSON with HS256 JWT auth + Tailscale binding.
- Watcher auto-reindex on filesystem changes.
- NSSM Windows service launcher.
- URL → `raw/` ingestion via `trafilatura` with SSRF / redirect / size
  guards.
- `overlays/skills/vault/{synth,crawl}.md` + initial monolithic
  `overlays/githooks/post-commit` overlay; installable into a target
  vault via `scripts/install-vault-overlays.sh`.
- Eval harness (JSONL fixtures, mock embedder, latency SLOs, page
  coverage assertions).
- 5 ADRs documenting non-obvious decisions (sqlite-vec, NetworkX,
  INFERRED threshold 0.85, router tiers, mxbai default model).
- 129 tests; ruff + format clean; CI runs eval against the sample
  vault on every PR.

### Security & correctness
- All 12 P0 review findings addressed (security, correctness, docs, perf).
- 11 critical P1 fixes:
  - SSRF guards, request size caps (query body + top_k bounded at the
    validation layer), JWT exp claim required, refuse-to-bind on
    non-loopback without secret.
  - Service stop-race resolved, watcher rename emits both src + dest,
    slug collisions surface as `SlugCollisionError`, vec_store mutations
    atomic, file-size cap in reads.
  - README CLI table + eval-fixture schema match reality, sample vault
    expanded with multi-hop chains, alias chains, orphan; CI eval gate
    uses real expected_pages so it can fail.
- Performance P0s: graph walk replaced with bounded BFS, similarity-edge
  inference replaced with single-matmul, `reindex_page` does one disk
  walk instead of two.

### Known issues
Honestly tracked in [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md). v0.2.0 will
land slug-schema migration, the Service-CLI refactor, full GraphQuery
facade, and observability polish. See the v0.2.0 hardening epic
(tracked in an internal issue tracker) for the full scope.

[Unreleased]: https://github.com/itotallyforgot/vault-retrieval-engine/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/itotallyforgot/vault-retrieval-engine/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/itotallyforgot/vault-retrieval-engine/releases/tag/v0.1.0
