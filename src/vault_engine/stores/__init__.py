"""Persistent stores backing vault-engine retrieval.

- :mod:`vec_store` — sqlite-vec adapter holding per-chunk embeddings,
  with checksum-based encode-skip.
- :mod:`graph_store` — NetworkX DiGraph with EXTRACTED (wikilink) and
  INFERRED (cosine-similarity) edges, plus alias resolution and BFS
  walk.
"""
