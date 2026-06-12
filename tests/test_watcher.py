import time
from pathlib import Path

from watchdog.events import FileModifiedEvent

from vault_engine.config import EngineConfig
from vault_engine.embedder import MockEmbedder
from vault_engine.indexer import Indexer
from vault_engine.watcher import VaultWatcher, _Handler


def test_handler_debounce_fires_once_for_a_burst(sample_vault: Path, tmp_path: Path):
    """Trailing-edge debounce: a burst of rapid writes collapses to ONE callback.

    Regression for E1 — the previous leading-edge throttle fired on the first
    write and DROPPED every trailing write, leaving the index stale. The
    trailing-edge debounce must instead fire exactly once, after the last
    event in the burst.
    """
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    fired: list[Path] = []
    handler = _Handler(cfg=cfg, on_change=fired.append, debounce_seconds=0.15)
    path = sample_vault / "wiki" / "topics" / "alpha.md"

    # Simulate a rapid burst of writes to the same path.
    for _ in range(10):
        handler.on_modified(FileModifiedEvent(str(path)))
        time.sleep(0.005)

    # Nothing should have fired yet (well within the debounce window).
    assert fired == []

    # After the debounce window elapses, exactly one callback should land.
    deadline = time.time() + 2.0
    while time.time() < deadline and not fired:
        time.sleep(0.02)
    assert fired == [path.resolve()]


def test_handler_debounce_delivers_latest_write_content(sample_vault: Path, tmp_path: Path):
    """The single fired callback must reflect the LAST write, not the first.

    We index through the real Indexer so the assertion checks end-state: after
    a burst of edits, the index reflects the final body, not the first one.
    """
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    idx = Indexer(cfg=cfg, embedder=MockEmbedder(dim=cfg.embedding_dim))
    idx.open()
    try:
        idx.rebuild()
        reindexed: list[Path] = []

        def on_change(p: Path) -> None:
            idx.reindex_page(p)
            reindexed.append(p)

        handler = _Handler(cfg=cfg, on_change=on_change, debounce_seconds=0.15)
        path = sample_vault / "wiki" / "topics" / "alpha.md"

        # Burst: several intermediate writes, then a final distinctive one.
        for n in range(5):
            path.write_text(
                f"---\ntitle: Alpha\naliases: []\ntags: [topic]\n"
                f"sources: []\nlast_updated: 2026-02-0{n}\n---\n\n# Alpha\n\nVersion {n}.\n",
                encoding="utf-8",
            )
            handler.on_modified(FileModifiedEvent(str(path)))
            time.sleep(0.01)
        path.write_text(
            "---\ntitle: Alpha\naliases: []\ntags: [topic]\n"
            "sources: []\nlast_updated: 2026-02-09\n---\n\n# Alpha\n\nFINAL body marker.\n",
            encoding="utf-8",
        )
        handler.on_modified(FileModifiedEvent(str(path)))

        deadline = time.time() + 2.0
        while time.time() < deadline and not reindexed:
            time.sleep(0.02)

        # Exactly one reindex for the whole burst, and the stored chunks must
        # match the FINAL body (trailing-edge), proving no trailing-write drop.
        assert reindexed == [path.resolve()]
        from vault_engine.chunker import chunk_page

        final_body = path.read_text(encoding="utf-8").split("---\n", 2)[-1]
        expected = {c.idx: c.checksum for c in chunk_page("alpha", final_body)}
        assert idx.vec.get_checksums("alpha") == expected
    finally:
        idx.close()


def test_watcher_stop_cancels_pending_timers(sample_vault: Path, tmp_path: Path):
    """stop() must cancel outstanding debounce timers so no late callback fires
    against a torn-down service."""
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    fired: list[Path] = []
    watcher = VaultWatcher(cfg=cfg, on_change=fired.append, debounce_seconds=5.0)
    watcher.start()
    try:
        (sample_vault / "wiki" / "topics" / "beta.md").write_text(
            "---\ntitle: Beta\naliases: []\ntags: [topic]\nsources: []\n"
            "last_updated: 2026-03-01\n---\n\n# Beta\n\nEdited.\n",
            encoding="utf-8",
        )
        # Give watchdog a moment to arm a debounce timer (5s window — won't fire).
        time.sleep(0.3)
    finally:
        watcher.stop()
    # The long debounce window has NOT elapsed; stop() must have cancelled it.
    time.sleep(0.2)
    assert fired == []


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
    events: list[Path] = []
    handler = _Handler(cfg=cfg, on_change=events.append, debounce_seconds=0.0)

    handler.on_modified(FileModifiedEvent(str(sample_vault / "raw" / "binary.bin")))

    assert not events


def test_watcher_decodes_bytes_event_paths(sample_vault: Path, tmp_path: Path):
    cfg = EngineConfig(vault_path=sample_vault, cache_dir=tmp_path / "cache")
    events: list[Path] = []
    handler = _Handler(cfg=cfg, on_change=events.append, debounce_seconds=0.0)
    path = sample_vault / "wiki" / "topics" / "alpha.md"

    handler.on_modified(FileModifiedEvent(bytes(path)))

    assert events == [path.resolve()]
