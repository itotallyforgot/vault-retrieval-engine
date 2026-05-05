# ADR 0003 — INFERRED similarity edge threshold = 0.85

**Status:** Accepted
**Date:** 2026-05-04

## Context

The engine derives two kinds of graph edges:

- **EXTRACTED** — wikilinks parsed from page bodies. Authoritative; unambiguous.
- **INFERRED** — semantic-similarity edges computed from page-level cosine of mean-pooled chunk embeddings. Heuristic; rank-ordered.

INFERRED edges enrich the graph with topical neighbors that the user didn't explicitly link. They feed multi-hop walks, community detection, and the `god_nodes` ranking.

A cosine threshold gates which similarity scores qualify as edges. Too low: the graph fills with noise and every walk degenerates. Too high: the inference layer adds nothing.

## Decision

**Default `inferred_edge_threshold = 0.85`** (configurable via `EngineConfig`).

## Calibration data

Empirical run on a personal vault:

| Threshold | Edges added | Avg per node | Qualitative read |
|---|---|---|---|
| 0.80 | 7,058 | 21 | 62% of edges fall in the noise band [0.80, 0.83); weak co-domain matches dominate. Walks lose signal. |
| **0.85** (default) | **1,319** | **4** | Elbow. Topical neighbors surface, noise drops sharply. Graph stays interpretable. |
| 0.90 | 200 | 0.6 | High precision, very low recall. Most pages have zero inferred neighbors. Multi-hop becomes EXTRACTED-only. |

At 0.85 the top-confidence INFERRED edges are duplicate topic↔source bundles (e.g. a paper's wiki-source and its topic page co-cluster at 0.959) and tightly co-domain tutorial pairs (RLHF tutorial ↔ Fine-Tune-LLMs tutorial at 0.978). Spot-checks confirm these are useful; users would likely add them as wikilinks themselves.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Lower (0.75-0.80) | Noise band swamps signal; multi-hop walks degenerate. |
| Higher (0.90+) | Precision-correct but recall too low — engine adds little vs lexical-only. |
| Adaptive per-vault | Possible future work — calibrate per corpus from a held-out edge-quality eval. Out of scope for v0.1.0. |
| ANN-rank-K instead of threshold | Considered. Threshold is simpler; both produce comparable graphs at our scale. Revisit if vaults grow large enough that the per-pair cosine becomes too slow regardless. |

## Consequences

### Positive

- **Interpretable.** "Connect pages whose mean-pooled chunk vectors cosine ≥ 0.85" is a single sentence; users can inspect why an edge exists.
- **Calibrated, not hand-waved.** The 0.85 number is from a real corpus, documented above.
- **Configurable.** Vaults with different topology (e.g., highly redundant note-taking style) can tune.

### Negative

- **Vault-size-sensitive.** The 0.85 calibration is from a 339-page vault. Larger vaults may need recalibration. KNOWN_ISSUES.md tracks this.
- **Embedding-model-coupled.** Cosine values are model-dependent. The default 0.85 is calibrated for mxbai-embed-large-v1. Users switching embedding models should re-evaluate.

## Status flags

Revisit if:

- A second vault profile (~10k+ pages) shows the 0.85 default is wrong.
- A different default embedding model makes 0.85 inappropriate.
- Per-vault auto-calibration becomes feasible (e.g. via a held-out edge-quality test fixture).
