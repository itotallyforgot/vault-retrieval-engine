from vault_engine.router import (
    QueryMode,
    classify,
    contains_negation,
    derate_for_negation,
)


def test_classify_lookup_exact_alias(monkeypatch):
    titles = {"alpha", "beta", "claude code"}
    assert classify("alpha", titles) == QueryMode.LOOKUP
    assert classify("Claude Code", titles) == QueryMode.LOOKUP


def test_classify_lookup_when_short_query_mentions_one_alias():
    assert classify("Z-pattern broadcast", {"z-pattern"}) == QueryMode.LOOKUP


def test_classify_multi_hop_when_query_is_multiple_known_entities():
    assert classify("gamma alpha", {"alpha", "gamma"}) == QueryMode.MULTI_HOP


def test_classify_multi_hop_when_query_mentions_multi_word_entity_pair():
    assert classify("claude code alpha", {"alpha", "claude code"}) == QueryMode.MULTI_HOP


def test_classify_semantic_when_known_entities_appear_in_prose():
    assert classify("gamma extends alpha with ranging", {"alpha", "gamma"}) == QueryMode.SEMANTIC


def test_classify_hybrid_when_known_entity_asks_for_source_provenance():
    assert classify("alpha source provenance", {"alpha"}) == QueryMode.HYBRID


def test_classify_multi_hop_relation_words():
    assert classify("controls that map to MITRE T1078", set()) == QueryMode.MULTI_HOP
    assert classify("how does X connect to Y", set()) == QueryMode.MULTI_HOP
    assert classify("incidents linked to AC-2", set()) == QueryMode.MULTI_HOP


def test_classify_semantic_default_natural_language():
    assert classify("what is constitutional AI", set()) == QueryMode.SEMANTIC


def test_classify_hybrid_when_relation_and_long():
    # Long natural-language with relation words -> hybrid (semantic + graph).
    q = "summarize every claim about constitutional AI and its connection to RLHF"
    assert classify(q, set()) == QueryMode.HYBRID


# --- Negation de-rating (bag-of-words mitigation) ---


def test_contains_negation_detects_words_and_contraction():
    assert contains_negation("is the deploy not safe")
    assert contains_negation("the deploy didn't succeed")
    assert contains_negation("access was never granted")
    assert contains_negation("a build without warnings")
    assert not contains_negation("is the deploy safe")
    # Substring false-positives are avoided by word boundaries.
    assert not contains_negation("notation and nobody-cares are fine tokens")


def test_derate_reroutes_semantic_to_hybrid_on_negation():
    assert derate_for_negation(QueryMode.SEMANTIC, "is X not safe") == QueryMode.HYBRID


def test_derate_leaves_semantic_untouched_without_negation():
    assert derate_for_negation(QueryMode.SEMANTIC, "is X safe") == QueryMode.SEMANTIC


def test_derate_does_not_touch_non_semantic_modes():
    # LOOKUP / MULTI_HOP / HYBRID already carry a lexical or structural leg.
    for mode in (QueryMode.LOOKUP, QueryMode.MULTI_HOP, QueryMode.HYBRID):
        assert derate_for_negation(mode, "X is not Y") == mode


def test_classify_negated_natural_language_routes_to_hybrid():
    # Bare semantic query that negates -> HYBRID so the lexical leg disambiguates.
    assert classify("what is not constitutional AI", set()) == QueryMode.HYBRID
    assert classify("constitutional AI", set()) == QueryMode.SEMANTIC
