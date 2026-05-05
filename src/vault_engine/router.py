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
        """Fan out to vector (always) and topology (when warranted). Fuse via RRF.

        Returns a dict with keys:
            intent        – QueryMode value string
            vector_hits   – list[RankedHit] from vec store (up to top_k)
            topology_hits – list[RankedHit] from graph walk (up to top_k; [] if skipped)
            fused_hits    – list[FusedHit] after RRF merge (up to top_k)
        """
        intent = self._classify(query)

        vector_hits = self._vector_search(query, top_k=top_k * 2)

        topology_hits: list[RankedHit] = []
        # HYBRID is included alongside MULTI_HOP because it explicitly mixes
        # semantic + graph signals — running topology without an explicit seed
        # is appropriate (top vector hit anchors the walk via _infer_seed).
        if seed_node is not None or intent in (QueryMode.MULTI_HOP, QueryMode.HYBRID):
            anchor = seed_node or self._infer_seed(vector_hits)
            if anchor:
                topology_hits = topology_walk(self.graph_store, seed=anchor, depth=3)

        if topology_hits:
            fused = reciprocal_rank_fusion([vector_hits, topology_hits])[:top_k]
        else:
            fused = [
                FusedHit(
                    doc_id=h.doc_id,
                    rrf_score=h.score,
                    channels=["vector"],
                    per_channel_scores={"vector": h.score},
                )
                for h in vector_hits[:top_k]
            ]

        return {
            "intent": intent,
            "vector_hits": vector_hits[:top_k],
            "topology_hits": topology_hits[:top_k],
            "fused_hits": fused,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify(self, query: str) -> QueryMode:
        """Heuristic classification reusing the module-level classify()."""
        # Pass empty known_titles; Router has no title registry by default.
        return classify(query, known_titles=set())

    def _vector_search(self, query: str, *, top_k: int) -> list:
        """Run KNN search and return list[RankedHit]."""
        # MockEmbedder / SentenceTransformerEmbedder both expose .encode(list[str]).
        emb = self.embedder.encode([query])[0]
        # VecStore.search returns list[VecHit]; each has .page_slug and .distance.
        raw = self.vec_store.search(emb, top_k=top_k)
        return [
            RankedHit(doc_id=hit.page_slug, score=hit.distance, channel="vector") for hit in raw
        ]

    def _infer_seed(self, vector_hits: list) -> str | None:
        """Return the top vector hit's doc_id if it exists in the graph, else None."""
        if not vector_hits:
            return None
        candidate = vector_hits[0].doc_id
        return candidate if candidate in self.graph_store.graph else None
