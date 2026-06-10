"""Bag-of-words adversarial eval — regression gate for embedder swaps.

Two layers:

1. Pure-logic tests (always run on CI): a deterministic stub embedder exercises
   the loader + cosine scorer, so the *instrument* is covered without the model.
2. Integration tests (``@pytest.mark.integration``, real model): run the engine
   default embedder against the committed adversarial set and assert on the
   failure axis the model is silent about.

Honest split per the source article and the measured baseline
(``mixedbread-ai/mxbai-embed-large-v1``, CPU, 2026-06):

- **Negation** pairs land at cosine 0.68-0.81 — high, but below 0.85, so the
  embedder *does* separate ``X`` from ``not X`` enough to gate on. This is a
  real PASS, asserted against a measured threshold.
- **Word-swap** (0.96-0.99) and **shuffle** (0.94-0.99) are indistinguishable
  from true paraphrases (~0.93). The default embedder FAILS these. They are
  ``xfail``-tracked regressions, not hard CI fails — an embedder swap that fixes
  them will surface as XPASS.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest

from vault_engine.bow_adversarial import (
    AdversarialPair,
    PairScore,
    load_pairs,
    max_cosine_by_class,
    score_pairs,
)
from vault_engine.config import EngineConfig
from vault_engine.embedder import EmbedderLoadError, SentenceTransformerEmbedder

FIXTURE = Path(__file__).parent / "fixtures" / "adversarial_bow.jsonl"

# Negation cosine ceiling the engine default is measured to clear (max observed
# 0.8113). A naive bag-of-words embedder that ignores "not" entirely would push
# these toward the paraphrase band (~0.93) and trip the gate.
NEGATION_MAX_COSINE = 0.85


class _AxisEmbedder:
    """Deterministic stub: encodes a sentence as a bag-of-words direction.

    Each token maps to a fixed basis index via a stable hash; the vector is the
    token-count histogram. Word-order and negation are therefore invisible (the
    whole point of the adversarial set), letting the logic tests assert that the
    scorer reports the *high* cosine such an embedder produces — without loading
    a real model.
    """

    def __init__(self, dim: int = 64) -> None:
        self.dim = dim

    def encode(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in text.lower().split():
                out[i, hash(tok) % self.dim] += 1.0
        return out


def test_load_pairs_parses_all_classes():
    pairs = load_pairs(FIXTURE)
    by_class: dict[str, int] = {}
    for pair in pairs:
        assert isinstance(pair, AdversarialPair)
        by_class[pair.klass] = by_class.get(pair.klass, 0) + 1
    # Ticket floors: negation >=4, word_swap >=4, shuffle >=3.
    assert by_class["negation"] >= 4
    assert by_class["word_swap"] >= 4
    assert by_class["shuffle"] >= 3


def test_score_pairs_returns_cosine_per_pair():
    pairs = load_pairs(FIXTURE)
    scores = score_pairs(_AxisEmbedder(), pairs)
    assert len(scores) == len(pairs)
    assert all(isinstance(s, PairScore) for s in scores)
    assert {s.id for s in scores} == {p.id for p in pairs}
    assert all(-1.0001 <= s.cosine <= 1.0001 for s in scores)
    # Word-swap and shuffle pairs share an *identical* token multiset, so an
    # order-blind bag-of-words embedder scores them exactly 1.0 — the failure
    # signature the real eval guards against. (Negation pairs add a token like
    # "not", so they are not multiset-identical and are excluded here.)
    for score in scores:
        if score.klass in ("word_swap", "shuffle"):
            assert score.cosine == pytest.approx(1.0, abs=1e-6), score.id


def test_max_cosine_by_class_raises_on_missing_class():
    scores = [PairScore(id="x", klass="negation", cosine=0.5)]
    with pytest.raises(ValueError, match="no adversarial pairs of class 'word_swap'"):
        max_cosine_by_class(scores, "word_swap")


def test_cosine_handles_zero_vector():
    # A stub that emits all-zeros for an empty string must not divide by zero.
    pairs = [AdversarialPair(id="empty", klass="negation", a="", b="")]
    scores = score_pairs(_AxisEmbedder(), pairs)
    assert scores[0].cosine == 0.0


# ---------------------------------------------------------------------------
# Integration: the real engine-default embedder vs. the adversarial set.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def default_embedder() -> SentenceTransformerEmbedder:
    """The engine's configured default embedder (real model).

    Loads strictly from the local HuggingFace cache (offline) and skips when the
    model isn't already present, so the plain CI lane — which uses ``--embedder
    mock`` everywhere else — does not trigger a ~640MB download. Runs for real
    wherever the model is cached (dev machines, a GPU integration lane).
    """
    model = EngineConfig.__dataclass_fields__["embedding_model"].default
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        return SentenceTransformerEmbedder(model)
    except EmbedderLoadError as exc:
        pytest.skip(f"default embedder {model!r} not available offline: {exc}")


@pytest.fixture(scope="module")
def real_scores(default_embedder: SentenceTransformerEmbedder) -> list[PairScore]:
    return score_pairs(default_embedder, load_pairs(FIXTURE))


@pytest.mark.integration
def test_negation_pairs_stay_below_threshold(real_scores: list[PairScore]):
    """REAL embedder PASS: negation pairs are separated below NEGATION_MAX_COSINE.

    Measured max ~0.81 < 0.85. This is the assertion that fails CI if an embedder
    swap regresses negation handling toward the paraphrase band.
    """
    worst = max_cosine_by_class(real_scores, "negation")
    assert worst < NEGATION_MAX_COSINE, (
        f"negation pairs regressed: max cosine {worst:.4f} >= {NEGATION_MAX_COSINE}. "
        "The embedder is collapsing 'X' onto 'not X'."
    )


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "Tracked regression: mxbai-embed-large is a bag-of-words embedder for "
        "subject-object swaps. Measured cosine 0.96-0.99 (2026-06), "
        "indistinguishable from true paraphrases (~0.93). Flips to XPASS if a "
        "swapped embedder distinguishes argument order."
    ),
    strict=False,
)
def test_word_swap_pairs_stay_below_threshold(real_scores: list[PairScore]):
    worst = max_cosine_by_class(real_scores, "word_swap")
    assert worst < NEGATION_MAX_COSINE


@pytest.mark.integration
@pytest.mark.xfail(
    reason=(
        "Tracked regression: mxbai-embed-large is a bag-of-words embedder for "
        "word shuffles. Measured cosine 0.94-0.99 (2026-06), grammatical "
        "wreckage does not lower similarity. Flips to XPASS if a swapped "
        "embedder becomes order-sensitive."
    ),
    strict=False,
)
def test_shuffle_pairs_stay_below_threshold(real_scores: list[PairScore]):
    worst = max_cosine_by_class(real_scores, "shuffle")
    assert worst < NEGATION_MAX_COSINE
