# vault-engine

Local semantic retrieval engine over personal markdown vaults. No external API. Citation chains for auditable retrieval.

A plug-in for second-brain-template-shaped vaults — overlays retrieval, semantic search, and citation chains onto a vault that runs standalone without it. Works with any markdown vault that uses wikilinks; the vault remains the source of truth and the engine is best-effort enrichment.

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
| `vault-engine eval --fixtures <path> [--embedder mock\|st]` | Run the JSONL fixture eval; assert latency + page-coverage |
| `vault-engine add <url>` | One-shot scrape a URL into `raw/` (trafilatura extraction) |
| `vault-engine mcp --vault <path>` | Start MCP stdio server |
| `vault-engine serve --vault <path>` | Start HTTP/JSON server |
| `vault-engine hook install --vault <path>` | Install the vault-side Glob/Grep hook |

Run `vault-engine <command> --help` for full options.

## Service mode

After indexing, the engine can run as a long-lived service with two transport surfaces.

### MCP stdio (Claude Code, Codex, Cursor)

```bash
uv run vault-engine mcp --vault ~/Projects/Second-Brain
```

Tool surface:

| Tool | HTTP equivalent | Description |
|---|---|---|
| `query_graph` | `POST /query` | Multi-hop graph search (vector + topology RRF) |
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
uv run vault-engine serve --vault ~/Projects/Second-Brain
```

Bind to the tailnet IP and require a token via `EngineConfig` (`http_bind_addr`, `http_port`, `http_token`) or env vars.

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
uv run vault-engine hook install --vault ~/Projects/Second-Brain
```

Installs a hint that nudges Claude Code Glob/Grep calls inside the vault toward `/vault query` first. The installer is idempotent and per-OS (`.sh` on macOS/Linux, `.ps1` on Windows).

## Configuration

Engine config is layered (later wins):

1. Defaults in `src/vault_engine/config.py`
2. `vault.toml` at the vault root (if present)
3. Environment variables (`VAULT_ENGINE_*`)
4. CLI flags

Common knobs:

| Setting | Default | Notes |
|---|---|---|
| `vault_path` | `--vault` flag required | The directory containing `wiki/` and `raw/` |
| `cache_dir` | `~/.cache/vault-engine` | Embedding cache, vec DB, graph pickle |
| `embedding_model` | `mxbai-embed-large-v1` | Or `nomic-embed-text-v1.5`, `all-MiniLM-L6-v2` |
| `inferred_threshold` | `0.85` | Cosine threshold for INFERRED graph edges |
| `http_bind_addr` | `127.0.0.1` | HTTP server bind interface (private by default) |
| `http_port` | `7842` | HTTP server port |
| `http_token` | — | JWT secret for HTTP auth |

## Eval methodology

The eval harness runs queries against a fixture file in JSONL. Each fixture asserts:

- **Latency SLO** — `latency_ms_max` per query phase (chunk → embed → search → graph walk → citation)
- **Page coverage** — top-k results must include named pages (e.g., `expected_pages: ["alpha-protocol", "beta-handoff"]`)
- **Tier classification** — query routes correctly to LOOKUP, SEMANTIC, MULTI_HOP, or HYBRID

Sample fixture entry shape:

```jsonl
{"query": "what does alpha do?", "expected_pages": ["alpha"], "tier": "SEMANTIC", "latency_ms_max": 250}
```

CI runs the eval on every push using the mock embedder against `tests/fixtures/sample_vault`. Production runs use the real embedder against the real vault.

## Resilience

The vault filesystem is the source of truth. The engine is enrichment. Consumers fall back gracefully when the engine is unavailable:

- `vault-engine reindex` rebuilds from vault truth — wiping the vec store is recoverable.
- The engine never writes to `wiki/` or `raw/`. The vault contract is read-only.
- Filesystem operations work without the engine.
- HTTP and MCP transports are optional surfaces; CLI is always the baseline.

If the engine is uninstalled, crashed, or unreachable, no vault content is lost. Reindex from scratch takes seconds-to-minutes depending on vault size.

## Plug-in pattern

The second-brain template runs standalone. Integrations between vault and engine are designed as overlays installed from this repo, not as plumbing inside the vault:

- **`vault-engine hook install --vault <path>`** — installs Claude Code Glob/Grep hint that prefers `/vault query` (idempotent, per-OS).
- **`scripts/install-windows-service.ps1`** — registers an NSSM service for engine HTTP/MCP on PC.
- **Post-commit reindex hook** (overlay, planned) — drops `.githooks/post-commit` into the vault to fire `vault-engine reindex` after every commit.
- **Engine-aware vault skills** (overlay, planned) — `synth.md`, `crawl.md` that hard-require engine MCP, installable from this repo into the consumer's vault.

A vault without these overlays still works — the engine remains an opt-in performance / capability boost, never a dependency.

## Project structure

```
src/vault_engine/
  __init__.py
  cli.py            # Typer commands: status, reindex, search, expand, source, eval, add, mcp, serve, hook
  config.py         # Pydantic config model + load_config()
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

Conventional Commits format. CI runs gitleaks + zizmor + ruff + pytest + eval-rig-mock on every push to `main` or `p1-engine-core`.

## License

Apache License 2.0. See [LICENSE](LICENSE).

## Status

Phase 3 complete (encode-skip, INFERRED edges, NSSM Windows service, post-commit auto-reindex hook, URL → raw/ adapter, ripgrep fallback). Public v0.1.0 release pending hardening pass.
