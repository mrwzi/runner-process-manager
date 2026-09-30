from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import threading

from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtWidgets import QLabel, QPushButton, QRadioButton, QVBoxLayout, QWizard, QWizardPage
from PySide6.QtWidgets import QMessageBox

from cluster.onboarding import OnboardingController
from cluster.tailscale import local_identity
from ui.background import BackgroundTaskPool


class SetupWizard(QWizard):
    """A deliberately plain-language first-run experience."""

    def __init__(self, runtime_root: Path, parent=None) -> None:
        super().__init__(parent)
        self.runtime_root = runtime_root
        self.workers = BackgroundTaskPool(runtime_root / "logs", self, workers=2, capacity=6)
        self._finishing = False
        self.controller = OnboardingController(runtime_root)
        self.setWindowTitle("Welcome to Runner")
        self.setMinimumSize(600, 390)
        self.setWizardStyle(QWizard.ModernStyle)

        choice = QWizardPage()
        choice.setTitle("Welcome to Runner")
        choice.setSubTitle("How will this computer be used? You can change advanced settings later.")
        layout = QVBoxLayout(choice)
        self.standalone = QRadioButton("Standalone computer")
        self.primary = QRadioButton("Primary server")
        self.backup = QRadioButton("Backup server")
        self.standalone.setChecked(True)
        layout.addWidget(self.standalone)
        layout.addWidget(QLabel("Runs your applications on this computer as Runner has always done."))
        layout.addWidget(self.primary)
        layout.addWidget(QLabel("Keeps this computer as the preferred server and lets you add a protected backup."))
        layout.addWidget(self.backup)
        layout.addWidget(QLabel("Keeps this computer ready to take over protected applications if the primary fails."))
        self.tailscale_status = QLabel()
        self.install_tailscale = QPushButton("Install Tailscale")
        self.connect_tailscale = QPushButton("Connect Tailscale")
        layout.addWidget(self.tailscale_status)
        layout.addWidget(self.install_tailscale)
        layout.addWidget(self.connect_tailscale)
        layout.addStretch(1)
        self.addPage(choice)

        summary = QWizardPage()
        summary.setTitle("Runner setup")
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        final_layout = QVBoxLayout(summary)
        final_layout.addWidget(self.summary)
        final_layout.addStretch(1)
        self.addPage(summary)
        self.currentIdChanged.connect(self._show_summary)
        self.install_tailscale.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://tailscale.com/download/windows")))
        self.connect_tailscale.clicked.connect(self._connect_tailscale)
        self._refresh_tailscale()

    def selected_mode(self) -> str:
        return "primary" if self.primary.isChecked() else "backup" if self.backup.isChecked() else "standalone"

    def _show_summary(self, page_id: int) -> None:
        if page_id != 1:
            return
        mode = self.selected_mode()
        self.summary.setText("Checking Runner Agent and secure connectivity…")
        def inspect() -> str:
            _agent_ok, agent_message = self.controller.agent_task_status()
            lines = ["Runner will prepare this computer.", "", f"Runner Agent: {agent_message}"]
            if mode != "standalone":
                try:
                    identity = local_identity(timeout=3.0)
                    lines.extend([f"Secure connection: {'Connected' if identity.online else 'Needs sign-in'}", f"Computer name: {identity.hostname or 'not available'}"])
                except Exception:
                    lines.append("Secure connection: Tailscale needs to be installed or connected")
                lines.append("\nAfter setup, use Servers to pair the other computer. Runner will check compatibility and synchronization automatically.")
            return "\n".join(lines)
        self.workers.submit("wizard-summary", inspect, lambda value, error: self.summary.setText(str(error or value)))

    def _refresh_tailscale(self) -> None:
        self.tailscale_status.setText("Checking secure connectivity…")
        def apply(identity: object, error: BaseException | None) -> None:
            if error:
                self.tailscale_status.setText("Tailscale is required for automatic remote-server connectivity.")
                self.install_tailscale.setVisible(True)
                self.connect_tailscale.setVisible(False)
                return
            self.tailscale_status.setText(f"Secure connection: Connected as {identity.hostname}")
            self.install_tailscale.setVisible(False)
            self.connect_tailscale.setVisible(not identity.online)
        self.workers.submit("wizard-tailscale-status", lambda: local_identity(timeout=3.0), apply)

    def _connect_tailscale(self) -> None:
        def connect() -> None:
            executable = shutil.which("tailscale")
            if not executable:
                raise RuntimeError("Tailscale is not installed")
            subprocess.Popen([executable, "up"], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        self.workers.submit("wizard-tailscale-connect", connect, lambda _value, error: self._tailscale_connect_result(error))

    def _tailscale_connect_result(self, error: BaseException | None) -> None:
        if error:
            QMessageBox.warning(self, "Tailscale", str(error))
        self._refresh_tailscale()

    def accept(self) -> None:
        if self._finishing:
            return
        mode = self.selected_mode()
        self._finishing = True
        finish_button = self.button(QWizard.FinishButton)
        if finish_button:
            finish_button.setEnabled(False)
            finish_button.setText("Preparing…")
        def finish_setup() -> str:
            if mode != "standalone":
                self.controller.prepare_mode(mode)
                self.controller.start_agent_task()
                healthy, reason = self.controller.local_agent_health(timeout=1.0)
                for _ in range(9):
                    if healthy:
                        break
                    threading.Event().wait(0.2)
                    healthy, reason = self.controller.local_agent_health(timeout=1.0)
                if not healthy:
                    raise RuntimeError(f"Runner Agent is not ready: {reason}. Use Repair Runner Agent, then finish setup again.")
            self.controller.commit_mode(mode)
            return mode
        self.workers.submit("wizard-finish", finish_setup, self._finish_result)

    def _finish_result(self, mode: object, error: BaseException | None) -> None:
        finish_button = self.button(QWizard.FinishButton)
        if error:
            self._finishing = False
            if finish_button:
                finish_button.setEnabled(True)
                finish_button.setText("Finish")
            QMessageBox.warning(self, "Runner Agent needs repair", str(error))
            return
        self.workers.shutdown()
        super().accept()

    def reject(self) -> None:
        self.workers.shutdown()
        super().reject()

    def closeEvent(self, event) -> None:
        self.workers.shutdown()
        super().closeEvent(event)
