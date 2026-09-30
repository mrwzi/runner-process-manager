"""Small bounded worker pool for UI-triggered I/O.

Work is coalesced by key (periodic refreshes never pile up), and completion
callbacks are delivered by a Qt signal on the object's owning thread.
"""
from __future__ import annotations

import logging
import functools
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal


def timed_ui_operation(function: Callable[..., Any]) -> Callable[..., Any]:
    """Log Qt-slot work that occupies the UI thread for at least 100 ms."""
    @functools.wraps(function)
    def measured(*args: Any, **kwargs: Any) -> Any:
        started = time.monotonic()
        try:
            return function(*args, **kwargs)
        finally:
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms >= 100:
                owner = args[0] if args else None
                workers = getattr(owner, "workers", None)
                if workers is not None:
                    workers.record_slow_operation(function.__qualname__, elapsed_ms, source="ui")
                else:
                    logging.getLogger("runner.ui.operations").warning(
                        "ui operation slow name=%s duration_ms=%.1f", function.__qualname__, elapsed_ms
                    )
    return measured


class BackgroundTaskPool(QObject):
    completed = Signal(str, object, object)

    def __init__(self, log_dir: str | Path, parent: QObject | None = None, *, workers: int = 2, capacity: int = 12) -> None:
        super().__init__(parent)
        self._executor = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="runner-ui-work")
        self._slots = threading.BoundedSemaphore(max(1, capacity))
        self._lock = threading.Lock()
        self._active: set[str] = set()
        self._callbacks: dict[str, Callable[[Any, BaseException | None], None]] = {}
        self._closed = False
        # Do not mkdir/open a log from the GUI constructor. Performance
        # instrumentation is buffered and written by this same bounded pool.
        self._log_dir = Path(log_dir)
        self._pending_log_records: deque[str] = deque(maxlen=128)
        self._log_writer_scheduled = False
        self.completed.connect(self._deliver)

    def submit(self, key: str, operation: Callable[[], Any], callback: Callable[[Any, BaseException | None], None]) -> bool:
        key = str(key)
        with self._lock:
            if self._closed or key in self._active or not self._slots.acquire(blocking=False):
                return False
            self._active.add(key)
            self._callbacks[key] = callback

        def run() -> None:
            started = time.monotonic()
            try:
                result = operation()
                error: BaseException | None = None
            except BaseException as exc:
                result, error = None, exc
            elapsed_ms = (time.monotonic() - started) * 1000
            if elapsed_ms >= 100:
                self.record_slow_operation(key, elapsed_ms, source="background")
            with self._lock:
                closed = self._closed
            if not closed:
                self.completed.emit(key, result, error)

        try:
            self._executor.submit(run)
        except RuntimeError:
            with self._lock:
                self._active.discard(key)
                self._callbacks.pop(key, None)
            self._slots.release()
            return False
        return True

    def record_slow_operation(self, name: str, duration_ms: float, *, source: str) -> None:
        record = f"{time.strftime('%Y-%m-%d %H:%M:%S')} WARNING {source} operation slow name={name} duration_ms={duration_ms:.1f}\n"
        with self._lock:
            if self._closed:
                return
            if len(self._pending_log_records) == self._pending_log_records.maxlen:
                self._pending_log_records.popleft()
            self._pending_log_records.append(record)
            if self._log_writer_scheduled:
                return
            self._log_writer_scheduled = True
        try:
            # This internal writer is the only additional queued task and uses
            # the same fixed-size executor; it cannot grow an unbounded thread
            # or work queue.
            self._executor.submit(self._write_performance_records)
        except RuntimeError:
            with self._lock:
                self._log_writer_scheduled = False

    def _write_performance_records(self) -> None:
        from logging.handlers import RotatingFileHandler

        while True:
            with self._lock:
                if not self._pending_log_records:
                    self._log_writer_scheduled = False
                    return
                records = list(self._pending_log_records)
                self._pending_log_records.clear()
            try:
                self._log_dir.mkdir(parents=True, exist_ok=True)
                handler = RotatingFileHandler(
                    self._log_dir / "runner-ui-performance.log",
                    maxBytes=512 * 1024,
                    backupCount=2,
                    encoding="utf-8",
                )
                try:
                    for message in records:
                        handler.emit(logging.LogRecord(
                            name="runner.ui.operations", level=logging.WARNING,
                            pathname="", lineno=0, msg=message.rstrip("\n"), args=(), exc_info=None,
                        ))
                finally:
                    handler.close()
            except OSError:
                # Monitoring must not affect UI responsiveness or task results.
                continue

    def _deliver(self, key: str, result: Any, error: BaseException | None) -> None:
        with self._lock:
            callback = self._callbacks.pop(key, None)
            self._active.discard(key)
        self._slots.release()
        if callback is not None:
            callback(result, error)

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
        # Existing work has finite operation timeouts and callbacks are
        # suppressed after close. Let queued durability work (notably the last
        # UI-state save) drain instead of canceling it.
        self._executor.shutdown(wait=False, cancel_futures=False)
