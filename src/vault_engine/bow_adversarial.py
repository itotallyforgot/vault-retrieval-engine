"""Bag-of-words adversarial probe — a regression gate for embedder swaps.

Modern sentence embeddings are dominated by their bag-of-words representation:
word-order, subject-object swaps, and negation barely move the cosine. See
``[[2026-06-06-bag-of-words-breaks-modern-embeddings]]`` (Warmerdam / marimo).
This module embeds crafted sentence pairs that share a token multiset but carry
opposite or scrambled meaning, and reports the pairwise cosine, so an embedder
swap (mxbai <-> nomic <-> MiniLM) is scored on the failure axis that matters,
not just recall@k.

The instrument is intentionally tiny and inspectable: a JSONL fixture of
``(klass, a, b)`` rows + a cosine over ``embedder.encode``. It does not quantify
how often the phenomenon bites a production corpus — it asserts the property the
model is silent about.

Measured baseline (``mixedbread-ai/mxbai-embed-large-v1``, the engine default,
CPU, 2026-06): negation pairs land at cosine 0.68-0.81 (distinguishable from
``X``/``not X`` but still high), while word-swap (0.96-0.99) and shuffle
(0.94-0.99) are *indistinguishable* from true paraphrases (~0.93). The
calibrated assertions live in ``tests/test_bow_adversarial.py``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from vault_engine.embedder import Embedder


@dataclass(frozen=True)
class AdversarialPair:
    """One crafted pair: ``a`` and ``b`` share a bag of words, differ in meaning."""

    id: str
    klass: str
    a: str
    b: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AdversarialPair:
        return cls(
            id=str(raw["id"]),
            klass=str(raw["klass"]),
            a=str(raw["a"]),
            b=str(raw["b"]),
        )


@dataclass(frozen=True)
class PairScore:
    """A scored pair: the cosine similarity the embedder assigns to ``(a, b)``."""

    id: str
    klass: str
    cosine: float


def load_pairs(fixture_path: Path) -> list[AdversarialPair]:
    """Parse the adversarial JSONL fixture (one pair per non-blank line)."""
    pairs: list[AdversarialPair] = []
    for line in fixture_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        pairs.append(AdversarialPair.from_dict(json.loads(line)))
    return pairs


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    """Cosine similarity between two 1-D vectors. Zero vectors -> 0.0."""
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denom == 0.0:
        return 0.0
    return float(np.dot(a, b) / denom)


def score_pairs(embedder: Embedder, pairs: list[AdversarialPair]) -> list[PairScore]:
    """Embed each pair and return its cosine similarity.

    Encodes ``a`` and ``b`` together so a batching-sensitive embedder sees them
    in one call, matching how the engine encodes query batches.
    """
    scores: list[PairScore] = []
    for pair in pairs:
        vecs = embedder.encode([pair.a, pair.b])
        scores.append(PairScore(id=pair.id, klass=pair.klass, cosine=_cosine(vecs[0], vecs[1])))
    return scores


def max_cosine_by_class(scores: list[PairScore], klass: str) -> float:
    """Worst-case (highest) cosine within a class — the value an assertion gates on.

    Raises ``ValueError`` if no pair of that class is present, so a fixture that
    silently dropped a class fails loudly instead of vacuously passing.
    """
    cosines = [s.cosine for s in scores if s.klass == klass]
    if not cosines:
        raise ValueError(f"no adversarial pairs of class {klass!r} in scored set")
    return max(cosines)
