from __future__ import annotations

import unittest

from cluster.platform import LinuxPlatformAdapter, WindowsPlatformAdapter


class PlatformTests(unittest.TestCase):
    def test_windows_batch_is_supported(self):
        command = WindowsPlatformAdapter().build_command("C:/apps/start.cmd", "C:/apps", ["--x"])
        self.assertEqual("/D", command[1])

    def test_linux_rejects_windows_only_deployment(self):
        with self.assertRaises(OSError):
            LinuxPlatformAdapter().build_command("/srv/app/start.bat", "/srv/app", [])


if __name__ == "__main__": unittest.main()
