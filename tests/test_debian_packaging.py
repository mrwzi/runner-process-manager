from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class DebianPackagingTests(unittest.TestCase):
    def test_package_is_a_real_debian_package_recipe_without_gui_runtime(self):
        package = (ROOT / "build" / "scripts" / "linux" / "package.sh").read_text(encoding="utf-8")
        self.assertIn("dpkg-deb --root-owner-group --build", package)
        self.assertIn("python3-cryptography", package)
        self.assertIn("python3-psutil", package)
        self.assertNotIn("python3-pyside", package.lower())
        self.assertIn("runner-agent", package)

    def test_service_is_headless_hardened_and_enabled_by_postinst(self):
        service = (ROOT / "build" / "scripts" / "linux" / "runner-agent.service").read_text(encoding="utf-8")
        postinst = (ROOT / "build" / "scripts" / "linux" / "debian" / "postinst").read_text(encoding="utf-8")
        postrm = (ROOT / "build" / "scripts" / "linux" / "debian" / "postrm").read_text(encoding="utf-8")
        self.assertIn("ExecStart=/usr/bin/runner-agent serve", service)
        self.assertIn("Restart=always", service)
        self.assertIn("NoNewPrivileges=true", service)
        self.assertIn("ProtectSystem=strict", service)
        self.assertIn("systemctl enable runner-agent.service", postinst)
        self.assertIn("runner-agent status", postinst)
        self.assertIn("retain /var/lib/runner", postrm)

    def test_cli_has_required_headless_commands(self):
        cli = (ROOT / "src" / "launcher" / "agent_cli.py").read_text(encoding="utf-8")
        for command in ("serve", "setup", "pair", "status", "test-connection", "repair", "version"):
            self.assertIn(f'add_parser("{command}"', cli)


if __name__ == "__main__":
    unittest.main()
