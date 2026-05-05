"""Smoke-test INFERRED edge inference against the real vault's warm cache.

Reads the existing embeddings.db (mxbai-embed-large vectors, already cached),
builds a fresh GraphStore from vault wikilinks, runs the similarity-edge
inference, and prints stats. Does NOT load the embedder model — pure CPU
pairwise cosine over warm vectors.

Usage:
    uv run python scripts/inference_smoke.py [VAULT_PATH] [--threshold 0.8]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.inference import add_similarity_edges
from vault_engine.stores.graph_store import GraphStore
from vault_engine.stores.vec_store import VecStore
from vault_engine.vault_reader import iter_pages


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("vault", type=Path, nargs="?", default=Path.cwd())
    p.add_argument("--threshold", type=float, default=0.8)
    p.add_argument("--show", type=int, default=10, help="Top N inferred edges to print.")
    args = p.parse_args()

    cfg = EngineConfig(vault_path=args.vault)
    print(f"vault: {cfg.vault_path}")
    print(f"cache: {cfg.cache_dir}")
    print(f"threshold: {args.threshold}")

    pages = list(iter_pages(cfg.vault_path))
    print(f"pages: {len(pages)}")

    graph = GraphStore()
    graph.rebuild(pages)
    print(
        f"graph after wikilink pass: nodes={graph.graph.number_of_nodes()} "
        f"edges={graph.graph.number_of_edges()}"
    )

    vec = VecStore(
        db_path=cfg.embeddings_db,
        dim=cfg.embedding_dim,
        model_name=cfg.embedding_model,
    )
    vec.open()
    try:
        t0 = time.perf_counter()
        added = add_similarity_edges(graph, vec, threshold=args.threshold)
        elapsed = time.perf_counter() - t0
        print(f"INFERRED edges added: {added} in {elapsed:.2f}s")
        print(
            f"graph after inference: nodes={graph.graph.number_of_nodes()} "
            f"edges={graph.graph.number_of_edges()}"
        )

        # Top-N inferred edges by confidence.
        inferred = [
            (s, d, float(data.get("confidence", 0.0)))
            for s, d, data in graph.graph.edges(data=True)
            if data.get("edge_type") == "INFERRED"
        ]
        inferred.sort(key=lambda r: r[2], reverse=True)
        print(f"top {args.show} INFERRED edges by confidence:")
        for s, d, c in inferred[: args.show]:
            print(f"  {s}  <->  {d}    sim={c:.3f}")
    finally:
        vec.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
