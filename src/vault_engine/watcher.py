"""Filesystem watcher.

Wraps watchdog. Emits per-file callbacks for markdown changes inside the vault,
filtered to wiki/ and raw/. Debouncing is left to the consumer (the service
layer in P2); here we just dedupe rapid duplicates within a small window.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from os import PathLike, fsdecode
from pathlib import Path
from threading import Lock

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from vault_engine.config import EngineConfig


class _Handler(FileSystemEventHandler):
    def __init__(
        self,
        cfg: EngineConfig,
        on_change: Callable[[Path], None],
        debounce_seconds: float,
    ) -> None:
        self.cfg = cfg
        self.on_change = on_change
        self.debounce = debounce_seconds
        self._last_seen: dict[Path, float] = {}
        self._lock = Lock()

    def _is_relevant(self, src_path: str | bytes | PathLike[str] | PathLike[bytes]) -> Path | None:
        path = Path(fsdecode(src_path)).resolve()
        if path.suffix.lower() != ".md":
            return None
        try:
            relative = path.relative_to(self.cfg.vault_path)
        except ValueError:
            return None
        first = relative.parts[0] if relative.parts else ""
        if first not in {"wiki", "raw"}:
            return None
        return path

    def _maybe_emit(self, src_path: str | bytes | PathLike[str] | PathLike[bytes]) -> None:
        path = self._is_relevant(src_path)
        if path is None:
            return
        now = time.monotonic()
        with self._lock:
            last = self._last_seen.get(path, 0.0)
            if now - last < self.debounce:
                return
            self._last_seen[path] = now
        self.on_change(path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_emit(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_emit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            # Emit BOTH paths: src so the indexer can drop the old slug's
            # chunks (file no longer exists at src), dest so it indexes the
            # new location. Without the src emit, rename leaks stale chunks.
            self._maybe_emit(event.src_path)
            self._maybe_emit(event.dest_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._maybe_emit(event.src_path)


class VaultWatcher:
    def __init__(
        self,
        cfg: EngineConfig,
        on_change: Callable[[Path], None],
        debounce_seconds: float = 2.0,
    ) -> None:
        self.cfg = cfg
        self.on_change = on_change
        self.debounce = debounce_seconds
        self._observer: BaseObserver | None = None

    def start(self) -> None:
        handler = _Handler(self.cfg, self.on_change, self.debounce)
        observer = Observer()
        observer.schedule(handler, str(self.cfg.vault_path), recursive=True)
        observer.start()
        self._observer = observer

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
