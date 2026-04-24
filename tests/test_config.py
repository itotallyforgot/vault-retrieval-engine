from pathlib import Path

import pytest

from vault_engine.config import EngineConfig, load_config


def test_engine_config_defaults_resolve_under_vault(tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    cfg = EngineConfig(vault_path=vault)
    assert cfg.vault_path == vault
    assert cfg.embeddings_db.parent.exists() or cfg.embeddings_db.parent == cfg.cache_dir
    assert cfg.graph_pickle.suffix == ".pkl"
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
    assert cfg.graph_pickle.parent == cache
