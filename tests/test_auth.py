import pytest
import jwt
from vault_engine.auth import verify_token, TokenError


SECRET = "test-secret-do-not-use-in-prod-padding"  # 38 bytes, satisfies HS256 RFC 7518
WRONG_SECRET = "another-test-secret-also-padded-32b"  # 35 bytes


def test_verify_token_accepts_valid_hs256():
    token = jwt.encode({"sub": "vault-engine"}, SECRET, algorithm="HS256")
    payload = verify_token(token, secret=SECRET)
    assert payload["sub"] == "vault-engine"


def test_verify_token_rejects_wrong_secret():
    token = jwt.encode({"sub": "vault-engine"}, WRONG_SECRET, algorithm="HS256")
    with pytest.raises(TokenError, match="signature"):
        verify_token(token, secret=SECRET)


def test_verify_token_rejects_alg_none():
    bad = jwt.encode({"sub": "x"}, "", algorithm="none")
    with pytest.raises(TokenError):
        verify_token(bad, secret=SECRET)


def test_verify_token_rejects_malformed():
    with pytest.raises(TokenError):
        verify_token("not-a-jwt", secret=SECRET)
