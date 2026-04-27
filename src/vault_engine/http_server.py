"""FastAPI HTTP/JSON server. Bound to a single interface address externally;
this module only constructs the app — binding lives in CLI.

Auth: optional pre-shared HS256 token. If `secret is None`, all routes are
open (only safe behind loopback or Tailscale on a trusted tailnet).
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel

from vault_engine.auth import TokenError, verify_token
from vault_engine.service import Service


class QueryRequest(BaseModel):
    q: str
    seed_node: str | None = None
    top_k: int = 10


def build_app(svc: Service, *, secret: str | None) -> FastAPI:
    app = FastAPI(title="vault-retrieval-engine", version="p2")

    async def auth_dep(authorization: str | None = Header(default=None)) -> None:
        if secret is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            verify_token(token, secret=secret)
        except TokenError as e:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "running": svc.is_running()}

    @app.post("/query", dependencies=[Depends(auth_dep)])
    async def query(req: QueryRequest) -> dict:
        result = svc.query(req.q, seed_node=req.seed_node, top_k=req.top_k)
        fused = [
            {
                "doc_id": h.doc_id,
                "rrf_score": h.rrf_score,
                "channels": list(dict.fromkeys(h.channels)),  # dedupe, preserve order
                "per_channel_scores": h.per_channel_scores,
            }
            for h in result["fused_hits"]
        ]
        intent = result["intent"]
        return {
            "intent": intent.value if hasattr(intent, "value") else str(intent),
            "fused_hits": fused,
        }

    @app.get("/graph/stats", dependencies=[Depends(auth_dep)])
    async def graph_stats() -> dict:
        G = svc.graph_store.graph
        return {
            "nodes": G.number_of_nodes(),
            "edges": G.number_of_edges(),
        }

    return app
