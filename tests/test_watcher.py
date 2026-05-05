import time
from pathlib import Path

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.watcher import VaultWatcher


def test_watcher_routes_md_change_to_indexer(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        events: list[Path] = []
        watcher = VaultWatcher(cfg=cfg, on_change=lambda p: events.append(p))
        watcher.start()
        try:
            (sample_vault / "wiki" / "topics" / "alpha.md").write_text(
                "---\ntitle: Alpha\naliases: []\ntags: [topic]\nsources: []\nlast_updated: 2026-01-04\n---\n\n# Alpha\n\nUpdated body.\n",
                encoding="utf-8",
            )
            # Watchdog is async; allow time for the event.
            deadline = time.time() + 5.0
            while time.time() < deadline and not events:
                time.sleep(0.1)
        finally:
            watcher.stop()
        assert any(p.name == "alpha.md" for p in events)
    finally:
        idx.close()


def test_watcher_ignores_non_markdown(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    events: list[Path] = []
    watcher = VaultWatcher(cfg=cfg, on_change=lambda p: events.append(p))
    watcher.start()
    try:
        (sample_vault / "raw" / "binary.bin").write_bytes(b"\x00\x01")
        time.sleep(1.0)
    finally:
        watcher.stop()
    assert not events
