# ADR 0004 — Router classifies queries into LOOKUP / SEMANTIC / MULTI_HOP / HYBRID

**Status:** Accepted
**Date:** 2026-05-04

## Context

The engine has two retrieval channels:

- **Vector** — sqlite-vec embedding match
- **Topology** — graph walk over wikilink + INFERRED edges

A naive policy runs both for every query and fuses the results. That's expensive and noisy: a "what is X?" query doesn't benefit from a multi-hop graph walk, and an "X relates to Y" query needs both channels.

The router classifies a query into one of four modes, dispatches to the appropriate channels, and the result shape carries the classification so callers know which evidence chain was followed.

## Decision

**Four modes, heuristic classifier:**

| Mode | Dispatches | Use case |
|---|---|---|
| `LOOKUP` | Vec only (top-1 / top-3) | "What is X?" / "Find page for X" |
| `SEMANTIC` | Vec only (top-K) | "Tell me about Y" — broader retrieval, no graph context |
| `MULTI_HOP` | Topology graph walk seeded by vec hits | "How does X relate to Y?" / "What connects A through B?" |
| `HYBRID` | Vec + topology with RRF fusion | Default for ambiguous queries; gives both channels signal |

Classification heuristics in `router.py:_classify`:

- Title / alias exact match in vault → `LOOKUP`
- Query contains "how", "why", "relate", "connect", multiple capitalized terms → `MULTI_HOP`
- Short query (<8 words), no relation words → `SEMANTIC`
- Otherwise → `HYBRID`

## Alternatives considered

| Option | Why rejected |
|---|---|
| Always run HYBRID | Wastes work. Multi-hop is expensive on large graphs; running it for "what is X?" queries is pure overhead. |
| ML classifier | Out of scope at v0.1.0. Heuristic gets ~80% right with zero training data; we revisit if usage logs show systematic mis-classifications. |
| User-supplied mode flag (no auto-classify) | Friction — users want to ask questions, not pick modes. Mode is exposed in the API response so the user can inspect or override. |
| Single-channel always (no router) | Loses the dual-channel advantage that's the engine's wedge over plain vec-search. |

## Consequences

### Positive

- **Cheap classifier.** No model load, no remote call. Classification is sub-millisecond.
- **Explicit dispatch.** Result includes `intent` so callers know what was done; debugging is straightforward.
- **Easy to tune.** All heuristics in one function (`_classify`); changing a boundary doesn't ripple through the engine.

### Negative

- **Heuristic fragility.** Edge cases mis-classify. Documented as v0.2.0 work in `KNOWN_ISSUES.md`: parametrized boundary tests + possibly a small ML classifier seeded by usage logs.
- **Non-deterministic boundary.** The same query phrased differently can land in different modes. Consistent with how a human user would adjust their search strategy, but worth flagging.
- **No "tell me everything" mode.** Users wanting full graph dump fall back to direct CLI commands (`expand`, `source`).

## Status flags

Revisit if:

- Usage logs show systematic mis-classification (>20% of queries land in the wrong mode).
- A new retrieval channel is added (e.g. full-text BM25, summary-pool) that needs a 5th mode.
- Users start forcing modes manually in the wild — signal that the auto-classifier is wrong often enough to undermine its value.
