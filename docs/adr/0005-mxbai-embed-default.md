# ADR 0005 — Default embedding model: mxbai-embed-large-v1

**Status:** Accepted
**Date:** 2026-05-04

## Context

The engine ships configurable embedding models. The default matters because most users will accept it, and it shapes the calibration of every downstream artifact (INFERRED threshold, eval fixtures, latency budgets).

Constraints:

- **Local-only.** Wedge claim. Rules out OpenAI's text-embedding-* / Cohere / Voyage hosted models.
- **Quality.** Must be MTEB-competitive at retrieval tasks for personal-knowledge-base content.
- **Throughput.** Must be tolerable on CPU for the laptop/PC scale we target. GPU when available is a bonus, not a requirement.
- **Dim trade-off.** Higher dim = more disk + memory + compute per query. Lower dim = faster but lossier.

## Decision

**Default: `mixedbread-ai/mxbai-embed-large-v1` (1024-dim).**

Two configurable alternatives:

- `nomic-ai/nomic-embed-text-v1.5` (768-dim) — alternative for users who want lower dim with similar MTEB scores.
- `sentence-transformers/all-MiniLM-L6-v2` (384-dim) — fast, lightweight; quality drop is real but acceptable for tiny vaults.

## Alternatives considered

| Model | Dim | Why not default |
|---|---|---|
| **OpenAI text-embedding-3-large** | 3072 | Hosted. Violates wedge. |
| **Cohere embed-english-v3.0** | 1024 | Hosted. Violates wedge. |
| **all-mpnet-base-v2** | 768 | Solid, but mxbai-embed-large outperforms on retrieval-flavored MTEB tasks at the same dim class. |
| **bge-large-en-v1.5** | 1024 | Comparable quality, similar throughput. Could be the default. mxbai-embed-large picked for slightly stronger retrieval scores at time of selection (May 2026). |
| **e5-large-v2** | 1024 | Strong, but requires "passage:" / "query:" prefix discipline that complicates the engine's API. Avoided to keep encoding straightforward. |
| **MiniLM as default** | 384 | Quality drop on multi-hop / nuanced retrieval is meaningful. OK for tiny vaults but a poor default for portfolio-grade work. |

## Consequences

### Positive

- **MTEB-competitive.** mxbai-embed-large-v1 ranks among the top open models on retrieval tasks at time of selection.
- **Local cache.** Loads from `~/.cache/huggingface/`. First-run download is a few hundred MB; subsequent runs are instant.
- **No prefix discipline required.** Unlike e5, the model encodes queries and passages identically.
- **L2-normalized output.** Cosine and dot-product are equivalent, simplifying the INFERRED threshold reasoning (see ADR 0003).

### Negative

- **First-run download.** ~ several hundred MB pull from HuggingFace. Documented in README; offline-first users hit `--mock-embedder` for iteration before the model lands.
- **CPU latency.** Encoding is slower on CPU than smaller models. mxbai's encoder is heavier than MiniLM by an order of magnitude. Acceptable for batch indexing; users hitting the engine for many ad-hoc queries on CPU may prefer a smaller model.
- **1024-dim disk overhead.** Per-chunk vec store row carries a 1024×float32 = 4 KB embedding. At 50k chunks that's 200 MB of vec data. Tolerable; sqlite-vec compresses internally.

## Status flags

Revisit if:

- A new open-weights model meaningfully out-performs mxbai-embed-large on retrieval MTEB at similar throughput.
- Local-only constraint relaxes for some users (they may want higher-quality hosted embeddings).
- CPU latency complaints accumulate — bumping default to nomic-embed (768-dim) trades quality for throughput in a known-good way.
