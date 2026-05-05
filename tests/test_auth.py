import time

import jwt
import pytest

from vault_engine.auth import TokenError, verify_token

SECRET = "test-secret-do-not-use-in-prod-padding"  # 38 bytes, satisfies HS256 RFC 7518
WRONG_SECRET = "another-test-secret-also-padded-32b"  # 35 bytes


def _token(payload: dict, secret: str = SECRET, alg: str = "HS256") -> str:
    """Build a token with a default 1-hour exp claim unless one is supplied."""
    payload = {**payload}
    payload.setdefault("exp", int(time.time()) + 3600)
    return jwt.encode(payload, secret, algorithm=alg)


def test_verify_token_accepts_valid_hs256():
    token = _token({"sub": "vault-engine"})
    payload = verify_token(token, secret=SECRET)
    assert payload["sub"] == "vault-engine"


def test_verify_token_rejects_wrong_secret():
    token = _token({"sub": "vault-engine"}, secret=WRONG_SECRET)
    with pytest.raises(TokenError, match="signature"):
        verify_token(token, secret=SECRET)


def test_verify_token_rejects_alg_none():
    bad = jwt.encode({"sub": "x"}, "", algorithm="none")
    with pytest.raises(TokenError):
        verify_token(bad, secret=SECRET)


def test_verify_token_rejects_malformed():
    with pytest.raises(TokenError):
        verify_token("not-a-jwt", secret=SECRET)


def test_verify_token_rejects_missing_exp():
    """Tokens without an `exp` claim are rejected per security policy."""
    no_exp = jwt.encode({"sub": "vault-engine"}, SECRET, algorithm="HS256")
    with pytest.raises(TokenError, match="exp"):
        verify_token(no_exp, secret=SECRET)


def test_verify_token_rejects_expired():
    """Tokens past their exp are rejected."""
    expired = jwt.encode(
        {"sub": "vault-engine", "exp": int(time.time()) - 60},
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(TokenError, match="expired"):
        verify_token(expired, secret=SECRET)


def test_verify_token_rejects_empty():
    with pytest.raises(TokenError, match="empty"):
        verify_token("", secret=SECRET)
