from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from manager import ProcessManager
from cluster.process_guard import FENCED_EXIT_CODE
import subprocess
import psutil


class ProcessOwnershipGateTests(unittest.TestCase):
    def test_guard_kills_child_after_agent_deadline_stops_advancing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            guard_file, pid_file = root / "lease.deadline", root / "child.pid"
            child = root / "child.py"
            child.write_text("import os,time,pathlib\npathlib.Path(r'%s').write_text(str(os.getpid()))\ntime.sleep(30)\n" % pid_file, encoding="utf-8")
            # Leave enough scheduling headroom for an overloaded Windows
            # builder/antivirus host to create the child and write its PID.
            # The invariant under test is fencing, not sub-second timing.
            guard_file.write_text(str(time.monotonic() + 3.0), encoding="ascii")
            process = subprocess.Popen([sys.executable, "-m", "cluster.process_guard", "--guard-file", str(guard_file), "--", sys.executable, str(child)])
            self.assertEqual(FENCED_EXIT_CODE, process.wait(timeout=8))
            child_pid = int(pid_file.read_text())
            time.sleep(0.1)
            self.assertFalse(psutil.pid_exists(child_pid))

    def test_denied_protected_start_never_spawns_process(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "protected_test_process.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            manager = ProcessManager([{
                "id": "protected", "name": "Protected", "runner_path": str(script), "cwd": str(root),
                "protected": True, "service_group": "payments",
            }], root / "logs")
            manager.set_start_authorizer(lambda app: (False, "no valid lease"))
            manager.start_app("protected")
            deadline = time.time() + 3
            while manager.snapshot("protected")["pending_action"] and time.time() < deadline:
                time.sleep(0.02)
            snapshot = manager.snapshot("protected")
            self.assertIsNone(snapshot["pid"])
            self.assertEqual("Stopped", snapshot["status"])
            self.assertIn("no valid lease", snapshot["last_error"])
            manager.shutdown()


if __name__ == "__main__": unittest.main()
