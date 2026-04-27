import pytest
from fastapi.testclient import TestClient
import jwt

from vault_engine.http_server import build_app
from vault_engine.service import Service
from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder


SECRET = "test-secret-do-not-use-in-prod-padding"  # 38 bytes for HS256


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


def test_query_with_auth_rejects_missing_token(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.post("/query", json={"q": "anything"})
    assert r.status_code == 401


def test_query_with_auth_accepts_valid_token(app_with_auth):
    client = TestClient(app_with_auth)
    token = jwt.encode({"sub": "vault-engine"}, SECRET, algorithm="HS256")
    r = client.post("/query", json={"q": "anything"}, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200


def test_query_with_auth_rejects_bad_token(app_with_auth):
    client = TestClient(app_with_auth)
    bad = jwt.encode({"sub": "x"}, "another-secret-also-padded-32-bytes", algorithm="HS256")
    r = client.post("/query", json={"q": "anything"}, headers={"Authorization": f"Bearer {bad}"})
    assert r.status_code == 401


def test_query_with_auth_rejects_empty_bearer_token(app_with_auth):
    """Edge case: Bearer with empty/whitespace token."""
    client = TestClient(app_with_auth)
    r = client.post("/query", json={"q": "test"}, headers={"Authorization": "Bearer "})
    assert r.status_code == 401, (
        f"Expected 401 for empty bearer token, got {r.status_code}: {r.json()}"
    )
