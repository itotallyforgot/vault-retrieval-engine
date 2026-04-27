# vault-engine

Local retrieval engine over the Second-Brain vault.

See `_ops/2026-04-24-vault-retrieval-engine-design.md` in the vault for design.

## P2 — service mode

After P1 ships indexing + retrieval, P2 promotes the engine into a long-running
service with two transport surfaces.

**Run as MCP stdio (Claude Code, Codex, Cursor):**

```bash
uv run vault-engine mcp --vault ~/Projects/Second-Brain
```

**Run as HTTP/JSON over Tailscale:**

```bash
# 1. Bring up tailscale, ensure 100.x.y.z address is assigned
# 2. Run serve; bind/port/token come from EngineConfig (loopback by default).
uv run vault-engine serve --vault ~/Projects/Second-Brain
```

To bind to the tailnet IP and require a token, set the corresponding fields on
`EngineConfig` (`http_bind_addr`, `http_port`, `http_token`). On Windows the
`scripts/start_service_pc.ps1` launcher verifies the tailnet IP first.

**Install vault-side hook (E1):**

```bash
uv run vault-engine hook install --vault ~/Projects/Second-Brain
```

After install, Claude Code Glob/Grep calls in the vault see a hint to prefer
`/vault query` first. The installer is idempotent and per-OS (`.sh` on macOS/
Linux, `.ps1` on Windows).

**Tool surface (MCP + HTTP):**

| Tool                       | HTTP                | Description                                    |
| -------------------------- | ------------------- | ---------------------------------------------- |
| `query_graph`              | `POST /query`       | Multi-hop graph search (vector + topology RRF) |
| `graph_stats`              | `GET /graph/stats`  | Counts                                         |
| `get_node`                 | —                   | Node details                                   |
| `get_neighbors`            | —                   | Direct neighbors with edge metadata            |
| `get_community`            | —                   | Members of a Louvain community                 |
| `god_nodes`                | —                   | Most-connected concepts                        |
| `shortest_path`            | —                   | Citation chain between two concepts            |
| `find_topic_page`          | —                   | Locate a `wiki/topics/*` page                  |
| `find_unlinked_references` | —                   | Candidate alias matches (P2 stub)              |
| `get_linked_references`    | —                   | Inbound wikilinks to a page                    |

`/health` (no auth) returns `{"status":"ok","running":bool}`.

**Generate auth token:**

```bash
uv run python -c "import secrets; print(secrets.token_urlsafe(32))"
# then sign:
uv run python -c "import jwt; print(jwt.encode({'sub':'vault-engine'}, '<secret>', algorithm='HS256'))"
```

Use the resulting JWT as `Authorization: Bearer <token>` for `POST /query` and
`GET /graph/stats`. `/health` is always open.
