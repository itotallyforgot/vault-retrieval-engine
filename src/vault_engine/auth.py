"""HS256 token verification for HTTP routes.

Tokens are pre-shared, not issued by this service. Generate one once via
`uv run python -c "import secrets; print(secrets.token_urlsafe(32))"` then
sign with `jwt.encode({'sub': 'vault-engine'}, secret, algorithm='HS256')`.

Bound to Tailscale-only HTTP server in P2 — defense in depth on top of network
isolation. If `http_token` is None in config, the HTTP server runs unauthenticated
on the loopback interface only (config-enforced; see http_server.py).
"""
from __future__ import annotations

import jwt


class TokenError(Exception):
    pass


def verify_token(token: str, *, secret: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.InvalidSignatureError as e:
        raise TokenError(f"signature mismatch: {e}") from e
    except jwt.InvalidAlgorithmError as e:
        raise TokenError(f"unsupported alg: {e}") from e
    except jwt.PyJWTError as e:
        raise TokenError(f"jwt rejected: {e}") from e
