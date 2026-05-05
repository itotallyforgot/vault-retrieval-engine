"""HS256 token verification for HTTP routes.

Tokens are pre-shared, not issued by this service. Generate one once via
``uv run python -c "import secrets; print(secrets.token_urlsafe(32))"`` then
sign with::

    jwt.encode({"sub": "vault-engine", "exp": <unix-ts>}, secret, algorithm="HS256")

The ``exp`` claim is REQUIRED. Tokens without ``exp`` are rejected.

Bound to Tailscale-only HTTP server in P2 — defense in depth on top of
network isolation. If ``http_token`` is None in config, the HTTP server
refuses to bind to non-loopback interfaces (config-enforced; see
http_server.py).
"""

import jwt


class TokenError(Exception):
    pass


def verify_token(token: str, *, secret: str) -> dict:
    """Verify token signature, algorithm, and require exp claim.

    Raises:
        TokenError: signature mismatch, missing required claim, expired,
            malformed, or any other JWT validation failure.
    """
    if not isinstance(token, str) or not token.strip():
        raise TokenError("empty or non-string token")
    try:
        return jwt.decode(
            token,
            secret,
            algorithms=["HS256"],
            options={"require": ["exp"]},
        )
    except jwt.InvalidSignatureError as e:
        raise TokenError(f"signature mismatch: {e}") from e
    except jwt.InvalidAlgorithmError as e:
        raise TokenError(f"unsupported alg: {e}") from e
    except jwt.MissingRequiredClaimError as e:
        raise TokenError(f"missing required claim: {e}") from e
    except jwt.ExpiredSignatureError as e:
        raise TokenError(f"token expired: {e}") from e
    except jwt.PyJWTError as e:
        raise TokenError(f"jwt rejected: {e}") from e
    except (ValueError, TypeError) as e:
        raise TokenError(f"malformed token: {e}") from e
