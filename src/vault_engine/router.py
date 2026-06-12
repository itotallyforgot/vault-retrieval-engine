"""Heuristic query classifier (v1) + P2 dual-channel Router.

QueryMode / classify: cheap heuristic running on every query.
Router: fans out to vector + topology channels, fuses via RRF.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from vault_engine.reranker import FusedHit, RankedHit, reciprocal_rank_fusion
from vault_engine.retrieval import topology_walk

if TYPE_CHECKING:
    from vault_engine.config import EngineConfig
    from vault_engine.embedder import Embedder
    from vault_engine.stores.graph_store import GraphStore
    from vault_engine.stores.vec_store import VecStore


class QueryMode(StrEnum):
    LOOKUP = "lookup"
    SEMANTIC = "semantic"
    MULTI_HOP = "multi_hop"
    HYBRID = "hybrid"


# Words that indicate a relational query.
_RELATION_WORDS = {
    "map",
    "maps",
    "mapped",
    "mapping",
    "connect",
    "connects",
    "connected",
    "connection",
    "link",
    "links",
    "linked",
    "touch",
    "touches",
    "touching",
    "depend",
    "depends",
    "depending",
    "dependency",
    "relate",
    "related",
    "relation",
    "relationship",
    "between",
    "across",
}

_RELATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_RELATION_WORDS)) + r")\b",
    re.IGNORECASE,
)

_PROVENANCE_WORDS = {"citation", "citations", "provenance", "source", "sources"}
_ENTITY_PAIR_CONNECTORS = {"and", "or", "to"}

# Heuristic length threshold for "hybrid" (semantic + graph) tier.
_HYBRID_TOKEN_THRESHOLD = 8

# Negation markers. Sentence embeddings are bag-of-words dominated: "X is safe"
# and "X is not safe" sit at near-identical cosine (see
# [[2026-06-06-bag-of-words-breaks-modern-embeddings]]). When a query negates,
# pure SEMANTIC nearest-neighbor cannot be trusted to honor the flip, so we
# de-rate it to HYBRID and let the lexical leg disambiguate. Whole-word markers
# plus the "n't" contraction suffix.
_NEGATION_WORDS = {
    "not",
    "no",
    "never",
    "none",
    "without",
    "cannot",
    "nor",
    "neither",
    "lacks",
    "lacking",
    "absent",
}

_NEGATION_RE = re.compile(
    r"\b(" + "|".join(re.escape(w) for w in sorted(_NEGATION_WORDS)) + r")\b|n't\b",
    re.IGNORECASE,
)


def contains_negation(query: str) -> bool:
    """True if the query carries a negation marker (word or ``n't`` contraction).

    Deliberately lexical and inspectable — an auditable router rule beats
    trusting the embedding to have understood "not".
    """
    return bool(_NEGATION_RE.search(query))


def derate_for_negation(mode: QueryMode, query: str) -> QueryMode:
    """De-rate cosine confidence on negation: SEMANTIC -> HYBRID.

    Only pure SEMANTIC is rerouted — it is the mode that leans entirely on the
    embedding's nearest-neighbor cosine, which is exactly the signal a negation
    corrupts. LOOKUP / MULTI_HOP / HYBRID already carry a lexical or structural
    leg, so they are left untouched.
    """
    if mode is QueryMode.SEMANTIC and contains_negation(query):
        return QueryMode.HYBRID
    return mode


def classify(query: str, known_titles: set[str]) -> QueryMode:
    """Classify query into LOOKUP / SEMANTIC / MULTI_HOP / HYBRID.

    known_titles: lowercased set of titles + aliases the engine knows about.
    """
    q = query.strip().lower()
    known = {t.lower() for t in known_titles}
    if q in known:
        return QueryMode.LOOKUP

    tokens = q.split()
    mentioned = [title for title in known if re.search(rf"\b{re.escape(title)}\b", q)]
    provenance_query = any(token in _PROVENANCE_WORDS for token in tokens)
    if mentioned and provenance_query:
        return QueryMode.HYBRID
    if len(mentioned) >= 2:
        remainder = q
        for title in sorted(mentioned, key=len, reverse=True):
            remainder = re.sub(rf"\b{re.escape(title)}\b", " ", remainder)
        if all(token in _ENTITY_PAIR_CONNECTORS for token in remainder.split()):
            return QueryMode.MULTI_HOP
    if len(mentioned) >= 2 and all(token in known for token in tokens):
        return QueryMode.MULTI_HOP
    if len(mentioned) == 1 and len(tokens) <= 4:
        return QueryMode.LOOKUP

    has_relation = bool(_RELATION_RE.search(q))
    token_count = len(tokens)

    if has_relation and token_count >= _HYBRID_TOKEN_THRESHOLD:
        return QueryMode.HYBRID
    if has_relation:
        return QueryMode.MULTI_HOP
    # Negation de-rate: a bare SEMANTIC query that negates is rerouted to HYBRID
    # so the lexical leg can disambiguate the flipped claim.
    return derate_for_negation(QueryMode.SEMANTIC, q)


# ---------------------------------------------------------------------------
# P2: Dual-channel Router
# ---------------------------------------------------------------------------


class Router:
    """P2 dual-channel dispatcher: vector + topology, fused via RRF.

    Accepts explicit stores so it can be constructed independently of the
    full Indexer stack (useful for tests and the MCP layer). Existing callers
    that only use ``classify`` are unaffected — the Router class is additive.
    """

    def __init__(
        self,
        *,
        cfg: EngineConfig,
        embedder: Embedder,
        vec_store: VecStore,
        graph_store: GraphStore,
    ) -> None:
        self.cfg = cfg
        self.embedder = embedder
        self.vec_store = vec_store
        self.graph_store = graph_store

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def dispatch(
        self,
        query: str,
        *,
        seed_node: str | None = None,
        top_k: int = 10,
    ) -> dict:
        """Fan out to vector + lexical (always) and topology (when warranted). Fuse via RRF.

        Returns a dict with keys:
            intent        – QueryMode value string
            vector_hits   – list[RankedHit] from vec store (up to top_k)
            lexical_hits  – list[RankedHit] from FTS5/BM25 (up to top_k)
            topology_hits – list[RankedHit] from graph walk (up to top_k; [] if skipped)
            fused_hits    – list[FusedHit] after RRF merge (up to top_k)
        """
        intent = self._classify(query)

        vector_hits = self._vector_search(query, top_k=top_k * 2)
        # Lexical (BM25) channel: always run. It is the keyword/word-order leg
        # the bag-of-words embedder can't provide (it scores "X is safe" ~=
        # "X is not safe"). This is what makes HYBRID actually disambiguate
        # negation queries — previously HYBRID was vector + topology only, with
        # no lexical signal. Pages absent from FTS (none, post-reindex) simply
        # contribute nothing.
        lexical_hits = self._lexical_search(query, top_k=top_k * 2)

        topology_hits: list[RankedHit] = []
        # HYBRID is included alongside MULTI_HOP because it explicitly mixes
        # semantic + graph signals — running topology without an explicit seed
        # is appropriate (top vector hit anchors the walk via _infer_seed).
        if seed_node is not None or intent in (QueryMode.MULTI_HOP, QueryMode.HYBRID):
            anchor = seed_node or self._infer_seed(vector_hits)
            if anchor:
                topology_hits = topology_walk(self.graph_store, seed=anchor, depth=3)

        # Fuse every non-empty channel. RRF takes N rankings, so adding the
        # lexical channel is a list entry — no fusion-math change.
        channels = [r for r in (vector_hits, lexical_hits, topology_hits) if r]
        if len(channels) > 1:
            fused = reciprocal_rank_fusion(channels)[:top_k]
        elif channels:
            only = channels[0]
            fused = [
                FusedHit(
                    doc_id=h.doc_id,
                    rrf_score=h.score,
                    channels=[h.channel],
                    per_channel_scores={h.channel: h.score},
                )
                for h in only[:top_k]
            ]
        else:
            fused = []

        return {
            "intent": intent,
            "vector_hits": vector_hits[:top_k],
            "lexical_hits": lexical_hits[:top_k],
            "topology_hits": topology_hits[:top_k],
            "fused_hits": fused,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify(self, query: str) -> QueryMode:
        """Heuristic classification using the current graph's slug/title/alias registry."""
        known_titles: set[str] = set()
        for node_id, data in self.graph_store.graph.nodes(data=True):
            known_titles.add(str(node_id))
            title = data.get("title")
            if title:
                known_titles.add(str(title))
            aliases = data.get("aliases") or []
            if isinstance(aliases, list):
                known_titles.update(str(alias) for alias in aliases)
        return classify(query, known_titles=known_titles)

    def _vector_search(self, query: str, *, top_k: int) -> list:
        """Run KNN search and return list[RankedHit]."""
        # MockEmbedder / SentenceTransformerEmbedder both expose .encode(list[str]).
        emb = self.embedder.encode([query])[0]
        # VecStore.search returns list[VecHit]; each has .page_slug and .distance.
        raw = self.vec_store.search(emb, top_k=top_k)
        return [
            RankedHit(doc_id=hit.page_slug, score=hit.distance, channel="vector") for hit in raw
        ]

    def _lexical_search(self, query: str, *, top_k: int) -> list[RankedHit]:
        """Run BM25 keyword search and return list[RankedHit] (channel='lexical').

        Dedupes to the best (lowest BM25 score) chunk per page so a page that
        matches in several chunks doesn't flood the channel — the channel ranks
        pages, mirroring how the vector channel is consumed downstream.
        """
        raw = self.vec_store.search_lexical(query, top_k=top_k * 2)
        best_by_page: dict[str, float] = {}
        for hit in raw:
            prev = best_by_page.get(hit.page_slug)
            if prev is None or hit.distance < prev:
                best_by_page[hit.page_slug] = hit.distance
        ranked = sorted(best_by_page.items(), key=lambda kv: kv[1])[:top_k]
        return [RankedHit(doc_id=slug, score=score, channel="lexical") for slug, score in ranked]

    def _infer_seed(self, vector_hits: list) -> str | None:
        """Return the top vector hit's doc_id if it exists in the graph, else None."""
        if not vector_hits:
            return None
        candidate = vector_hits[0].doc_id
        return candidate if candidate in self.graph_store.graph else None
