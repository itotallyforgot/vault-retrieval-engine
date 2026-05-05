# Architecture Decision Records

Numbered records of non-obvious architectural decisions. New ADRs go here; existing ADRs aren't rewritten when a decision is revisited — instead, supersede with a new ADR that references the old one.

## Index

| # | Title | Status |
|---|---|---|
| [0001](0001-sqlite-vec.md) | sqlite-vec for vector storage | Accepted |
| [0002](0002-networkx.md) | NetworkX for graph storage and traversal | Accepted |
| [0003](0003-inferred-edge-threshold.md) | INFERRED similarity edge threshold = 0.85 | Accepted |
| [0004](0004-router-tiers.md) | Router classifies queries into LOOKUP / SEMANTIC / MULTI_HOP / HYBRID | Accepted |
| [0005](0005-mxbai-embed-default.md) | Default embedding model: mxbai-embed-large-v1 | Accepted |

## Format

Each ADR follows: Context → Decision → Alternatives considered → Consequences (positive + negative) → Status flags (when to revisit).

Aim for one page per record. If an ADR exceeds two pages, the decision is probably under-decomposed; split it.
