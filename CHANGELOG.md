# Changelog

All notable changes to `vault-retrieval-engine` are documented here. The
format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
this project follows [Semantic Versioning](https://semver.org/).

Versions in `Unreleased` may still slip; the actively-tracked roadmap
lives at the [v0.2.0 hardening epic](https://linear.app/ogre-labs/issue/OGR-19).

## [Unreleased]

### Added
- `overlays/githooks/post-commit` — vault-owned dispatcher pattern that
  walks `post-commit.d/*` in lexical order. Plug-ins drop their own
  `<NN>-<plugin>.sh`; engine claims `10-` so future plug-ins can sequence
  around it. ([slice 1 of standalone-refactor], #13)
- `overlays/githooks/post-commit.d/10-vault-engine.sh` — engine's reindex
  piece, extracted from the legacy monolithic hook. (#13)
- `tests/smoke_install_vault_overlays_dispatcher.sh` — bash smoke harness
  for `install-vault-overlays.sh` covering fresh-install,
  legacy-monolithic-hook auto-migration, custom-hook refusal, and re-run
  idempotency. Alongside `tests/smoke_post_commit_dispatcher.sh` (dispatcher
  ordering) and `tests/smoke_check_blocked_terms.sh` (blocked-term scanner).
  (#15)
- CI job `smoke` running all three shell smoke harnesses on every PR. (#16)
- README "Install onto your vault" section above the fold; opens with
  the canonical 3-step `install-vault-overlays.sh` flow. Top-of-README
  callout explicitly frames the engine as a plug-in for
  second-brain-template-shaped vaults. (#14)

### Changed
- `scripts/install-vault-overlays.sh` now installs the dispatcher only
  when absent or matching the legacy monolithic engine SHA
  (`b68cfa92…`). Custom user hooks are never overwritten; legacy hooks
  are auto-migrated with a `.legacy.bak` next to the new dispatcher. (#13)

### Bumped
- `actions/checkout` 4 → 6 (#12)
- `actions/setup-python` 5 → 6 (#11)
- `ossf/scorecard-action` 2.4.1 → 2.4.3 (#10)

[slice 1 of standalone-refactor]: https://linear.app/ogre-labs/issue/OGR-13

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
facade, and observability polish. See
[OGR-19](https://linear.app/ogre-labs/issue/OGR-19) for the epic.

[Unreleased]: https://github.com/itotallyforgot/vault-retrieval-engine/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/itotallyforgot/vault-retrieval-engine/releases/tag/v0.1.0
