"""Filesystem watcher.

Wraps watchdog. Emits per-file callbacks for markdown changes inside the vault,
filtered to wiki/ and raw/.

Debounce is **trailing-edge**: each relevant path gets a timer that resets on
every event, and the callback fires ``debounce_seconds`` after the *last* event
for that path. A burst of rapid writes therefore collapses into a single
callback carrying the final state — unlike a leading-edge throttle, which fired
on the first write and silently dropped every trailing write, leaving the index
stale until some later unrelated edit. ``debounce_seconds <= 0`` short-circuits
to a synchronous emit on every event (used by unit tests and ad-hoc callers).
"""

from __future__ import annotations

from collections.abc import Callable
from os import PathLike, fsdecode
from pathlib import Path
from threading import Lock, Timer

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
        # Per-path pending timers. A new event for a path cancels the path's
        # outstanding timer and arms a fresh one, so only the trailing event
        # in a burst survives to fire the callback.
        self._timers: dict[Path, Timer] = {}
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

    def _fire(self, path: Path) -> None:
        """Drop the path's timer record and deliver the callback."""
        with self._lock:
            self._timers.pop(path, None)
        self.on_change(path)

    def _maybe_emit(self, src_path: str | bytes | PathLike[str] | PathLike[bytes]) -> None:
        path = self._is_relevant(src_path)
        if path is None:
            return
        # Zero/negative debounce: emit synchronously. Keeps the unit-test
        # contract (deterministic, no background thread) and lets callers
        # opt out of debouncing entirely.
        if self.debounce <= 0:
            self._fire(path)
            return
        with self._lock:
            existing = self._timers.get(path)
            if existing is not None:
                existing.cancel()
            timer = Timer(self.debounce, self._fire, args=(path,))
            timer.daemon = True
            self._timers[path] = timer
            timer.start()

    def cancel_pending(self) -> None:
        """Cancel every outstanding debounce timer. Called on watcher stop so
        no callback fires against a torn-down service."""
        with self._lock:
            timers = list(self._timers.values())
            self._timers.clear()
        for t in timers:
            t.cancel()

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
        self._handler: _Handler | None = None

    def start(self) -> None:
        handler = _Handler(self.cfg, self.on_change, self.debounce)
        observer = Observer()
        observer.schedule(handler, str(self.cfg.vault_path), recursive=True)
        observer.start()
        self._observer = observer
        self._handler = handler

    def stop(self) -> None:
        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None
        # Drop any debounce timers still pending after the observer stops so a
        # late-firing callback can't hit a closed indexer.
        if self._handler is not None:
            self._handler.cancel_pending()
            self._handler = None
