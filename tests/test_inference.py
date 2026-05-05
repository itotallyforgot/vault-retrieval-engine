"""Tests for INFERRED edge inference (semantic-similarity ≥ threshold)."""

from __future__ import annotations

from pathlib import Path

import numpy as np

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
