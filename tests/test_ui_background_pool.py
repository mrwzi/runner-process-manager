from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel

from ui.background import BackgroundTaskPool
from ui.app import AppEditorDialog


class UiBackgroundPoolTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_slow_operation_does_not_block_qt_timer_and_callback_returns_to_ui_thread(self):
        with tempfile.TemporaryDirectory() as temp:
            pool = BackgroundTaskPool(Path(temp), workers=2, capacity=4)
            self.addCleanup(pool.shutdown)
            ui_thread = threading.get_ident()
            timer_fired = threading.Event()
            completed = threading.Event()
            callback_threads: list[int] = []
            QTimer.singleShot(10, timer_fired.set)
            accepted = pool.submit("slow-test", lambda: time.sleep(0.3) or "done", lambda value, error: (callback_threads.append(threading.get_ident()), completed.set()))
            self.assertTrue(accepted)
            deadline = time.monotonic() + 2
            while not (timer_fired.is_set() and completed.is_set()) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(timer_fired.is_set(), "Qt timer was starved by background operation")
            self.assertTrue(completed.is_set())
            self.assertEqual(callback_threads, [ui_thread])
            pool.shutdown()

    def test_duplicate_periodic_work_is_coalesced_and_capacity_is_bounded(self):
        with tempfile.TemporaryDirectory() as temp:
            pool = BackgroundTaskPool(Path(temp), workers=1, capacity=2)
            self.addCleanup(pool.shutdown)
            release = threading.Event()
            done = threading.Event()
            other_done = threading.Event()
            self.assertTrue(pool.submit("refresh", lambda: release.wait(1), lambda *_: done.set()))
            self.assertFalse(pool.submit("refresh", lambda: None, lambda *_: None))
            self.assertTrue(pool.submit("other", lambda: None, lambda *_: other_done.set()))
            self.assertFalse(pool.submit("overflow", lambda: None, lambda *_: None))
            release.set()
            deadline = time.monotonic() + 2
            while not (done.is_set() and other_done.is_set()) and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(done.is_set())
            self.assertTrue(other_done.is_set())
            pool.shutdown()

    def test_performance_log_directory_is_created_only_by_background_writer(self):
        with tempfile.TemporaryDirectory() as temp:
            log_dir = Path(temp) / "deferred-logs"
            pool = BackgroundTaskPool(log_dir, workers=1, capacity=2)
            self.addCleanup(pool.shutdown)
            self.assertFalse(log_dir.exists(), "worker-pool construction performed filesystem work")
            pool.record_slow_operation("synthetic-ui-slot", 125.0, source="ui")
            log_file = log_dir / "runner-ui-performance.log"
            deadline = time.monotonic() + 2
            while not log_file.exists() and time.monotonic() < deadline:
                self.app.processEvents()
                time.sleep(0.005)
            self.assertTrue(log_file.exists())
            self.assertIn("synthetic-ui-slot", log_file.read_text(encoding="utf-8"))

    def test_app_editor_exposes_no_docker_controls(self):
        with tempfile.TemporaryDirectory() as temp:
            workers = BackgroundTaskPool(Path(temp), workers=1, capacity=2)
            dialog = AppEditorDialog(workers=workers)
            self.addCleanup(dialog.close)
            self.addCleanup(workers.shutdown)
            visible_text = " ".join(widget.text() for widget in dialog.findChildren(QLabel))
            self.assertNotIn("Docker", visible_text)
            self.assertFalse(hasattr(dialog, "compose_input"))
            self.assertFalse(hasattr(dialog, "app_type_input"))

if __name__ == "__main__":
    unittest.main()
