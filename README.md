# vault-engine

Local semantic retrieval engine over personal markdown vaults. No external API. Citation chains for auditable retrieval.

A plug-in for markdown/Obsidian-style vaults — overlays retrieval, semantic search, and citation chains onto a vault that runs standalone without it. Works with any markdown vault that uses wikilinks; the vault remains the source of truth and the engine is best-effort enrichment.

## Why

Cloud-hosted RAG creates three exposures that are unacceptable for personal knowledge bases that may contain sensitive material:

- **Inference-time data leakage.** Every query sends content to the provider. Vault content includes private notes, draft writing, work-in-progress thinking. Sending that to an external service trades retrieval quality for permanent loss of confidentiality.
- **Query log retention.** Most providers log queries. A query reveals what you're researching, who you're investigating, what you're worried about. Query metadata is its own intelligence stream.
- **Provider-side retention with unclear controls.** Even with deletion APIs, you're trusting the provider's word. There's no audit trail.

vault-engine takes the constraint seriously: **local-only, no external API, citation chains for auditable retrieval.** The architecture follows from the threat model. Embedding model loads from local cache. Vector store is sqlite-vec on local disk. Graph store is in-process NetworkX. Query results carry citation chains so you can verify what fed each answer.

If you've decided cloud RAG is fine for your use case, this isn't the right tool. If you've decided it isn't, the engine is shaped around that decision.

## What it does

- **Semantic search** over markdown chunks using local SentenceTransformer embeddings (mxbai, nomic, or MiniLM).
- **Multi-hop graph walks** over wikilink edges plus inferred similarity edges (cosine threshold calibrated for vault topology).
- **Citation chains** — each retrieved chunk traces back to its page and onward to source pages, producing a verifiable evidence trail.
- **Watcher** — auto-reindex on filesystem changes, so newly-edited pages are queryable within seconds.
- **Eval harness** — JSONL fixture runner with latency SLOs and page-coverage assertions. CI runs the eval against a mock embedder + sample vault.
- **Service surfaces** — MCP stdio (Claude Code, Codex, Cursor) and HTTP/JSON (Tailscale) in addition to the CLI.
- **No external API** — all retrieval, embedding, and storage is local. Embedding model loads from local Hugging Face cache.

## Architecture

```
                +------------------------------+
                |   wiki/  +  raw/  (vault)    |
                +---------------+--------------+
                                |
                  watcher sees fs events
                                |
                                v
+------------------------+  +------------+  +-------------------+
|  Indexer.rebuild()     |  | GraphStore |  |     VecStore      |
|  - chunk_page()        |->| NetworkX   |  | sqlite-vec        |
|  - encode-skip per     |  | EXTRACTED  |  | per-chunk         |
|    chunk checksum      |  | + INFERRED |  | mxbai/nomic/mini  |
+------------------------+  +-----+------+  +---------+---------+
                                  |                   |
                                  v                   v
                         +----------------------------------+
                         |          Retrieval               |
                         |  router  -> LOOKUP / SEMANTIC /  |
                         |              MULTI_HOP / HYBRID  |
                         |  citations.assemble_chain()      |
                         +-----------------+----------------+
                                           |
                          +----------------+----------------+
                          |                |                |
                          v                v                v
                       Typer CLI       MCP stdio       HTTP/JSON
                                                       (Tailscale + JWT)
```

The vault filesystem is the only source of truth. The engine is best-effort enrichment. See [Resilience](#resilience).

## Quick start

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/itotallyforgot/vault-retrieval-engine.git
cd vault-retrieval-engine
uv sync

# Index a vault. tests/fixtures/sample_vault is a small synthetic vault
# you can use to try the engine without pointing at your own.
uv run vault-engine --vault tests/fixtures/sample_vault reindex

# Search.
uv run vault-engine --vault tests/fixtures/sample_vault search "alpha protocol"

# Run the eval harness against the same fixture vault.
uv run vault-engine --vault tests/fixtures/sample_vault eval \
    --fixtures tests/fixtures/eval_fixtures.jsonl \
    --embedder mock
```

The `mock` embedder is fast and deterministic for iteration. Switch to `sentence-transformer` once you've decided on a model.

## CLI

| Command | What it does |
|---|---|
| `vault-engine status` | Show vault path, vec store stats, graph stats, last reindex |
| `vault-engine reindex [--force]` | Rebuild the index from the vault. Encode-skip on unchanged chunks. |
| `vault-engine search <query> [--k N]` | Top-k semantic results with citation chains |
| `vault-engine expand <wikilink>` | Multi-hop graph walk from a seed page |
| `vault-engine source <page>` | Resolve `wiki/topics/<page>` → its source pages |
| `vault-engine eval --fixtures <path> [--embedder mock\|default]` | Run the JSONL fixture eval; assert latency + page-coverage |
| `vault-engine add <url> --vault <path>` | One-shot scrape a URL into `raw/` (trafilatura extraction). Note: `add`, `serve`, `mcp`, and `hook` define their own `--vault` flag; placement matters. |
| `vault-engine mcp --vault <path>` | Start MCP stdio server |
| `vault-engine serve --vault <path>` | Start HTTP/JSON server |
| `vault-engine hook install --vault <path>` | Install the vault-side Glob/Grep hook |

Run `vault-engine <command> --help` for full options.

## Service mode

After indexing, the engine can run as a long-lived service with two transport surfaces.

### MCP stdio (Claude Code, Codex, Cursor)

```bash
uv run vault-engine mcp --vault ~/Projects/your-vault
```

Tool surface:

| Tool | HTTP equivalent | Description |
|---|---|---|
| `query_graph` | `POST /query` | Multi-hop graph search (vector + lexical BM25 + topology RRF) |
| `graph_stats` | `GET /graph/stats` | Counts |
| `get_node` | — | Node details |
| `get_neighbors` | — | Direct neighbors with edge metadata |
| `get_community` | — | Members of a Louvain community |
| `god_nodes` | — | Most-connected concepts |
| `shortest_path` | — | Citation chain between two concepts |
| `find_topic_page` | — | Locate a `wiki/topics/*` page |
| `find_unlinked_references` | — | Candidate alias matches |
| `get_linked_references` | — | Inbound wikilinks to a page |

### HTTP/JSON (Tailscale)

```bash
# Tailscale up, 100.x.y.z address assigned.
# Bind/port/token come from EngineConfig (loopback by default).
uv run vault-engine serve --vault ~/Projects/your-vault
```

Bind to the tailnet IP and require a token via `EngineConfig` (`http_bind_addr`, `http_port`, `http_token`) or env vars (`VAULT_ENGINE_BIND_ADDR`, `VAULT_ENGINE_HTTP_PORT`, `VAULT_ENGINE_HTTP_TOKEN`, `VAULT_ENGINE_CACHE_DIR`). Env-var precedence is `env-var > function-arg > dataclass-default`; the launchd plist and NSSM service both rely on this so the install scripts can fully configure the engine without a TOML file.

For long-lived service mode see `docs/windows-service.md` (PC, NSSM) and the new macOS path:

```bash
./scripts/install-launchd-service.sh --vault ~/Projects/your-vault \
    --bind <tailnet-ip-or-127.0.0.1> --token <bearer-secret>
```

Phone-side: `docs/ios-shortcut.md` walks through an iOS Shortcut that
hits `POST /query` over Tailscale (Authorization: Bearer).

`/health` (no auth) returns `{"status":"ok","running":bool}`.

Generate a JWT:

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
# then sign:
uv run python -c "import jwt; print(jwt.encode({'sub':'vault-engine'}, '<secret>', algorithm='HS256'))"
```

Use as `Authorization: Bearer <token>` for `POST /query` and `GET /graph/stats`.

### Vault-side hook

```bash
uv run vault-engine hook install --vault ~/Projects/your-vault
```

Installs a hint that nudges Claude Code Glob/Grep calls inside the vault toward `/vault query` first. The installer is idempotent and per-OS (`.sh` on macOS/Linux, `.ps1` on Windows).

## Configuration

Engine config is layered (later wins):

1. Defaults in `src/vault_engine/config.py`
2. Environment variables (`VAULT_ENGINE_*`)
3. CLI flags

There is no config-file layer — config is a plain dataclass populated from defaults, the `VAULT_ENGINE_*` env vars (read by `load_config`), and CLI flags. The graph is held in memory and rebuilt at startup; it is not persisted to disk.

Common knobs:

| Setting | Default | Env var | Notes |
|---|---|---|---|
| `vault_path` | `--vault` flag required | — | The directory containing `wiki/` and `raw/` |
| `cache_dir` | `~/.cache/vault-engine` | `VAULT_ENGINE_CACHE_DIR` | Embedding cache + vec DB (`embeddings.db`) |
| `embedding_model` | `mxbai-embed-large-v1` | — | Or `nomic-embed-text-v1.5`, `all-MiniLM-L6-v2` |
| `inferred_threshold` | `0.85` | — | Cosine threshold for INFERRED graph edges |
| `http_bind_addr` | `127.0.0.1` | `VAULT_ENGINE_BIND_ADDR` | HTTP server bind interface (private by default) |
| `http_port` | `7842` | `VAULT_ENGINE_HTTP_PORT` | HTTP server port |
| `http_token` | — | `VAULT_ENGINE_HTTP_TOKEN` | Bearer secret for HTTP auth (HS256 JWT or pre-shared) |

## Eval methodology

The eval harness runs queries against a fixture file in JSONL. Each fixture asserts:

- **Latency SLO:** `max_latency_ms` end-to-end per query
- **Page coverage:** top-k results must include named pages (e.g., `expected_pages: ["alpha", "beta"]`)
- **Negative coverage:** top-k results must not include named pages (e.g., `forbidden_pages: ["orphan"]`)
- **Mode classification:** query routes correctly: `lookup`, `semantic`, `multi_hop`, or `hybrid`
- **Citation depth:** `min_citation_depth` lower bound on chunk -> page -> source chain length
- **Citation correctness:** `expected_citations` must appear in the assembled citation chain
- **Eval track:** optional `track` groups rows by claim type, such as `retrieval_coverage` or `verification`
- **Candidate window:** optional `top_k` narrows the result set for negative coverage checks

Sample fixture entry shape:

```jsonl
{"id": "source-citation-alpha", "query": "alpha source provenance", "expected_pages": ["alpha", "2026-01-01-alpha-source"], "expected_citations": ["2026-01-01-alpha-source"], "min_citation_depth": 1, "mode": "hybrid", "track": "verification", "top_k": 20, "max_latency_ms": 5000}
```

The CLI report prints overall pass/fail plus latency buckets by `mode` and `track`. See `tests/fixtures/eval_fixtures.jsonl` for the full schema in use.

CI runs the eval on every push using the mock embedder against `tests/fixtures/sample_vault`. Production runs use the real embedder against the real vault.

## Resilience

The vault filesystem is the source of truth. The engine is enrichment. Consumers fall back gracefully when the engine is unavailable:

- `vault-engine reindex` rebuilds from vault truth — wiping the vec store is recoverable.
- The engine never writes to `wiki/` or `raw/`. The vault contract is read-only.
- Filesystem operations work without the engine.
- HTTP and MCP transports are optional surfaces; CLI is always the baseline.

If the engine is uninstalled, crashed, or unreachable, no vault content is lost. Reindex from scratch takes seconds-to-minutes depending on vault size.

## Plug-in pattern

The vault runs standalone. Integrations between vault and engine are designed as overlays installed from this repo, not as plumbing inside the vault:

- **`vault-engine hook install --vault <path>`** — installs Claude Code Glob/Grep hint that prefers `/vault query` (idempotent, per-OS).
- **`scripts/install-windows-service.ps1`** — registers an NSSM service for engine HTTP/MCP on PC. Wires `VAULT_ENGINE_BIND_ADDR`, `VAULT_ENGINE_HTTP_PORT`, and `VAULT_ENGINE_HTTP_TOKEN` into the service environment via NSSM's `AppEnvironmentExtra`.
- **`scripts/install-launchd-service.sh`** — registers a macOS LaunchAgent for the same engine HTTP/MCP surface. Mirrors the NSSM env-var conventions so the same token/bind/port wiring works across machines. See `overlays/launchd/com.vault-retrieval.engine.plist` for the template and `docs/ios-shortcut.md` for the phone-to-engine path over Tailscale.
- **`scripts/install-vault-overlays.sh`** — drops engine-aware vault overlays into a target vault:
  - `skills/vault/synth.md` — engine-aware insight-discovery skill (uses MCP `query_graph`).
  - `skills/vault/crawl.md` — engine-aware URL → `raw/` scrape skill (wraps `vault-engine add`).
  - `.githooks/post-commit` — fires `vault-engine reindex` after every commit. Graceful no-op when the engine isn't on PATH, so the hook is safe to keep installed even after engine removal.

A vault without these overlays still works — the engine remains an opt-in performance / capability boost, never a dependency. Vault skills that benefit from the engine (e.g. `query.md`) include lexical fallbacks.

### Installing overlays into a vault

```bash
# From the engine repo:
./scripts/install-vault-overlays.sh --vault /path/to/your/vault

# Then point git at the vault's hooks (one-time):
git -C /path/to/your/vault config core.hooksPath .githooks
```

The installer is idempotent — re-running reports skipped vs updated vs new files. Pass `--dry-run` to preview without writing.

## Project structure

```
src/vault_engine/
  __init__.py
  cli.py            # Typer commands: status, reindex, search, expand, source, eval, add, mcp, serve, hook
  config.py         # EngineConfig dataclass + load_config() (env-var aware)
  vault_reader.py   # markdown reader, frontmatter parser, wikilink extractor
  chunker.py        # header-section chunker with checksum
  embedder.py       # SentenceTransformer + MockEmbedder
  indexer.py        # orchestrate chunk + embed + vec/graph stores
  router.py         # heuristic LOOKUP/SEMANTIC/MULTI_HOP/HYBRID classifier
  retrieval.py      # search, expand, multi_hop, graph_walk
  citations.py      # chunk -> page -> sources[] -> raw chain assembler
  eval.py           # JSONL fixture runner with latency + coverage assertions
  watcher.py        # watchdog adapter for fs events
  stores/
    vec_store.py    # sqlite-vec adapter with checksum skip
    graph_store.py  # NetworkX DiGraph with alias resolution + BFS walk

tests/
  test_*.py         # pytest, mock embedder, isolated fixtures
  fixtures/
    sample_vault/   # tiny synthetic vault for tests + demos
    eval_fixtures.jsonl

scripts/
  check-blocked-terms.sh   # pre-commit blocked-terms scan
  smoke_real_vault.sh      # end-to-end test on a real vault

.github/workflows/
  ci.yml            # gitleaks + zizmor + ruff + pytest + eval-rig-mock
  security.yml      # ossf scorecard + dependency-review
```

## Development

```bash
# Install dev deps.
uv sync --group dev

# Install pre-commit hooks. The blocked-terms scan checks for sensitive
# terms before every commit so contributors don't accidentally leak.
pip install pre-commit
pre-commit install
pre-commit install --hook-type commit-msg

# Run tests.
uv run pytest -q

# Lint + format.
uv run ruff check .
uv run ruff format .

# Run the eval rig against the sample vault.
uv run vault-engine --vault tests/fixtures/sample_vault eval \
    --fixtures tests/fixtures/eval_fixtures.jsonl \
    --embedder mock
```

Conventional Commits format. CI runs gitleaks + zizmor + ruff + pytest + pyright + eval-rig-mock on pull requests and configured push branches.

## Architecture decisions

See [`docs/adr/`](docs/adr/README.md) for ADRs covering the non-obvious choices: sqlite-vec, NetworkX, the 0.85 INFERRED edge threshold, the router's mode boundaries, and the default embedding model.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Status

**v0.1.0 shipped** (2026-05-04, tag `v0.1.0`) — Phase 3 complete: encode-skip, INFERRED edges, NSSM Windows service, post-commit auto-reindex hook, URL → `raw/` adapter, ripgrep fallback. All P0 review findings addressed; 11 critical P1 fixes; 5 ADRs. Current local collection: 146 tests. See [`CHANGELOG.md`](./CHANGELOG.md) for the release notes and [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md) for honest carry-overs.

**Current status**: post-v0.1.0 hardening is tracked as the v0.2.0 hardening epic (tracked in an internal issue tracker). Recent work has landed in `main`; see the `Unreleased` section of [`CHANGELOG.md`](./CHANGELOG.md) for shipped slices and [`KNOWN_ISSUES.md`](./KNOWN_ISSUES.md) for deferred items.
