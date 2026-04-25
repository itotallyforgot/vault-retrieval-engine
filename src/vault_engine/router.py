"""Heuristic query classifier (v1).

Returns one of four QueryModes. Cheap; runs on every query.
Promote to LLM classifier later if eval flags misroutes.
"""
from __future__ import annotations

import re
from enum import Enum


class QueryMode(str, Enum):
    LOOKUP = "lookup"
    SEMANTIC = "semantic"
    MULTI_HOP = "multi_hop"
    HYBRID = "hybrid"


# Words that indicate a relational query.
_RELATION_WORDS = {
    "map", "maps", "mapped", "mapping",
    "connect", "connects", "connected", "connection",
    "link", "links", "linked",
    "touch", "touches", "touching",
    "depend", "depends", "depending", "dependency",
    "relate", "related", "relation", "relationship",
    "between", "across",
}

_RELATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_RELATION_WORDS)) + r")\b",
    re.IGNORECASE,
)

# Heuristic length threshold for "hybrid" (semantic + graph) tier.
_HYBRID_TOKEN_THRESHOLD = 8


def classify(query: str, known_titles: set[str]) -> QueryMode:
    """Classify query into LOOKUP / SEMANTIC / MULTI_HOP / HYBRID.

    known_titles: lowercased set of titles + aliases the engine knows about.
    """
    q = query.strip().lower()
    if q in {t.lower() for t in known_titles}:
        return QueryMode.LOOKUP

    has_relation = bool(_RELATION_RE.search(q))
    token_count = len(q.split())

    if has_relation and token_count >= _HYBRID_TOKEN_THRESHOLD:
        return QueryMode.HYBRID
    if has_relation:
        return QueryMode.MULTI_HOP
    return QueryMode.SEMANTIC
