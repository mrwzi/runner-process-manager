from __future__ import annotations

import json
import os
import shlex
import shutil
import socket
import sys
import zlib
from ctypes import WinDLL, byref, c_int, c_long, c_void_p, get_last_error, sizeof, wintypes
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtGui import QColor, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QMessageBox

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
    def __init__(self, token: str) -> None:
        self._socket: socket.socket | None = None
        self._mutex_handle: int | None = None
        token_hash = zlib.crc32(token.encode("utf-8")) & 0xFFFFFFFF
        if os.name == "nt":
            mutex_name = f"Local\\{PROJECT_NAME}_{token_hash:08x}"
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
        if runner_path.is_file() and cwd.is_dir():
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
        QWidget {
            background-color: #101417;
            color: #f3f4f6;
        }
        QMainWindow, QDialog {
            background-color: #101417;
        }
        QFrame {
            background-color: #151b20;
            border: 1px solid #27313a;
            border-radius: 8px;
        }
        QLineEdit, QPlainTextEdit, QTableView {
            background-color: #171c21;
            border: 1px solid #2d3944;
            border-radius: 6px;
            padding: 4px;
            selection-background-color: #3a7bd5;
        }
        QTableView::item {
            border: none;
            padding: 5px 7px;
        }
        QTableView::item:selected {
            background-color: #285f9e;
            color: #ffffff;
            border: none;
        }
        QHeaderView::section {
            background-color: #1b2229;
            color: #dce3ea;
            border: none;
            border-bottom: 1px solid #2d3944;
            padding: 6px;
        }
        QToolBar {
            background-color: #151b20;
            border: none;
            spacing: 6px;
            padding: 5px 8px;
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
            padding: 6px 12px;
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
    return PROJECT_ROOT / relative_path


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
        if (candidate / "launcher" / "run.py").exists() and (candidate / "build").is_dir() and (candidate / "manager").is_dir():
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
    source_root = PROJECT_ROOT
    local_appdata = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    executable_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else source_root
    project_root = find_project_root(executable_root)
    running_from_source_tree = (
        (project_root / "launcher" / "run.py").exists()
        and (project_root / "build").is_dir()
        and (project_root / "manager").is_dir()
    )
    runtime_root = runtime_root_override or (
        project_root / RUNTIME_DIR_NAME
        if running_from_source_tree
        else local_appdata / PROJECT_NAME
    )

    bundled_config = project_root / "config" / "apps.json"
    legacy_roots = [
        project_root,
        executable_root,
        local_appdata / PROJECT_NAME,
        local_appdata / "Runner_V3",
        local_appdata / "Runner_V2",
        local_appdata / "Runner_V1",
    ]

    runtime_root.mkdir(parents=True, exist_ok=True)
    logs_dir = runtime_root / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    config_path = runtime_root / "apps.json"

    if not config_path.exists():
        migrated = migrate_legacy_data(legacy_roots, runtime_root)
        if migrated:
            pass
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


def main() -> int:
    base_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else PROJECT_ROOT
    runtime_root_override = _runtime_root_from_args(sys.argv)
    config_path, logs_dir = resolve_runtime_paths(runtime_root_override)

    if "--headless" in sys.argv:
        app = QCoreApplication(sys.argv)
        app.setApplicationName("Runner")
        try:
            apps = load_apps(config_path)
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

    try:
        guard = SingleInstanceGuard(str(base_dir))
    except OSError:
        return 1

    try:
        apps = load_apps(config_path)
    except Exception as exc:
        QMessageBox.critical(None, "Runner", f"Failed to load apps.json:\n{exc}")
        guard.close()
        return 1

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
