"""FastAPI HTTP/JSON server.

Bound to a single interface address externally; this module only constructs
the app — binding lives in CLI.

Auth: optional pre-shared HS256 token. If ``secret is None``, the server
**refuses to bind** to anything other than loopback (127.0.0.1, ::1,
localhost). This is config-enforced at app construction time.

Request limits: query body capped at 2 KB, top_k capped at 100. Larger
requests rejected at validation time.

Observability: every authenticated route logs a single structured line
with request id, path, status, latency, and result counts.
"""

import logging
import time
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

from vault_engine.auth import TokenError, verify_token
from vault_engine.service import Service

log = logging.getLogger(__name__)

_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})


class HttpServerConfigError(Exception):
    """Raised when the HTTP server configuration is unsafe to start."""


class QueryRequest(BaseModel):
    """Bounded query payload. Caller-supplied strings are length-capped to
    prevent memory/CPU DoS via crafted oversize inputs."""

    q: str = Field(..., min_length=1, max_length=2000)
    seed_node: str | None = Field(default=None, max_length=200)
    top_k: int = Field(default=10, ge=1, le=100)


def build_app(svc: Service, *, secret: str | None, bind_addr: str | None = None) -> FastAPI:
    """Construct the FastAPI app for this service instance.

    Args:
        svc: Running Service to back the routes.
        secret: HS256 pre-shared key. ``None`` runs unauthenticated; the
            server then refuses to bind to non-loopback interfaces.
        bind_addr: Bind interface this app will be served on. Used for the
            non-loopback safety check when ``secret`` is None. Defaults to
            ``svc.cfg.http_bind_addr`` if available.

    Raises:
        HttpServerConfigError: ``secret is None`` and the bind interface is
            not loopback. Set ``http_token`` in config or bind to localhost.
    """
    effective_bind = (
        bind_addr
        if bind_addr is not None
        else getattr(getattr(svc, "cfg", None), "http_bind_addr", "127.0.0.1")
    )
    if secret is None and effective_bind not in _LOOPBACK_HOSTS:
        raise HttpServerConfigError(
            f"refusing to start HTTP server: secret is None on non-loopback bind "
            f"({effective_bind!r}). Set http_token in config or bind to loopback."
        )

    app = FastAPI(title="vault-retrieval-engine", version="p2")

    @app.middleware("http")
    async def _log_request(request: Request, call_next):
        rid = uuid.uuid4().hex[:8]
        start = time.monotonic()
        response = await call_next(request)
        latency_ms = int((time.monotonic() - start) * 1000)
        log.info(
            "rid=%s method=%s path=%s status=%d latency_ms=%d",
            rid,
            request.method,
            request.url.path,
            response.status_code,
            latency_ms,
        )
        response.headers["X-Request-Id"] = rid
        return response

    async def auth_dep(authorization: str | None = Header(default=None)) -> None:
        if secret is None:
            return
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer")
        token = authorization.removeprefix("Bearer ").strip()
        try:
            verify_token(token, secret=secret)
        except TokenError as e:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e)) from e

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
