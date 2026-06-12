"""Engine configuration: paths, model, chunking constants."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _default_cache_dir() -> Path:
    """Default cache directory.

    On Windows: %APPDATA%/vault-retrieval. On Unix: ~/.cache/vault-retrieval.
    """
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "vault-retrieval"
    return Path.home() / ".cache" / "vault-retrieval"


def _env_str(name: str) -> str | None:
    """Read an env var as a stripped string. Empty/whitespace-only → None.

    Centralising this prevents the launchd/NSSM service-config path from
    flipping ``http_token`` to the empty string just because the operator
    left a placeholder unfilled.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    stripped = raw.strip()
    if not stripped:
        return None
    return stripped


def _env_int(name: str) -> int | None:
    """Read an env var as an int. Empty/unset → None. Invalid → ValueError.

    Loud over silent: a typo in the service config should fail at
    ``load_config`` time, not on the first inbound request.
    """
    raw = _env_str(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError as e:
        raise ValueError(
            f"{name} must be an integer (got {raw!r}). Fix the service config or unset it."
        ) from e


def _env_path(name: str) -> Path | None:
    """Read an env var as an expanded ``Path``. Empty/unset → None."""
    raw = _env_str(name)
    if raw is None:
        return None
    return Path(raw).expanduser()


@dataclass
class EngineConfig:
    vault_path: Path
    cache_dir: Path = field(default_factory=_default_cache_dir)
    embedding_model: str = "mixedbread-ai/mxbai-embed-large-v1"
    embedding_dim: int = 1024
    chunk_max_tokens: int = 512
    chunk_min_tokens: int = 32
    semantic_top_k: int = 10
    graph_max_depth: int = 3
    # Cosine-similarity floor for INFERRED graph edges (P3 #6). Pairs of pages
    # whose mean-pooled chunk vectors meet or exceed this threshold get a
    # symmetric INFERRED edge with confidence = similarity. EXTRACTED wikilink
    # edges are never overwritten regardless of threshold.
    #
    # Default is 0.85 based on empirical real-vault smoke (mxbai-embed-large,
    # 339 pages): 0.80 emits 7058 edges (62% of which sit in [0.80, 0.83) and
    # represent weak same-domain co-occurrence), 0.85 emits 1319 (avg 4/node),
    # 0.90 emits 200 (high precision, low recall). 0.85 is the elbow where
    # noise drops sharply but topical neighbors are still surfaced. mxbai
    # vectors are L2-normalised so absolute scores skew high.
    inferred_edge_threshold: float = 0.85

    # --- P2 additions ---
    http_bind_addr: str = "127.0.0.1"  # default: loopback only
    http_port: int = 7842
    http_token: str | None = None  # None disables HTTP auth gate (loopback-only)
    mcp_enabled: bool = False
    service_pidfile: Path | None = None

    # Prototype flag (default OFF). Gates the decision-trace retrieval path: a
    # DECISION_TRACE node/edge type on the graph layer plus a structural hop that
    # walks reasoning edges to answer "why was X concluded?" rather than fetching
    # semantic neighbors. See [[2026-06-06-decision-traces-context-graphs-neo4j]].
    # OFF means construction and retrieval are byte-identical to pre-prototype
    # behavior; the node/edge type are inert metadata until a caller opts in.
    decision_trace_enabled: bool = False

    def __post_init__(self) -> None:
        self.vault_path = Path(self.vault_path).expanduser().resolve()
        if not self.vault_path.exists():
            raise FileNotFoundError(f"vault_path does not exist: {self.vault_path}")
        self.cache_dir = Path(self.cache_dir).expanduser().resolve()

    @property
    def embeddings_db(self) -> Path:
        return self.cache_dir / "embeddings.db"

    @property
    def wiki_dir(self) -> Path:
        return self.vault_path / "wiki"

    @property
    def raw_dir(self) -> Path:
        return self.vault_path / "raw"


def load_config(vault_path: Path, cache_dir: Path | None = None) -> EngineConfig:
    """Build a config and ensure the cache dir exists.

    Reads service-shaped env-vars so the launchd plist (macOS) and NSSM
    service (Windows) can configure the engine without a TOML / CLI flag.

    Recognised env vars:

    - ``VAULT_ENGINE_BIND_ADDR`` -> ``http_bind_addr``
    - ``VAULT_ENGINE_HTTP_PORT`` -> ``http_port``
    - ``VAULT_ENGINE_HTTP_TOKEN`` -> ``http_token``
    - ``VAULT_ENGINE_CACHE_DIR`` -> ``cache_dir`` (only when the
      ``cache_dir`` arg is None)

    Precedence: env-var > function-arg > dataclass-default. The one
    exception is ``cache_dir``: an explicit function-arg wins over env
    so callers (tests, ad-hoc invocations) can override per-call. Fields
    that ``load_config`` does not accept as args (``http_bind_addr``,
    ``http_port``, ``http_token``) take their value strictly from env
    or dataclass default.

    Empty strings and whitespace-only values are treated as unset so a
    placeholder left blank in a service config does not silently set
    ``http_token`` to an empty string.
    """
    # cache_dir: explicit arg wins; otherwise env; otherwise default.
    if cache_dir is not None:
        resolved_cache = Path(cache_dir)
    else:
        env_cache = _env_path("VAULT_ENGINE_CACHE_DIR")
        resolved_cache = env_cache if env_cache is not None else _default_cache_dir()

    # HTTP surface knobs: env > dataclass-default.
    env_bind = _env_str("VAULT_ENGINE_BIND_ADDR")
    env_port = _env_int("VAULT_ENGINE_HTTP_PORT")
    env_token = _env_str("VAULT_ENGINE_HTTP_TOKEN")

    kwargs: dict[str, object] = {
        "vault_path": Path(vault_path),
        "cache_dir": resolved_cache,
    }
    if env_bind is not None:
        kwargs["http_bind_addr"] = env_bind
    if env_port is not None:
        kwargs["http_port"] = env_port
    if env_token is not None:
        kwargs["http_token"] = env_token

    cfg = EngineConfig(**kwargs)  # type: ignore[arg-type]
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg
