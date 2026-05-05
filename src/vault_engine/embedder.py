"""Embedding wrappers.

Production: SentenceTransformerEmbedder backed by sentence-transformers + CUDA.
Test/CI: MockEmbedder produces deterministic hash-based vectors.

Both expose .encode(texts: list[str]) -> np.ndarray of shape (n, dim).
"""

from __future__ import annotations

import hashlib
from typing import Protocol

import numpy as np

_KNOWN_DIMS = {
    "mixedbread-ai/mxbai-embed-large-v1": 1024,
    "nomic-ai/nomic-embed-text-v1.5": 768,
    "sentence-transformers/all-MiniLM-L6-v2": 384,
}


def embed_dim_for_model(model_name: str) -> int:
    if model_name not in _KNOWN_DIMS:
        raise ValueError(f"Unknown embedding model: {model_name}")
    return _KNOWN_DIMS[model_name]


class Embedder(Protocol):
    def encode(self, texts: list[str]) -> np.ndarray: ...


class MockEmbedder:
    """Deterministic hash-based pseudo-embedder for tests.

    Maps each text to a stable vector by hashing into the float space.
    Not semantically meaningful — only useful to exercise pipelines.
    """

    def __init__(self, dim: int = 1024) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            digest = hashlib.sha256(t.encode("utf-8")).digest()
            # Repeat digest to fill dim, treat each byte as a float in [-1, 1].
            buf = (digest * ((self.dim // len(digest)) + 1))[: self.dim]
            arr = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
            arr = (arr / 127.5) - 1.0
            out[i] = arr
        return out


class SentenceTransformerEmbedder:
    """Real embedder using sentence-transformers; CUDA when available."""

    def __init__(self, model_name: str) -> None:
        import torch
        from sentence_transformers import SentenceTransformer  # local import (heavy)

        self.model_name = model_name
        self.dim = embed_dim_for_model(model_name)
        device = "cuda" if torch.cuda.is_available() else "cpu"
        self._model = SentenceTransformer(model_name, device=device)

    def encode(self, texts: list[str]) -> np.ndarray:
        out = self._model.encode(
            texts,
            convert_to_numpy=True,
            normalize_embeddings=True,
            batch_size=32,
            show_progress_bar=False,
        )
        return out.astype(np.float32)
