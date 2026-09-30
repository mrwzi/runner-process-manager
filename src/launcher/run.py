from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import signal
import subprocess
import sys
import time
import zlib
from ctypes import WinDLL, byref, c_int, c_long, c_void_p, get_last_error, sizeof, wintypes
from pathlib import Path
from typing import Any

# ``src`` is the import root in a source checkout.  Packaged builds use the
# PyInstaller bundle and never depend on the repository layout.
SOURCE_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SOURCE_ROOT.parent
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from manager import ProcessManager
from ui.app import MainWindow

PROJECT_NAME = "Runner_V4"
RUNTIME_DIR_NAME = ".runner_runtime"
DWM_WINDOW_ATTRIBUTE_USE_IMMERSIVE_DARK_MODE = 20
DWM_WINDOW_ATTRIBUTE_USE_IMMERSIVE_DARK_MODE_LEGACY = 19
ERROR_ALREADY_EXISTS = 183
KERNEL32 = WinDLL("kernel32", use_last_error=True) if os.name == "nt" else None
DWMAPI = WinDLL("dwmapi", use_last_error=True) if os.name == "nt" else None
if KERNEL32 is not None:
    KERNEL32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    KERNEL32.CreateMutexW.restype = wintypes.HANDLE
    KERNEL32.CloseHandle.argtypes = [wintypes.HANDLE]
    KERNEL32.CloseHandle.restype = wintypes.BOOL
if DWMAPI is not None:
    DWMAPI.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, c_void_p, wintypes.DWORD]
    DWMAPI.DwmSetWindowAttribute.restype = c_long


class SingleInstanceGuard:
    def __init__(self, token: str, *, namespace: str = "Local") -> None:
        self._socket: socket.socket | None = None
        self._mutex_handle: int | None = None
        token_hash = zlib.crc32(token.encode("utf-8")) & 0xFFFFFFFF
        if os.name == "nt":
            mutex_name = f"{namespace}\\{PROJECT_NAME}_{token_hash:08x}"
            handle = KERNEL32.CreateMutexW(None, False, mutex_name)
            if not handle:
                raise OSError("Could not create Runner instance mutex.")
            if get_last_error() == ERROR_ALREADY_EXISTS:
                KERNEL32.CloseHandle(handle)
                raise OSError("Runner is already running.")
            self._mutex_handle = int(handle)
            return

        self._socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        port = 42000 + (token_hash % 1000)
        self._socket.bind(("127.0.0.1", port))
        self._socket.listen(1)

    def close(self) -> None:
        if self._mutex_handle is not None:
            try:
                KERNEL32.CloseHandle(self._mutex_handle)
            except OSError:
                pass
            self._mutex_handle = None
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None


def load_apps(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.exists():
        return []

    raw = json.loads(config_path.read_text(encoding="utf-8-sig"))
    if isinstance(raw, list):
        return [normalize_app(index, item) for index, item in enumerate(raw)]
    if isinstance(raw, dict):
        apps = raw.get("apps", [])
        return [normalize_app(index, item) for index, item in enumerate(apps)]
    raise ValueError("apps.json must contain either an array or an object with an 'apps' list")


def save_apps(config_path: Path, apps: list[dict[str, Any]]) -> None:
    payload = {"apps": apps}
    write_text_atomic(config_path, json.dumps(payload, indent=4))


def copy_available_apps(source_path: Path, destination_path: Path) -> bool:
    """Seed a new local configuration without importing another PC's paths."""
    try:
        apps = load_apps(source_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return False

    available_apps = []
    for app in apps:
        runner_path = Path(str(app.get("runner_path") or app.get("runner_file") or "")).expanduser()
        cwd = Path(str(app.get("cwd") or app.get("folder") or "")).expanduser()
        available = runner_path.is_file() and cwd.is_dir()
        if available:
            available_apps.append(app)

    save_apps(destination_path, available_apps)
    return True


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def normalize_app(index: int, item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"App entry #{index + 1} must be an object")
    runner_path = str(item.get("runner_path") or item.get("runner_file") or item.get("command") or "").strip()
    cwd = str(item.get("cwd") or item.get("folder") or "").strip()
    startup_input = str(item.get("startup_input") or "")
    visible_console = bool(item.get("visible_console", False))
    raw_args = item.get("args", item.get("arguments", []))
    if isinstance(raw_args, str):
        args = [str(arg) for arg in shlex.split(raw_args, posix=os.name != "nt")]
    else:
        args = [str(arg) for arg in raw_args]
    if not cwd and runner_path:
        cwd = str(Path(runner_path).expanduser().resolve().parent)
    return {
        "id": str(item.get("id") or index),
        "name": str(item.get("name") or f"App {index + 1}"),
        "runner_path": runner_path,
        "args": args,
        "cwd": cwd,
        "startup_input": startup_input,
        # Menu-driven apps usually define startup input; treat them as interactive by default.
        "interactive": bool(item.get("interactive", False) or startup_input.strip() or visible_console),
        "visible_console": visible_console,
        "auto_start": bool(item.get("auto_start", False)),
        "env": {str(key): str(value) for key, value in item.get("env", {}).items()},
    }


def apply_dark_theme(app: QApplication) -> None:
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#101417"))
    palette.setColor(QPalette.WindowText, QColor("#f3f4f6"))
    palette.setColor(QPalette.Base, QColor("#171c21"))
    palette.setColor(QPalette.AlternateBase, QColor("#1d242b"))
    palette.setColor(QPalette.Text, QColor("#f3f4f6"))
    palette.setColor(QPalette.Button, QColor("#1b2229"))
    palette.setColor(QPalette.ButtonText, QColor("#f3f4f6"))
    palette.setColor(QPalette.Highlight, QColor("#3a7bd5"))
    palette.setColor(QPalette.HighlightedText, QColor("#ffffff"))
    palette.setColor(QPalette.ToolTipBase, QColor("#171c21"))
    palette.setColor(QPalette.ToolTipText, QColor("#f3f4f6"))
    palette.setColor(QPalette.BrightText, QColor("#ff7b72"))
    palette.setColor(QPalette.PlaceholderText, QColor("#8b98a5"))
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QToolTip {
            color: #f3f4f6;
            background-color: #202830;
            border: 1px solid #465563;
            border-radius: 5px;
            padding: 5px 8px;
        }
        QWidget {
            background-color: #101417;
            color: #f3f4f6;
        }
        QMainWindow, QDialog {
            background-color: #101417;
        }
        /* Frames are structural by default.  Only named surfaces receive a
           border; this avoids the "box inside a box" appearance. */
        QFrame { background: transparent; border: none; }
        QFrame#detailsCard, QFrame#logPanel {
            background-color: #151b20;
            border: 1px solid #222c34;
            border-radius: 8px;
        }
        QFrame#metricStrip { background-color: #171d22; border: none; border-radius: 6px; }
        QFrame#clusterOverview {
            background-color: #131c25;
            border: 1px solid #222c34;
            border-radius: 8px;
        }
        QFrame#overviewTile {
            background-color: #171e24;
            border: none;
            border-left: 2px solid #34414c;
            border-radius: 5px;
        }
        QFrame#overviewTile[tone="good"] { border-left-color: #3e9b69; background-color: #17221f; }
        QFrame#overviewTile[tone="warn"] { border-left-color: #c5963e; background-color: #201f1a; }
        QFrame#overviewTile[tone="bad"] { border-left-color: #bd625e; background-color: #241c1d; }
        QLabel#overviewTitle { color: #9fb4c7; font-size: 11px; font-weight: 700; letter-spacing: 1px; }
        QLabel#overviewSummary { color: #dcecff; font-weight: 600; }
        QLabel#overviewHeading { color: #91a4b5; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
        QLabel#overviewValue { color: #f3f7fb; font-size: 16px; font-weight: 650; }
        QLabel#overviewDetail { color: #9fb0bf; font-size: 11px; }
        QLineEdit, QPlainTextEdit, QTableView {
            background-color: #171c21;
            border: 1px solid #2d3944;
            border-radius: 6px;
            padding: 5px;
            selection-background-color: #1c3548;
        }
        QTableView::item {
            border: none;
            padding: 6px 9px;
        }
        QTableView::item:selected {
            background-color: #1c3548;
            color: #ffffff;
            border: none;
        }
        QHeaderView::section {
            background-color: #1b2229;
            color: #dce3ea;
            border: none;
            border-bottom: 1px solid #2d3944;
            padding: 7px 9px;
        }
        QToolBar {
            background-color: #151b20;
            border: none;
            spacing: 5px;
            padding: 4px 8px;
        }
        QToolBar::separator {
            background-color: #31404d;
            width: 1px;
            margin: 5px 8px;
        }
        QPushButton, QToolButton {
            background-color: #1f2931;
            border: 1px solid #31404d;
            border-radius: 6px;
            min-height: 18px;
            padding: 5px 10px;
        }
        QPushButton:hover, QToolButton:hover {
            background-color: #27313a;
        }
        QPushButton:pressed, QToolButton:pressed {
            background-color: #31404d;
        }
        QPushButton#primaryAction:enabled {
            background-color: #2f72bd;
            border-color: #5798df;
            color: #ffffff;
            font-weight: 600;
        }
        QPushButton#primaryAction:hover:enabled {
            background-color: #3a82d0;
        }
        QToolButton#toolbarPrimary {
            background-color: #2f72bd;
            border-color: #5798df;
            color: #ffffff;
            font-weight: 600;
        }
        QToolButton#detailMore, QToolButton#logMore { min-width: 48px; padding-left: 8px; padding-right: 8px; }
        QToolButton#followLog:checked, QToolButton#wrapLog:checked {
            background-color: #263d4a; border-color: #3e6479; color: #e4f2fb; font-weight: 600;
        }
        QPushButton#compactAction { min-height: 16px; padding: 3px 8px; }
        QLineEdit#consoleInput { padding: 4px 7px; }
        QLabel#appTitle { color: #f5f8fb; font-size: 19px; font-weight: 650; }
        QLabel#appSubtitle { color: #93a4b4; font-size: 12px; }
        QLabel#metricLabel { color: #778793; font-size: 11px; }
        QLabel#metricValue { color: #eef3f7; font-size: 13px; font-weight: 600; }
        QLabel#statusPill { font-weight: 600; border-radius: 10px; padding: 3px 9px; }
        QPlainTextEdit { font-family: Consolas, "Cascadia Mono", monospace; line-height: 1.4; padding: 10px; }
        QToolButton#disclosureButton {
            background: transparent;
            border: none;
            color: #b9c5cf;
            padding: 4px 2px;
            font-weight: 500;
        }
        QToolButton#disclosureButton:hover {
            color: #ffffff;
            background-color: #1d252c;
        }
        QScrollBar:vertical {
            background: #11171c; width: 8px; margin: 2px 1px 2px 1px; border: none;
        }
        QScrollBar::handle:vertical { background: #46525d; min-height: 26px; border-radius: 4px; }
        QScrollBar::handle:vertical:hover { background: #5b6874; }
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; border: none; }
        QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        QScrollBar:horizontal {
            background: #101519; height: 5px; margin: 1px 2px; border: none;
        }
        QScrollBar::handle:horizontal { background: #343e47; min-width: 26px; border-radius: 3px; }
        QScrollBar::handle:horizontal:hover { background: #46525d; }
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; border: none; }
        QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: transparent; }
        QPushButton:disabled, QToolButton:disabled {
            color: #8f9ba6;
            background-color: #1a2026;
            border-color: #2a343d;
        }
        QLabel#fieldLabel {
            color: #8f9ba6;
            font-size: 12px;
        }
        QLabel#appCountLabel {
            color: #94a1ac;
            padding: 0 6px;
        }
        QLabel#emptySearchLabel {
            color: #8f9ba6;
            background: transparent;
            font-size: 14px;
        }
        QCheckBox#startupToggle {
            spacing: 9px;
            padding: 5px 8px;
        }
        QCheckBox#startupToggle::indicator {
            width: 34px;
            height: 18px;
            border-radius: 9px;
            border: 1px solid #46535f;
            background-color: #242c33;
        }
        QCheckBox#startupToggle::indicator:checked {
            background-color: #3a7bd5;
            border-color: #65a3ef;
        }
        QLabel {
            background: transparent;
            border: none;
        }
        QMessageBox QLabel {
            color: #f3f4f6;
        }
        """
    )


def resource_path(relative_path: str) -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)) / relative_path
    return SOURCE_ROOT / relative_path


def runner_icon() -> QIcon:
    icon = QIcon()
    for relative_path in (
        "assets/runner_icon_16x16.png",
        "assets/runner_icon_32x32.png",
        "assets/runner_icon_48x48.png",
        "assets/runner_icon_256x256.png",
        "assets/runner_icon.ico",
    ):
        path = resource_path(relative_path)
        if path.exists():
            icon.addFile(str(path))
    return icon


def apply_dark_title_bar(window: MainWindow) -> None:
    if os.name != "nt":
        return
    try:
        hwnd = int(window.winId())
        enabled = c_int(1)
        for attribute in (
            DWM_WINDOW_ATTRIBUTE_USE_IMMERSIVE_DARK_MODE,
            DWM_WINDOW_ATTRIBUTE_USE_IMMERSIVE_DARK_MODE_LEGACY,
        ):
            result = DWMAPI.DwmSetWindowAttribute(
                hwnd,
                attribute,
                byref(enabled),
                sizeof(enabled),
            )
            if result == 0:
                break
    except Exception:
        return


def find_project_root(start: Path) -> Path:
    for candidate in [start, *start.parents]:
        if (candidate / "src" / "launcher" / "run.py").exists() and (candidate / "build").is_dir():
            return candidate
    return start


def _runtime_root_from_args(argv: list[str]) -> Path | None:
    for index, arg in enumerate(argv):
        if arg == "--runtime-root" and index + 1 < len(argv):
            return Path(argv[index + 1]).expanduser()
        if arg.startswith("--runtime-root="):
            return Path(arg.split("=", 1)[1]).expanduser()
    return None


def resolve_runtime_paths(runtime_root_override: Path | None = None) -> tuple[Path, Path]:
    source_root = SOURCE_ROOT
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    executable_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else source_root
    project_root = find_project_root(executable_root)
    running_from_source_tree = (
        (project_root / "src" / "launcher" / "run.py").exists()
        and (project_root / "build").is_dir()
    )
    installed_runtime = Path(os.environ.get("PROGRAMDATA", local_appdata)) / PROJECT_NAME
    runtime_root = runtime_root_override or (
        project_root / RUNTIME_DIR_NAME
        if running_from_source_tree
        else installed_runtime
    )

    bundled_config = project_root / "src" / "config" / "apps.json"
    legacy_roots = [
        project_root,
        executable_root,
        local_appdata / PROJECT_NAME,
        local_appdata / PROJECT_NAME,
        local_appdata / "Runner_V3",
        local_appdata / "Runner_V2",
        local_appdata / "Runner_V1",
    ]

    runtime_root.mkdir(parents=True, exist_ok=True)
    logs_dir = runtime_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_root / "apps.json"

    def app_count(path: Path) -> int:
        try:
            return len(load_apps(path))
        except (OSError, ValueError, json.JSONDecodeError):
            return 0

    # An empty ProgramData registry is not evidence that this is a clean
    # machine.  Legacy installs can still contain the user's non-empty app
    # registry; migration must recover it or fail visibly instead of silently
    # presenting an empty Runner UI.
    legacy_app_count = sum(
        app_count(root / "apps.json") if (root / "apps.json").exists()
        else app_count(root / "config" / "apps.json")
        for root in legacy_roots
        if root.exists()
    )
    if not config_path.exists() or (app_count(config_path) == 0 and legacy_app_count > 0):
        if config_path.exists():
            backup_dir = runtime_root / "config-backups"
            backup_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(config_path, backup_dir / f"apps.empty-before-recovery-{int(time.time())}.json")
            config_path.unlink()
        migrated = migrate_legacy_data(legacy_roots, runtime_root)
        if migrated and app_count(config_path) > 0:
            pass
        elif legacy_app_count > 0:
            raise RuntimeError(
                f"Runner found {legacy_app_count} legacy application records but could not migrate any. "
                "Recovery was stopped; inspect config-backups before continuing."
            )
        elif bundled_config.exists():
            if not copy_available_apps(bundled_config, config_path):
                write_text_atomic(config_path, json.dumps({"apps": []}, indent=4))
        else:
            write_text_atomic(config_path, json.dumps({"apps": []}, indent=4))

    return config_path, logs_dir


def migrate_legacy_data(legacy_roots: list[Path], runtime_root: Path) -> bool:
    runtime_config = runtime_root / "apps.json"
    runtime_logs = runtime_root / "logs"
    migrated = False

    for legacy_root in legacy_roots:
        try:
            if legacy_root.resolve() == runtime_root.resolve():
                continue
        except OSError:
            continue

        legacy_config = legacy_root / "apps.json"
        if not legacy_config.exists():
            legacy_config = legacy_root / "config" / "apps.json"
        legacy_logs = legacy_root / "logs"

        # An installed upgrade moves the existing node identity and encrypted
        # state into ProgramData only when the new runtime has none.  A clean
        # installer never carries these files, so a second machine still gets
        # a brand-new identity.
        for name in ("cluster.json", "identity.json", "secrets.enc", "secrets.key", "onboarding.json"):
            source = legacy_root / name
            destination = runtime_root / name
            if source.exists() and not destination.exists():
                try:
                    shutil.copy2(source, destination)
                    migrated = True
                except OSError:
                    continue

        if legacy_config.exists() and not runtime_config.exists():
            migrated = copy_available_apps(legacy_config, runtime_config)

        if legacy_logs.is_dir():
            runtime_logs.mkdir(parents=True, exist_ok=True)
            for entry in legacy_logs.iterdir():
                if not entry.is_file():
                    continue
                target = runtime_logs / entry.name
                if target.exists():
                    continue
                try:
                    shutil.copy2(entry, target)
                except OSError:
                    continue
                migrated = True

    return migrated


def complete_first_run_setup(app: QApplication, runtime_root: Path, wizard_type=None) -> tuple[bool, str]:
    """Run the welcome wizard and return its accepted role without enum ambiguity."""
    if wizard_type is None:
        from ui.setup_wizard import SetupWizard
        wizard_type = SetupWizard
    wizard = wizard_type(runtime_root)
    # QWizard inherits QDialog, but dialog result values live on QDialog's
    # DialogCode enum.  Do not look them up on the wizard instance.
    if wizard.exec() != QDialog.DialogCode.Accepted:
        return False, ""
    return True, str(wizard.selected_mode())


def repair_local_agent(install_dir: Path, runtime_root: Path) -> tuple[bool, str]:
    """Run the installed repair helper elevated, then verify the signed local API.

    This deliberately repairs only Runner infrastructure.  It never calls any
    application stop/restart endpoint and the Agent reconciles existing
    processes after it is alive.
    """
    if os.name != "nt":
        return False, "Use the Runner Agent systemd repair command on Linux"
    script = install_dir / "tools" / "repair-runner.ps1"
    if not script.exists():
        return False, f"Repair helper is missing: {script}"

    def ps_quote(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    command = (
        "$p=Start-Process -FilePath 'powershell.exe' -Verb RunAs -Wait -PassThru "
        "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File',"
        f"{ps_quote(str(script))},'-InstallDir',{ps_quote(str(install_dir))},"
        f"'-RuntimeRoot',{ps_quote(str(runtime_root))}); exit $p.ExitCode"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", command],
            capture_output=True, text=True, timeout=45,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception as exc:
        return False, f"Could not start Runner Agent repair: {exc}"
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "The elevated repair was cancelled or failed.").strip()
        return False, detail
    from cluster.onboarding import OnboardingController
    return OnboardingController(runtime_root).local_agent_health(timeout=4.0)


def main() -> int:
    if "--process-guard" in sys.argv:
        from cluster.process_guard import main as guard_main
        index = sys.argv.index("--process-guard")
        return guard_main(sys.argv[index + 1 :])
    if "--witness" in sys.argv:
        from cluster.witness_server import main as witness_main
        index = sys.argv.index("--witness")
        return witness_main(sys.argv[index + 1 :])
    base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT
    runtime_root_override = _runtime_root_from_args(sys.argv)
    config_path, logs_dir = resolve_runtime_paths(runtime_root_override)

    if "--agent" in sys.argv:
        from cluster.agent import RunnerAgent
        from cluster.api import AgentApiServer

        app = QCoreApplication(sys.argv)
        app.setApplicationName("Runner Agent")
        try:
            agent_guard = SingleInstanceGuard(f"agent:{config_path.parent.resolve()}", namespace="Global")
        except OSError:
            # Scheduled Task + GUI fallback can race at logon.  A machine-wide
            # mutex prevents a second Agent from creating ownership threads or
            # reconciling processes before its API bind would fail.
            return 0
        agent = RunnerAgent(config_path.parent)
        api = AgentApiServer(agent)
        api.start()
        remote_api = None
        peer_service = None
        if agent.cluster.enabled:
            import base64
            from cluster.remote_api import RemoteAgentServer
            from cluster.secrets_store import SecretStore
            from cluster.transport import TailscaleTransport
            from cluster.peer_service import PeerService

            trusted_records = SecretStore(config_path.parent).get("trusted_nodes", {})
            trusted = {
                node_id: base64.urlsafe_b64decode(record["shared_secret"])
                for node_id, record in trusted_records.items()
            }
            endpoints = {str(node["node_id"]): str(node["endpoint"]) for node in agent.cluster.nodes}
            if (not agent.cluster.tls_certificate or not agent.cluster.tls_private_key) and agent.cluster.transport == "tailscale":
                try:
                    from cluster.tailscale import ensure_certificate, local_identity
                    tailscale_identity = local_identity()
                    certificate, private_key = ensure_certificate(config_path.parent, tailscale_identity.endpoint_host)
                    agent.cluster.tls_certificate = str(certificate)
                    agent.cluster.tls_private_key = str(private_key)
                    agent.store.save_cluster(agent.cluster)
                except Exception as exc:
                    agent.last_transition_reason = f"Remote Agent API unavailable: {exc}"
            if agent.cluster.tls_certificate and agent.cluster.tls_private_key:
                remote_api = RemoteAgentServer(
                    agent,
                    agent.cluster.remote_host,
                    agent.cluster.remote_port,
                    trusted,
                    Path(agent.cluster.tls_certificate),
                    Path(agent.cluster.tls_private_key),
                )
                remote_api.start()
                peer_service = PeerService(agent, TailscaleTransport(agent.cluster.node_id, endpoints, trusted))
                peer_service.start()
        QTimer.singleShot(1200, agent.start_configured)
        signal.signal(signal.SIGINT, lambda *_: app.quit())
        if hasattr(signal, "SIGTERM"):
            signal.signal(signal.SIGTERM, lambda *_: app.quit())
        exit_code = app.exec()
        if peer_service:
            peer_service.stop()
        if remote_api:
            remote_api.stop()
        api.stop()
        # A controlled service restart (including a binary upgrade) must not
        # treat Agent-owned applications as installer children. The process
        # guard/lease expiry remains responsible for fencing protected apps if
        # the replacement Agent cannot return in time.
        agent.shutdown(stop_applications=False)
        agent_guard.close()
        return exit_code

    if "--headless" in sys.argv:
        app = QCoreApplication(sys.argv)
        app.setApplicationName("Runner")
        try:
            from cluster.config import ConfigStore
            apps = ConfigStore(config_path.parent).migrate_apps()["apps"]
        except Exception:
            return 1
        manager = ProcessManager(apps, logs_dir=logs_dir)
        QTimer.singleShot(1200, manager.start_auto_start_apps)
        exit_code = app.exec()
        manager.shutdown()
        return exit_code

    app = QApplication(sys.argv)
    app.setApplicationName("Runner")
    apply_dark_theme(app)
    icon = runner_icon()
    if not icon.isNull():
        app.setWindowIcon(icon)

    # First-run configuration is intentionally before the main controller: a
    # normal user chooses a role, never edits a JSON file or starts a service.
    from cluster.onboarding import OnboardingController
    onboarding = OnboardingController(config_path.parent)
    if not onboarding.complete():
        accepted, selected_mode = complete_first_run_setup(app, config_path.parent)
        if not accepted:
            return 0
    try:
        guard = SingleInstanceGuard(str(base_dir))
    except OSError:
        return 1

    try:
        from cluster.config import ConfigStore
        apps = ConfigStore(config_path.parent).migrate_apps()["apps"]
    except Exception as exc:
        QMessageBox.critical(None, "Runner", f"Failed to load apps.json:\n{exc}")
        guard.close()
        return 1

    manager: Any
    try:
        from cluster.config import ConfigStore
        cluster_config = ConfigStore(config_path.parent).load_cluster()
    except Exception:
        cluster_config = None
    if cluster_config and cluster_config.enabled:
        from cluster.client import AgentClient
        if getattr(sys, "frozen", False):
            agent_command = [str(Path(sys.executable).resolve()), "--agent", "--runtime-root", str(config_path.parent)]
        else:
            agent_command = [sys.executable, str(SOURCE_ROOT / "launcher" / "run.py"), "--agent", "--runtime-root", str(config_path.parent)]
        # The controller opens immediately with the locally migrated registry.
        # Agent startup/reconnect and health checks run on AgentClient's worker,
        # so an unavailable or slow Agent never blocks the Qt event loop.
        manager = AgentClient(config_path.parent, fallback_apps=apps, agent_command=agent_command)
    else:
        manager = ProcessManager(apps, logs_dir=logs_dir)
    window = MainWindow(manager, config_path=config_path)
    if not icon.isNull():
        window.setWindowIcon(icon)
    window.show()
    apply_dark_title_bar(window)
    QTimer.singleShot(1200, window.start_configured_apps)

    exit_code = app.exec()
    manager.shutdown()
    guard.close()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
