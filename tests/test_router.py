from vault_engine.router import QueryMode, classify


def test_classify_lookup_exact_alias(monkeypatch):
    titles = {"alpha", "beta", "claude code"}
    assert classify("alpha", titles) == QueryMode.LOOKUP
    assert classify("Claude Code", titles) == QueryMode.LOOKUP


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
