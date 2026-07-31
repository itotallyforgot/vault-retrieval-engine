# ADR 0007 — Lexical BM25 channel runs on every dispatch

**Status:** Accepted
**Date:** 2026-07-30
**Supersedes in part:** ADR 0004 (the mode dispatch table)

## Context

ADR 0004 described a two-channel engine, vector and topology, and gave each router mode a dispatch plan built from those two. A third channel has since shipped: an FTS5 index over chunk text, scored with BM25.

`VecStore` creates `chunks_fts` next to the `chunks` vec0 table, with `page_slug` and `chunk_idx` stored `UNINDEXED` so a hit can name its chunk, and with rowids mirroring `chunks` so deletes stay coordinated. `VecStore.search_lexical` runs the BM25 query and returns the score in `VecHit.distance`, negative and ascending, which matches the vector channel's lower-is-better convention. `Router._lexical_search` dedupes to the best chunk per page so a page matching in several chunks contributes once.

`Router.dispatch` calls `_lexical_search` on every query, before it decides whether topology runs. That makes ADR 0004's table wrong on all four rows: no mode is vector-only anymore, and `HYBRID` is no longer "vec + topology".

The reason the channel exists is a measured property of the default embedder, recorded below.

## Decision

**Vector and lexical run on every dispatch. Topology runs when a seed is supplied or the mode calls for it. Whatever channels return hits get fused by RRF.**

Corrected mode table:

| Mode | Channels dispatched now | ADR 0004 said |
|---|---|---|
| `LOOKUP` | Vector + lexical, RRF fused | Vec only (top-1 / top-3) |
| `SEMANTIC` | Vector + lexical, RRF fused | Vec only (top-K) |
| `MULTI_HOP` | Vector + lexical + topology walk, RRF fused | Topology graph walk seeded by vec hits |
| `HYBRID` | Vector + lexical + topology walk, RRF fused | Vec + topology with RRF fusion |

Three details the table doesn't carry:

- An explicit `seed_node` argument engages topology in any mode, including `LOOKUP` and `SEMANTIC`.
- Without an explicit seed, `MULTI_HOP` and `HYBRID` anchor the walk on the top vector hit via `_infer_seed`, and skip topology if that hit isn't a graph node.
- Each channel is fetched at `top_k * 2` and the fused list is truncated to `top_k`.

Everything else in ADR 0004 stands. The four modes, the heuristic classifier, and the `intent` field in the response shape are unchanged.

**Lexical runs unconditionally rather than only on `HYBRID`.** The tempting version of this change was to add the keyword leg to the one mode that already advertised itself as mixing signals. That would leave the weakness in place everywhere else. The embedder can't tell a claim from its negation, and nothing about the classifier's route makes that stop being true: a query that lands in `SEMANTIC` or `LOOKUP` is running on exactly the cosine that the measurements below say is unreliable on polarity and word order. The de-rate rule in `derate_for_negation` only catches queries carrying an explicit negation marker, and only from `SEMANTIC`. Keyword evidence is cheap enough to buy on every path instead of guessing which paths need it.

**RRF absorbed the third channel with no change to the fusion math.** `reciprocal_rank_fusion` takes a list of rankings and sums `1 / (k + rank)` across all of them, so it's N-ary already. The router builds `channels = [r for r in (vector_hits, lexical_hits, topology_hits) if r]` and passes it through. As the commit that shipped it put it, adding the channel was "a list entry". Rank-based fusion also sidesteps the score-scale problem that would otherwise bite here, since BM25 scores and cosine distances aren't comparable numbers.

## Motivating measurements

From the adversarial fixtures in `KNOWN_ISSUES.md`, run against `mxbai-embed-large-v1`:

| Pair type | Cosine | What it means for retrieval |
|---|---|---|
| Word-swap (same words, reordered to change meaning) | 0.96 to 0.99 | The embedder is close to blind here. Lexical ordering is the only signal that separates the pair. |
| Sentence shuffle | 0.94 to 0.99 | Same story. |
| Negation ("X is safe" vs "X is not safe") | 0.68 to 0.81 | Lower, still high enough that pure semantic ranking can surface the wrong polarity. |

These are the numbers that justify the channel. There is no measured end-to-end retrieval-quality delta from adding it: no scored eval compares fused ranking before and after. What exists is unit coverage in `tests/test_router_dual_channel.py` proving the channel runs, ranks a page by an exact keyword the mock vectors don't guarantee, and reaches the fused output's channel provenance. Treat the improvement as argued from the embedder's failure modes, not as benchmarked.

## Alternatives considered

| Option | Why rejected |
|---|---|
| Lexical only on `HYBRID` | Puts the keyword leg exactly where the classifier guessed the query was ambiguous. The embedder's blindness to word order isn't limited to queries the heuristic flags, so `LOOKUP` and `SEMANTIC` would keep ranking on the corrupted signal alone. |
| Cross-encoder reranker instead of BM25 | Better quality per hit and the likely long-term answer, but it means a second model load, per-candidate inference latency, and a new dependency in a local-only engine whose wedge is no external calls. BM25 is already inside SQLite and costs one extra query. Revisit as a fourth stage over the fused list rather than a replacement. |
| Weighted fusion (tuned per-channel weights) instead of RRF | Requires calibration data the project doesn't have, and the weights would be corpus-specific and embedder-specific. RRF needs no tuning and is insensitive to the mismatch between BM25 scores and cosine distances. |
| Keep semantic-only and accept the weakness | The engine's claim is auditable retrieval. Ranking a page that states the opposite of the query above the page that states it is a correctness failure, and the fixtures show the embedder does this on word order. |

## Consequences

### Positive

- **Every mode gets keyword evidence.** Exact terms, identifiers, and filenames rank on something other than a cosine that treats reordered text as near-identical.
- **Fusion stayed simple.** No new fusion parameter, no weights, no calibration. RRF's `k=60` default carries over unchanged.
- **Channel provenance is inspectable.** `FusedHit.channels` and `per_channel_scores` name which channels backed each result, so "why did this rank here" stays answerable.
- **No new dependency.** FTS5 ships with SQLite, the same file the vec store already opens.

### Negative

- **A bare polarity flip is still the weak case, and BM25 helps least there.** "X is safe" and "X is not safe" share nearly every token, so their BM25 scores are close for the same reason their cosines are. The channel helps most where word choice or word order differs and least where the only difference is a negation word. The negation problem is not solved by this ADR. What it buys on negation queries is a second, inspectable ranking that at least scores the presence of the negation token, rather than a single ranking that provably ignores it.
- **Cost on every query.** One extra SQLite query per dispatch, plus the FTS index's storage and its maintenance on every upsert and delete. Small at current vault scale, and it's paid whether or not the query has any lexical character.
- **Two indexes to keep in sync.** `chunks_fts` shares rowids with `chunks`, so any write path that touches one must touch the other. That coupling is a standing correctness hazard for future write-path changes.
- **ADR 0004's table is now wrong on its own page.** Readers who find 0004 first get stale routing detail until they follow the pointer here. That's the cost of append-only ADRs, accepted deliberately.

## Status flags

Revisit if:

- A scored eval becomes available and shows the lexical channel doesn't move fused ranking quality, or hurts it on some query class.
- The default embedder is replaced by one that handles word order and negation, which would make the unconditional lexical leg a cost without a matching benefit.
- A cross-encoder reranking stage lands over the fused list, changing what the lexical channel needs to contribute.
- Lexical cost shows up in dispatch latency at larger vault sizes, which would argue for running the channel conditionally after all.
- A fifth channel arrives, at which point the "fuse everything non-empty" policy deserves rechecking against per-channel weighting.
