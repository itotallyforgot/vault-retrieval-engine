"""Semantic-similarity edge inference (P3 #6).

Adds INFERRED edges to the graph for page pairs whose mean-pooled chunk
vectors meet or exceed a cosine-similarity threshold. EXTRACTED wikilink
edges are never overwritten — the inference layer is strictly additive.

Edges are emitted symmetrically (a → b and b → a) so graph walks surface
the relationship from either direction. Each edge carries
``relation="similarity"``, ``edge_type="INFERRED"``, and
``confidence = similarity`` so downstream consumers (citation chains, the
MCP ``graph_stats`` tool) can distinguish them from wikilink edges and rank
by strength.

Performance: similarity computation uses a single ``M @ M.T`` matmul over
all page vectors instead of an O(N^2) Python loop of per-pair cosines.
At 10k pages this is one BLAS call vs 50M Python iterations.
"""

import numpy as np

from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity in [-1, 1]. Returns 0.0 if either vector is zero."""
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def page_vector_from_chunks(chunks: list[np.ndarray]) -> np.ndarray | None:
    """Mean-pool chunk vectors into a page-level vector and L2-normalise.

    Returns None for an empty chunk list so callers can skip empty pages
    without crashing on a divide-by-zero.
    """
    if not chunks:
        return None
    stacked = np.vstack([c.astype(np.float32) for c in chunks])
    mean = stacked.mean(axis=0)
    norm = float(np.linalg.norm(mean))
    if norm == 0.0:
        return mean
    return mean / norm


def add_similarity_edges(
    graph: GraphStore,
    vec_store: VecStore,
    threshold: float = 0.8,
) -> int:
    """Add symmetric INFERRED edges for page pairs above ``threshold``.

    Skips:

    - pages with zero chunks in the vec store (e.g. empty body)
    - pairs that already have an EXTRACTED edge in that direction (the
      reverse direction is still eligible for an INFERRED edge if the
      reverse EXTRACTED edge does not exist)

    Returns the number of edges added.

    Performance: uses a single matmul over all page vectors. Page vectors
    from ``page_vector_from_chunks`` are already L2-normalized, so the
    matmul produces cosine similarities directly — no per-pair normalize
    needed.
    """
    nodes = list(graph.graph.nodes)
    page_vecs: dict[str, np.ndarray] = {}
    for slug in nodes:
        chunk_rows = vec_store.iter_chunks_for_page(slug)
        if not chunk_rows:
            continue
        v = page_vector_from_chunks([row[1] for row in chunk_rows])
        if v is not None:
            page_vecs[slug] = v

    if not page_vecs:
        return 0

    slugs = list(page_vecs.keys())
    n = len(slugs)
    if n < 2:
        return 0

    # Single matmul: M @ M.T produces cosine similarities for all page
    # pairs in one BLAS call. Replaces O(N^2) Python per-pair dot product.
    matrix = np.vstack([page_vecs[s] for s in slugs]).astype(np.float32)
    sims = matrix @ matrix.T  # (n, n)

    added = 0
    for i in range(n):
        for j in range(i + 1, n):
            sim = float(sims[i, j])
            if sim < threshold:
                continue
            src, dst = slugs[i], slugs[j]
            for a, b in ((src, dst), (dst, src)):
                if graph.graph.has_edge(a, b):
                    # EXTRACTED takes precedence; existing INFERRED edge is
                    # left alone for stability across reruns.
                    continue
                graph.add_edge(
                    a,
                    b,
                    relation="similarity",
                    edge_type="INFERRED",
                    confidence=sim,
                )
                added += 1
    return added
