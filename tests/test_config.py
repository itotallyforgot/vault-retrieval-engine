from pathlib import Path

import pytest

from vault_engine.config import EngineConfig, load_config


def test_engine_config_defaults_resolve_under_vault(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = EngineConfig(vault_path=vault)
    assert cfg.vault_path == vault
    assert cfg.embeddings_db.parent.exists() or cfg.embeddings_db.parent == cfg.cache_dir
    assert cfg.embedding_model == "mixedbread-ai/mxbai-embed-large-v1"
    assert cfg.embedding_dim == 1024
    assert cfg.chunk_max_tokens > 0


def test_engine_config_rejects_nonexistent_vault(tmp_path: Path):
    missing = tmp_path / "no-such-dir"
    with pytest.raises(FileNotFoundError):
        EngineConfig(vault_path=missing)


def test_load_config_creates_cache_dir(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cache = tmp_path / "cache"
    cfg = load_config(vault_path=vault, cache_dir=cache)
    assert cache.exists()
    assert cfg.embeddings_db.parent == cache


def test_config_p2_defaults(tmp_path: Path):
    # Given a config with only P1 fields
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = EngineConfig(vault_path=vault)
    # P2 fields default to disabled / loopback
    assert cfg.http_bind_addr == "127.0.0.1"
    assert cfg.http_port == 7842
    assert cfg.http_token is None
    assert cfg.mcp_enabled is False
    assert cfg.service_pidfile is None


def test_config_p2_explicit_tailnet(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = EngineConfig(
        vault_path=vault,
        http_bind_addr="100.64.0.5",  # tailnet IP
        http_port=7842,
        http_token="dev-token",
        mcp_enabled=True,
    )
    assert cfg.http_bind_addr == "100.64.0.5"
    assert cfg.http_token == "dev-token"
    assert cfg.mcp_enabled is True
