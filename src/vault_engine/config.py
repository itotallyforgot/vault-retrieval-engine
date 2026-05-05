"""Engine configuration: paths, model, chunking constants."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def _default_cache_dir() -> Path:
    """Default cache directory.

    On Windows: %APPDATA%/vault-retrieval. On Unix: ~/.cache/vault-retrieval.
    """
    import os

    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "vault-retrieval"
    return Path.home() / ".cache" / "vault-retrieval"


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

    def __post_init__(self) -> None:
        self.vault_path = Path(self.vault_path).expanduser().resolve()
        if not self.vault_path.exists():
            raise FileNotFoundError(f"vault_path does not exist: {self.vault_path}")
        self.cache_dir = Path(self.cache_dir).expanduser().resolve()

    @property
    def embeddings_db(self) -> Path:
        return self.cache_dir / "embeddings.db"

    @property
    def graph_pickle(self) -> Path:
        return self.cache_dir / "graph.pkl"

    @property
    def wiki_dir(self) -> Path:
        return self.vault_path / "wiki"

    @property
    def raw_dir(self) -> Path:
        return self.vault_path / "raw"


def load_config(vault_path: Path, cache_dir: Path | None = None) -> EngineConfig:
    """Build a config and ensure the cache dir exists."""
    cfg = EngineConfig(
        vault_path=Path(vault_path),
        cache_dir=Path(cache_dir) if cache_dir else _default_cache_dir(),
    )
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    return cfg
