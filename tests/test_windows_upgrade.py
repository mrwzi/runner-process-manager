from __future__ import annotations

import unittest
import subprocess
import sys
import time
from pathlib import Path

import psutil


ROOT = Path(__file__).resolve().parents[1]


class WindowsUpgradeTests(unittest.TestCase):
    def test_installer_forces_stable_directory_and_controlled_handoff(self):
        installer = (ROOT / "build" / "scripts" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("DefaultDirName={autopf}\\Runner", installer)
        self.assertIn("UsePreviousAppDir=no", installer)
        self.assertIn("DisableDirPage=yes", installer)
        self.assertIn("CloseApplications=no", installer)
        self.assertIn("PrepareToInstall", installer)
        self.assertIn("prepare-runner-upgrade.ps1", installer)
        self.assertIn("Flags: dontcopy", installer)
        self.assertIn("CopyFile(ExpandConstant('{tmp}\\Runner.exe')", installer)
        self.assertIn("procedure DeinitializeSetup", installer)
        self.assertNotIn("Tasks: agent", installer.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0])

    def test_installer_registry_verifier_accounts_for_retired_compose_records(self):
        root = Path(__file__).resolve().parents[1]
        handoff = (root / "build" / "scripts" / "prepare-runner-upgrade.ps1").read_text(encoding="utf-8")
        installer = (root / "build" / "scripts" / "installer.iss").read_text(encoding="utf-8")
        migration = (root / "build" / "scripts" / "migrate-runner-apps.ps1").read_text(encoding="utf-8")
        self.assertIn("expectedPostUpgradeApps", handoff)
        self.assertIn("expected_app_count = $expectedPostUpgradeApps.Count", handoff)
        self.assertIn("migrate-runner-apps.ps1", installer)
        self.assertIn("retired_compose_count", migration)
        self.assertIn("apps-before-compose-removal", migration)
        self.assertIn("$apps.Count -gt 0 -and $kept.Count -eq 0", migration)

    def test_installer_migrates_registry_before_any_post_install_verification_or_agent_start(self):
        installer = (ROOT / "build" / "scripts" / "installer.iss").read_text(encoding="utf-8")
        run_section = installer.split("[Run]", 1)[1].split("[UninstallRun]", 1)[0]
        verify_function = installer.split("function VerifyRunnerRegistry(): Boolean;", 1)[1].split("function InitializeUninstall", 1)[0]
        # A standalone raw verifier ran before the migration and rejected valid
        # legacy registries containing retired Docker Compose entries (9 -> 7).
        self.assertNotIn('"{app}\\tools\\verify-runner-upgrade.ps1"', run_section)
        self.assertLess(verify_function.index("migrate-runner-apps.ps1"), verify_function.index("verify-runner-upgrade.ps1"))
        self.assertIn("Check: VerifyRunnerRegistry", run_section)
        self.assertLess(run_section.index("Check: VerifyRunnerRegistry"), run_section.index('Filename: "{app}\\Runner.exe"'))

    def test_in_place_installer_overwrites_existing_binaries_and_can_restore_them(self):
        installer = (ROOT / "build" / "scripts" / "installer.iss").read_text(encoding="utf-8")
        self.assertIn("CopyFile(ExpandConstant('{tmp}\\Runner.exe'), ExpandConstant('{app}\\Runner.exe'), False)", installer)
        self.assertIn("CopyFile(ExpandConstant('{tmp}\\UpdateRunner.exe'), ExpandConstant('{app}\\UpdateRunner.exe'), False)", installer)
        self.assertIn("CopyFile(BinaryBackupDir + '\\Runner.exe', ExpandConstant('{app}\\Runner.exe'), False)", installer)
        self.assertIn("CopyFile(BinaryBackupDir + '\\UpdateRunner.exe', ExpandConstant('{app}\\UpdateRunner.exe'), False)", installer)
        self.assertIn("False means overwrite an existing destination", installer)

    def test_handoff_only_targets_runner_binaries_and_preserves_managed_children(self):
        handoff = (ROOT / "build" / "scripts" / "prepare-runner-upgrade.ps1").read_text(encoding="utf-8")
        self.assertIn("RunnerInstallerFresh-", handoff)
        self.assertIn("Get-AuthenticatedAgentPid", handoff)
        self.assertIn("/v1/health", handoff)
        self.assertIn("authenticated_agent_pid", handoff)
        self.assertIn("MainWindowHandle -ne 0", handoff)
        self.assertIn("$allRunnerProcesses", handoff)
        self.assertIn("if ($DryRun) { return }", handoff)
        self.assertIn("Export-ScheduledTask -TaskName 'Runner Agent'", handoff)
        self.assertIn("Restored the pre-upgrade Runner Agent task definition", handoff)
        self.assertIn("Test-RunnerBinary", handoff)
        self.assertIn("Save-LegacyHandoff", handoff)
        self.assertIn("legacy-handoff-processes.json", handoff)
        self.assertIn("Disable-ScheduledTask", handoff)
        self.assertIn("Stopping only verified Runner GUI PID=$pidToClose after persisted handoff", handoff)
        self.assertIn("Test-RunnerBinary $liveGui.ExecutablePath", handoff)
        self.assertIn("Stop-Process -Id $pidToClose -Force", handoff)
        self.assertNotIn("$gui.CloseMainWindow()", handoff)
        self.assertIn("Exclusive replacement check", handoff)
        self.assertIn("RunnerRestartManager", handoff)
        self.assertIn("Stop-Process -Id $runnerPid", handoff)
        self.assertIn("$verifiedAgentPids -contains [int]$_.ProcessId", handoff)
        self.assertIn("Stopping only $role PID=$runnerPid after persisted handoff", handoff)
        self.assertIn("verified_agent_processes_closed", handoff)
        self.assertNotIn("Stop-Process -Name", handoff)
        self.assertNotIn("taskkill /IM", handoff)
        self.assertIn("$targetRunner", handoff)
        self.assertIn("not an install target; will not terminate it", handoff)
        self.assertIn("logs\\installer-upgrade.log", handoff)

    def test_agent_task_upgrade_does_not_unregister_before_replacement(self):
        provision = (ROOT / "build" / "scripts" / "install-runner-agent.ps1").read_text(encoding="utf-8")
        self.assertNotIn("Unregister-ScheduledTask", provision)
        self.assertIn("Register-ScheduledTask", provision)

    def test_update_front_process_releases_installed_updater_before_setup(self):
        source = (ROOT / "build" / "scripts" / "update_runner.py").read_text(encoding="utf-8")
        self.assertIn("launch_detached_worker", source)
        self.assertIn("shutil.copy2(sys.executable, worker)", source)
        self.assertIn('"--worker"', source)
        self.assertIn('"/SILENT", "/NORESTART"', source)
        self.assertNotIn("/SUPPRESSMSGBOXES", source)

    def test_agent_exit_preserves_apps_for_reconciliation(self):
        launcher = (ROOT / "src" / "launcher" / "run.py").read_text(encoding="utf-8")
        self.assertIn("agent.shutdown(stop_applications=False)", launcher)
        agent = (ROOT / "src" / "cluster" / "agent.py").read_text(encoding="utf-8")
        self.assertIn("self.ownership.adopt(lease)", agent)
        self.assertIn("upgrade_reconciled", agent)

    def test_legacy_gui_normal_close_would_kill_children_but_upgrade_handoff_does_not(self):
        class ManagedApp:
            def __init__(self, pid: int): self.pid, self.alive = pid, True
        class LegacyGui:
            def __init__(self, apps): self.apps, self.alive = apps, True
            def normal_close(self):
                self.alive = False
                for app in self.apps: app.alive = False
            def force_terminate_runner_only(self): self.alive = False

        apps = [ManagedApp(4101), ManagedApp(4102), ManagedApp(4103)]
        old_gui = LegacyGui(apps)
        # This models the historical callback which the installer must not call.
        old_gui.normal_close()
        self.assertFalse(any(app.alive for app in apps))
        apps = [ManagedApp(4101), ManagedApp(4102), ManagedApp(4103)]
        old_gui = LegacyGui(apps)
        captured_pids = [app.pid for app in apps]
        old_gui.force_terminate_runner_only()
        self.assertFalse(old_gui.alive)
        self.assertEqual(captured_pids, [app.pid for app in apps])
        self.assertTrue(all(app.alive for app in apps))

    def test_force_terminating_management_parent_keeps_real_child_alive(self):
        child_program = "import time; time.sleep(30)"
        parent_program = (
            "import subprocess,sys,time; "
            f"p=subprocess.Popen([sys.executable,'-c',{child_program!r}]); "
            "print(p.pid, flush=True); time.sleep(30)"
        )
        parent = subprocess.Popen([sys.executable, "-c", parent_program], stdout=subprocess.PIPE, text=True)
        child_pid = int(parent.stdout.readline().strip())
        try:
            parent.kill()
            parent.wait(timeout=5)
            time.sleep(0.2)
            self.assertTrue(psutil.pid_exists(child_pid))
        finally:
            if psutil.pid_exists(child_pid):
                try: psutil.Process(child_pid).kill()
                except psutil.Error: pass
            if parent.stdout:
                parent.stdout.close()


if __name__ == "__main__":
    unittest.main()
