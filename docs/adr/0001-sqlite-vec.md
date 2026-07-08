# ADR 0001 — sqlite-vec for vector storage

**Status:** Accepted
**Date:** 2026-05-04
**Decision-maker:** project owner

## Context

vault-retrieval-engine needs persistent, queryable vector storage for chunk embeddings. The wedge claim is local-only / no-external-API. The vault scale we're optimizing for is **personal-knowledge-base size** (hundreds to low thousands of pages, tens of thousands of chunks), not enterprise-scale corpora.

Constraints:

- Single-process, single-user, on-laptop / on-PC. No server fleet.
- Python primary. Embedding pipeline already Python-resident.
- Portable on disk — store should travel with the user across machines, OR rebuild quickly from the vault if missing.
- No external API. Rules out hosted services (Pinecone, Weaviate Cloud, Qdrant Cloud).
- Embedding dimensionality: 384 / 768 / 1024 depending on model choice.

## Decision

**sqlite-vec** (the SQLite extension, not Postgres + pgvector).

## Alternatives considered

| Option | Why rejected |
|---|---|
| **faiss** | Industry-standard, but no native persistence — chunk metadata storage requires a parallel store + manual sync. Adds complexity vs sqlite-vec's vec0 virtual table that holds embeddings + auxiliary columns in one transaction. |
| **chromadb** | Heavier dependency tree; spawns a separate process by default; persistence layer is more opinionated. Overkill for single-user. |
| **lancedb** | Strong Rust core, columnar storage, attractive for analytics. But: heavier on-disk footprint, less mature Python ergonomics for the upsert + checksum-skip pattern, and the engine doesn't need its strengths (analytics queries over vectors). |
| **qdrant (embedded mode)** | Embeddable, but the supported deployment path is server-mode; embedded is less polished. Rust dependency, larger install. |
| **pgvector** | Requires running Postgres. Defeats single-binary portability. |
| **In-memory (numpy + pickle)** | Trivial for tiny vaults. Loses durability on crash. Linear search past ~10k chunks. |

## Consequences

### Positive

- **Single-file portability.** The vec store lives at one `.db` path. Backup, sync, archive, share — file-level operations.
- **Transactional consistency.** sqlite-vec's vec0 virtual table participates in SQLite transactions (used in `vec_store.py` for atomic upsert + chunk_meta updates).
- **No process model.** No supervisor, no port, no IPC. The engine runs as a Python process and the DB opens in that same process.
- **Familiar SQL.** Auxiliary columns (`page_slug`, `chunk_idx`, `checksum`, `content`) queryable with regular SQL alongside the vec MATCH operation.

### Negative

- **Performance ceiling at scale.** sqlite-vec's brute-force vec MATCH is acceptable at our target scale (~10k-50k chunks). Past that, ANN structures (faiss / hnswlib / qdrant) win. Documented in `KNOWN_ISSUES.md`.
- **Single-writer model.** SQLite's writer-lock means concurrent writes serialize. In our usage (one indexer + one query path coordinated by `Service._lock`) this is fine.
- **Dimensionality limit.** sqlite-vec supports our target dims (384-1024) cleanly. Multi-thousand-dim vectors would be less optimal, but we don't need them.

## Status flags

This decision should be revisited if:

- Vault scale crosses ~50k chunks consistently (vector-search latency starts to bite).
- A use case appears that needs ANN approximate-NN beyond brute-force MATCH.
- A different store gains a comparable single-file portability story.
