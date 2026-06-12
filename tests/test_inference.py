"""Tests for INFERRED edge inference (semantic-similarity ≥ threshold)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from vault_engine.embedder import MockEmbedder
from vault_engine.inference import (
    add_similarity_edges,
    cosine_similarity,
    page_vector_from_chunks,
)
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore


def _seed_store(store: VecStore, slug: str, vectors: list[np.ndarray]) -> None:
    for i, v in enumerate(vectors):
        store.upsert(slug, i, f"chunk-{i}", f"csum-{slug}-{i}", v)


def test_cosine_similarity_basic():
    a = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    b = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    c = np.array([-1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    d = np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32)
    assert cosine_similarity(a, b) == 1.0
    assert cosine_similarity(a, c) == -1.0
    assert cosine_similarity(a, d) == 0.0


def test_page_vector_from_chunks_mean_pools_and_normalises():
    chunks = [
        np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32),
    ]
    v = page_vector_from_chunks(chunks)
    # Mean would be [0.5, 0.5, 0, 0]; normalised → 1/sqrt(2) on each non-zero axis.
    assert v is not None
    np.testing.assert_allclose(v, np.array([1, 1, 0, 0]) / np.sqrt(2), atol=1e-6)


def test_page_vector_handles_empty():
    assert page_vector_from_chunks([]) is None


def test_add_similarity_edges_links_close_pages(tmp_path: Path):
    """Two pages whose page-vectors exceed the threshold should be linked
    by symmetric INFERRED edges with confidence = similarity."""
    store = VecStore(db_path=tmp_path / "v.db", dim=4, model_name="m1")
    store.open()
    try:
        # alpha and beta share a strong axis; gamma is orthogonal.
        _seed_store(store, "alpha", [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        _seed_store(store, "beta", [np.array([0.99, 0.14, 0.0, 0.0], dtype=np.float32)])
        _seed_store(store, "gamma", [np.array([0.0, 0.0, 1.0, 0.0], dtype=np.float32)])

        graph = GraphStore()
        for slug in ("alpha", "beta", "gamma"):
            graph.add_node(slug)

        added = add_similarity_edges(graph, store, threshold=0.8)
        assert added == 2  # alpha→beta + beta→alpha (symmetric)

        for src, dst in [("alpha", "beta"), ("beta", "alpha")]:
            assert graph.has_edge(src, dst)
            data = graph.graph.edges[src, dst]
            assert data["edge_type"] == "INFERRED"
            assert data["relation"] == "similarity"
            assert 0.8 <= float(data["confidence"]) <= 1.0

        # gamma is orthogonal → no edges to/from gamma.
        for nbr in ("alpha", "beta"):
            assert not graph.has_edge("gamma", nbr)
            assert not graph.has_edge(nbr, "gamma")
    finally:
        store.close()


def test_add_similarity_edges_respects_threshold(tmp_path: Path):
    store = VecStore(db_path=tmp_path / "v.db", dim=4, model_name="m1")
    store.open()
    try:
        # Similarity ≈ 0.7071 — below 0.8, above 0.5.
        _seed_store(store, "alpha", [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        _seed_store(store, "beta", [np.array([1.0, 1.0, 0.0, 0.0], dtype=np.float32)])

        graph = GraphStore()
        for slug in ("alpha", "beta"):
            graph.add_node(slug)

        assert add_similarity_edges(graph, store, threshold=0.8) == 0
        assert not graph.has_edge("alpha", "beta")

        # Lower threshold → edges emitted.
        added = add_similarity_edges(graph, store, threshold=0.5)
        assert added == 2
    finally:
        store.close()


def test_add_similarity_edges_does_not_overwrite_extracted(tmp_path: Path):
    """If an EXTRACTED wikilink edge already exists between two pages,
    the INFERRED pass must NOT downgrade it."""
    store = VecStore(db_path=tmp_path / "v.db", dim=4, model_name="m1")
    store.open()
    try:
        _seed_store(store, "alpha", [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        _seed_store(store, "beta", [np.array([0.99, 0.14, 0.0, 0.0], dtype=np.float32)])

        graph = GraphStore()
        graph.add_node("alpha")
        graph.add_node("beta")
        graph.add_edge("alpha", "beta", relation="wikilink")  # EXTRACTED

        added = add_similarity_edges(graph, store, threshold=0.8)
        # Reverse direction is new; forward direction must not be touched.
        assert added == 1
        forward = graph.graph.edges["alpha", "beta"]
        assert forward["edge_type"] == "EXTRACTED"
        assert forward["relation"] == "wikilink"
        reverse = graph.graph.edges["beta", "alpha"]
        assert reverse["edge_type"] == "INFERRED"

    finally:
        store.close()


def test_add_similarity_edges_skips_nodes_without_chunks(tmp_path: Path):
    """A node with zero chunks (e.g. empty page) must not crash inference
    and must not produce edges."""
    store = VecStore(db_path=tmp_path / "v.db", dim=4, model_name="m1")
    store.open()
    try:
        _seed_store(store, "alpha", [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        _seed_store(store, "beta", [np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)])
        # gamma intentionally has no chunks.

        graph = GraphStore()
        for slug in ("alpha", "beta", "gamma"):
            graph.add_node(slug)

        added = add_similarity_edges(graph, store, threshold=0.8)
        assert added == 2  # alpha↔beta only
        assert not any(graph.has_edge("gamma", n) for n in ("alpha", "beta"))
        assert not any(graph.has_edge(n, "gamma") for n in ("alpha", "beta"))
    finally:
        store.close()


# --- AMBIGUOUS confidence band (E3) --------------------------------------


def test_add_similarity_edges_bands_by_confidence(tmp_path: Path):
    """E3: similarity edges split by confidence band.

    >= AMBIGUOUS_CEILING (0.95) → INFERRED; [threshold, 0.95) → AMBIGUOUS.
    """
    from vault_engine.inference import AMBIGUOUS_CEILING

    store = VecStore(db_path=tmp_path / "v.db", dim=2, model_name="m1")
    store.open()
    try:
        # high ~ cos 0.98 with anchor → INFERRED band.
        # mid  ~ cos 0.90 with anchor → AMBIGUOUS band.
        anchor = np.array([1.0, 0.0], dtype=np.float32)
        high = np.array([0.98, 0.198], dtype=np.float32)  # cos ≈ 0.980
        mid = np.array([0.90, 0.4359], dtype=np.float32)  # cos ≈ 0.900
        _seed_store(store, "anchor", [anchor])
        _seed_store(store, "high", [high])
        _seed_store(store, "mid", [mid])

        graph = GraphStore()
        for slug in ("anchor", "high", "mid"):
            graph.add_node(slug)

        add_similarity_edges(graph, store, threshold=0.85)

        # anchor↔high is in the confident band.
        assert graph.has_edge("anchor", "high")
        ah = graph.graph.edges["anchor", "high"]
        assert float(ah["confidence"]) >= AMBIGUOUS_CEILING
        assert ah["edge_type"] == "INFERRED"

        # anchor↔mid is in the ambiguous band.
        assert graph.has_edge("anchor", "mid")
        am = graph.graph.edges["anchor", "mid"]
        assert 0.85 <= float(am["confidence"]) < AMBIGUOUS_CEILING
        assert am["edge_type"] == "AMBIGUOUS"
    finally:
        store.close()


# --- Edge-precision fixture: near-duplicate pages, inverted relations (E3) ---
#
# The bag-of-words embedder scores near-duplicate / word-swapped / negated text
# as highly similar (see KNOWN_ISSUES). When two pages state *inverted* versions
# of a relation, a naive INFERRED edge would assert a confident link that hides
# the polarity flip. The confidence band makes those land in the AMBIGUOUS tier
# instead, so consumers can treat them with suspicion rather than as fact.

# (page_a_text, page_b_text) pairs whose surface forms are near-duplicates but
# whose meaning is inverted.
INVERTED_RELATION_PAIRS = [
    (
        "Service A depends on Service B for authentication and session state.",
        "Service B depends on Service A for authentication and session state.",
    ),
    (
        "The migration is safe to run during peak traffic without downtime.",
        "The migration is not safe to run during peak traffic without downtime.",
    ),
    (
        "Module X must be deployed before Module Y in the release sequence.",
        "Module Y must be deployed before Module X in the release sequence.",
    ),
]


def test_inverted_relation_pairs_are_not_confident_inferred_edges(tmp_path: Path):
    """Edge-precision regression for E3.

    For each near-duplicate-but-inverted pair, IF the embedder scores them above
    the INFERRED threshold, the resulting edge must NOT be a confident INFERRED
    edge whenever the similarity sits in the ambiguous band — it must be
    AMBIGUOUS. This guards against the engine asserting a confident semantic
    link between two pages that actually state opposite relations.

    Uses the MockEmbedder (deterministic, always available); the band logic is
    embedder-agnostic, so this is a pure logic guard. The companion
    integration check against the real bag-of-words model lives behind the
    BoW adversarial fixtures.
    """
    from vault_engine.inference import AMBIGUOUS_CEILING

    threshold = 0.85
    store = VecStore(db_path=tmp_path / "v.db", dim=16, model_name="m")
    store.open()
    embedder = MockEmbedder(dim=16)
    try:
        graph = GraphStore()
        for i, (text_a, text_b) in enumerate(INVERTED_RELATION_PAIRS):
            a, b = f"a{i}", f"b{i}"
            for slug, text in ((a, text_a), (b, text_b)):
                from vault_engine.chunker import chunk_page

                for c in chunk_page(slug, text):
                    emb = embedder.encode([c.text])[0]
                    store.upsert(slug, c.idx, c.text, c.checksum, emb)
                graph.add_node(slug)

        add_similarity_edges(graph, store, threshold=threshold)

        # Every similarity edge that exists must be correctly typed by band:
        # nothing in [threshold, 0.95) may be labelled INFERRED.
        for u, v, data in graph.graph.edges(data=True):
            if data.get("edge_type") in {"INFERRED", "AMBIGUOUS"}:
                conf = float(data["confidence"])
                if conf < AMBIGUOUS_CEILING:
                    assert data["edge_type"] == "AMBIGUOUS", (
                        f"{u}->{v} conf={conf:.3f} is in the ambiguous band but "
                        f"was labelled {data['edge_type']}"
                    )
    finally:
        store.close()
