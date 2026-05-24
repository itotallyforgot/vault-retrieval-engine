"""Env-var overrides for ``load_config`` (OGR-181).

The launchd plist (mac) and NSSM service (PC) both set ``VAULT_ENGINE_*``
env vars so the same install scripts work across machines without
hard-coding ports, bind addresses, or tokens. ``load_config`` reads these
on construction.

Precedence: env-var > function-arg > dataclass-default. For fields
accepted by ``load_config`` (``cache_dir``), an explicit non-None arg
still wins over env so callers can override per-call. Fields not
exposed in the signature (``http_bind_addr``, ``http_port``,
``http_token``) take their value strictly from env or default.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vault_engine.config import load_config


@pytest.fixture
def vault(tmp_path: Path) -> Path:
    v = tmp_path / "vault"
    v.mkdir()
    return v


def test_env_var_overrides_bind_addr(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("VAULT_ENGINE_BIND_ADDR", "100.64.0.5")
    cfg = load_config(vault_path=vault)
    assert cfg.http_bind_addr == "100.64.0.5"


def test_env_var_overrides_http_token(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("VAULT_ENGINE_HTTP_TOKEN", "shhh-secret")
    cfg = load_config(vault_path=vault)
    assert cfg.http_token == "shhh-secret"


def test_env_var_overrides_http_port(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("VAULT_ENGINE_HTTP_PORT", "9090")
    cfg = load_config(vault_path=vault)
    assert cfg.http_port == 9090


def test_env_var_overrides_cache_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault: Path
) -> None:
    env_cache = tmp_path / "env-cache"
    monkeypatch.setenv("VAULT_ENGINE_CACHE_DIR", str(env_cache))
    cfg = load_config(vault_path=vault)
    assert cfg.cache_dir == env_cache.resolve()
    assert env_cache.exists()


def test_env_var_missing_leaves_default(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    # Ensure no env var leakage from the surrounding environment.
    for k in (
        "VAULT_ENGINE_BIND_ADDR",
        "VAULT_ENGINE_HTTP_TOKEN",
        "VAULT_ENGINE_HTTP_PORT",
        "VAULT_ENGINE_CACHE_DIR",
    ):
        monkeypatch.delenv(k, raising=False)
    cfg = load_config(vault_path=vault)
    assert cfg.http_bind_addr == "127.0.0.1"
    assert cfg.http_token is None
    assert cfg.http_port == 7842


def test_env_var_empty_string_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, vault: Path
) -> None:
    monkeypatch.setenv("VAULT_ENGINE_BIND_ADDR", "")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_TOKEN", "")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_PORT", "")
    cfg = load_config(vault_path=vault)
    # Empty strings should not override the default.
    assert cfg.http_bind_addr == "127.0.0.1"
    assert cfg.http_token is None
    assert cfg.http_port == 7842


def test_env_var_whitespace_is_stripped(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("VAULT_ENGINE_BIND_ADDR", "  100.64.0.5  ")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_TOKEN", "\ttoken-with-tabs\n")
    cfg = load_config(vault_path=vault)
    assert cfg.http_bind_addr == "100.64.0.5"
    assert cfg.http_token == "token-with-tabs"


def test_env_var_whitespace_only_treated_as_unset(
    monkeypatch: pytest.MonkeyPatch, vault: Path
) -> None:
    monkeypatch.setenv("VAULT_ENGINE_BIND_ADDR", "   ")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_TOKEN", "\t\n")
    cfg = load_config(vault_path=vault)
    assert cfg.http_bind_addr == "127.0.0.1"
    assert cfg.http_token is None


def test_env_var_invalid_port_raises(monkeypatch: pytest.MonkeyPatch, vault: Path) -> None:
    monkeypatch.setenv("VAULT_ENGINE_HTTP_PORT", "not-a-number")
    with pytest.raises(ValueError, match="VAULT_ENGINE_HTTP_PORT"):
        load_config(vault_path=vault)


def test_explicit_cache_arg_wins_over_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault: Path
) -> None:
    # When the caller explicitly passes cache_dir, the function-arg wins
    # over the env-var. Env only fills in for None args.
    env_cache = tmp_path / "env-cache"
    arg_cache = tmp_path / "arg-cache"
    monkeypatch.setenv("VAULT_ENGINE_CACHE_DIR", str(env_cache))
    cfg = load_config(vault_path=vault, cache_dir=arg_cache)
    assert cfg.cache_dir == arg_cache.resolve()


def test_all_env_vars_together(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, vault: Path
) -> None:
    env_cache = tmp_path / "env-cache"
    monkeypatch.setenv("VAULT_ENGINE_BIND_ADDR", "100.64.0.5")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_TOKEN", "service-token")
    monkeypatch.setenv("VAULT_ENGINE_HTTP_PORT", "8080")
    monkeypatch.setenv("VAULT_ENGINE_CACHE_DIR", str(env_cache))
    cfg = load_config(vault_path=vault)
    assert cfg.http_bind_addr == "100.64.0.5"
    assert cfg.http_token == "service-token"
    assert cfg.http_port == 8080
    assert cfg.cache_dir == env_cache.resolve()
