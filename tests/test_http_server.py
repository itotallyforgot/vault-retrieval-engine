import time

import jwt
import pytest
from fastapi.testclient import TestClient

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.http_server import build_app
from vault_engine.service import Service

SECRET = "test-secret-do-not-use-in-prod-padding"  # 38 bytes for HS256


def _bearer_token(payload: dict | None = None, secret: str = SECRET) -> str:
    payload = {"sub": "vault-engine", **(payload or {})}
    payload.setdefault("exp", int(time.time()) + 3600)
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def app_no_auth(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
        http_token=None,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    app = build_app(svc, secret=None)
    yield app
    svc.stop()


@pytest.fixture
def app_with_auth(sample_vault, tmp_path):
    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
        http_token=SECRET,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    app = build_app(svc, secret=SECRET)
    yield app
    svc.stop()


def test_health_endpoint_no_auth(app_no_auth):
    client = TestClient(app_no_auth)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_query_endpoint_returns_fused_hits(app_no_auth):
    client = TestClient(app_no_auth)
    r = client.post("/query", json={"q": "anything"})
    assert r.status_code == 200
    data = r.json()
    assert "fused_hits" in data


def test_query_endpoint_preserves_lookup_intent_for_exact_title(app_no_auth):
    client = TestClient(app_no_auth)
    r = client.post("/query", json={"q": "Alpha"})
    assert r.status_code == 200
    assert r.json()["intent"] == "lookup"


def test_query_with_auth_rejects_missing_token(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.post("/query", json={"q": "anything"})
    assert r.status_code == 401


def test_query_with_auth_accepts_valid_token(app_with_auth):
    client = TestClient(app_with_auth)
    token = _bearer_token()
    r = client.post("/query", json={"q": "anything"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_query_with_auth_rejects_bad_token(app_with_auth):
    client = TestClient(app_with_auth)
    bad = _bearer_token(secret="another-secret-also-padded-32-bytes")
    r = client.post("/query", json={"q": "anything"}, headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_query_with_auth_rejects_expired_token(app_with_auth):
    client = TestClient(app_with_auth)
    expired = _bearer_token({"exp": int(time.time()) - 60})
    r = client.post(
        "/query", json={"q": "anything"}, headers={"Authorization": f"Bearer {expired}"}
    )
    assert r.status_code == 401


def test_query_rejects_oversize_q(app_no_auth):
    """Pydantic max_length on q caps at 2000 chars."""
    client = TestClient(app_no_auth)
    r = client.post("/query", json={"q": "a" * 3000})
    assert r.status_code == 422


def test_query_rejects_oversize_top_k(app_no_auth):
    """Pydantic le constraint on top_k caps at 100."""
    client = TestClient(app_no_auth)
    r = client.post("/query", json={"q": "anything", "top_k": 1000000})
    assert r.status_code == 422


def test_build_app_refuses_non_loopback_without_secret(sample_vault, tmp_path):
    """Refuse to construct an HTTP app with no secret on a non-loopback bind."""
    from vault_engine.http_server import HttpServerConfigError

    cfg = EngineConfig(
        vault_path=sample_vault,
        cache_dir=tmp_path / "cache",
        embedding_model="mock",
        embedding_dim=8,
        http_token=None,
    )
    svc = Service(cfg, embedder=MockEmbedder(dim=8))
    svc.start()
    try:
        with pytest.raises(HttpServerConfigError, match="non-loopback"):
            build_app(svc, secret=None, bind_addr="0.0.0.0")
    finally:
        svc.stop()


def test_query_with_auth_rejects_empty_bearer_token(app_with_auth):
    """Edge case: Bearer with empty/whitespace token."""
    client = TestClient(app_with_auth)
    r = client.post("/query", json={"q": "test"}, headers={"Authorization": "Bearer "})
    assert (
        r.status_code == 401
    ), f"Expected 401 for empty bearer token, got {r.status_code}: {r.json()}"
