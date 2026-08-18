from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRectF, QSize, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QHeaderView,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTextEdit,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


UI_LOG_BLOCK_LIMIT = 300
RUNNER_FILE_SUFFIXES = {".exe", ".py", ".js", ".mjs", ".bat", ".cmd"}
STARTUP_CMD_NAME = "Runner Auto Start.cmd"
STATUS_COLORS = {
    "Running": "#55c982",
    "Already Running": "#69aaf2",
    "Starting": "#e8b85c",
    "Waiting Input": "#e8b85c",
    "Stopping": "#e8b85c",
    "Crashed": "#f07970",
    "Stopped": "#8996a1",
    "Stopped by Runner": "#8996a1",
}
_STATUS_ICONS: dict[str, QIcon] = {}


def status_icon(status: str) -> QIcon:
    color = STATUS_COLORS.get(status, "#8996a1")
    if color not in _STATUS_ICONS:
        pixmap = QPixmap(10, 10)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(color))
        painter.drawEllipse(QRectF(1, 1, 8, 8))
        painter.end()
        _STATUS_ICONS[color] = QIcon(pixmap)
    return _STATUS_ICONS[color]


def format_uptime(value: int | None) -> str:
    if value is None:
        return "N/A"
    hours, remainder = divmod(value, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}"


def save_apps_file(config_path: Path, apps: list[dict[str, Any]]) -> None:
    write_text_atomic(config_path, json.dumps({"apps": apps}, indent=4))


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def detect_runner_file(folder: str) -> str:
    base_path = Path(folder).expanduser()
    if not base_path.is_dir():
        return ""

    folder_name = base_path.name.lower()
    candidates: list[tuple[int, str]] = []
    skip_dirs = {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "venv",
        "env",
        "node_modules",
        "build",
        "dist",
        "logs",
    }
    max_candidates = 250
    max_seen_files = 5000

    seen_files = 0
    for root, dirs, files in os.walk(base_path):
        dirs[:] = [name for name in dirs if name.lower() not in skip_dirs]
        root_path = Path(root)

        for file_name in files:
            seen_files += 1
            if seen_files > max_seen_files or len(candidates) >= max_candidates:
                break

            path = root_path / file_name
            if path.suffix.lower() not in RUNNER_FILE_SUFFIXES:
                continue
            try:
                path.is_file()
            except OSError:
                continue
            if not path.is_file():
                continue
            try:
                resolved = str(path.resolve())
            except OSError:
                resolved = str(path)
            candidates.append((_runner_score(base_path, path, folder_name), resolved))

        if seen_files > max_seen_files or len(candidates) >= max_candidates:
            break

    if not candidates:
        return ""
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def _runner_score(base_path: Path, path: Path, folder_name: str) -> int:
    stem = path.stem.lower()
    suffix = path.suffix.lower()
    relative = path.relative_to(base_path)
    depth = len(relative.parts) - 1
    parent_parts = [part.lower() for part in relative.parts[:-1]]

    score = 0
    if suffix == ".exe":
        score += 60
    elif suffix == ".py":
        score += 40
    elif suffix in {".js", ".mjs"}:
        score += 40
    elif suffix in {".bat", ".cmd"}:
        score += 48

    score -= depth * 8
    if depth == 0:
        score += 70

    if stem == folder_name:
        score += 120
    elif folder_name and folder_name in stem:
        score += 80

    if stem in {"main", "app", "run", "start", "launcher", "start-tunnel"}:
        score += 45
    if stem.startswith(("run", "start", "launch")):
        score += 85
    if stem == "server":
        score += 60
    elif stem == "app":
        score += 55
    elif stem == "bot":
        score += 20
    elif stem.endswith(("server", "runner")):
        score += 35
    elif stem.endswith("bot"):
        score += 25
    if stem == "__main__":
        score += 40

    if "dist" in parent_parts:
        score += 20
    if "src" in parent_parts:
        score -= 35
    if relative.name.lower() == "__init__.py":
        score -= 120

    if stem.startswith("update"):
        score -= 70

    if "test" in stem or "spec" in stem:
        score -= 40

    if suffix == ".py":
        score += _python_entrypoint_bonus(path)

    return score


def _python_entrypoint_bonus(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as handle:
            snippet = handle.read(4000).lower()
    except OSError:
        return 0

    bonus = 0
    if 'if __name__ == "__main__"' in snippet or "if __name__ == '__main__'" in snippet:
        bonus += 80
    if "argparse.argumentparser" in snippet:
        bonus += 25
    if "asyncio.run(" in snippet:
        bonus += 15
    if any(token in snippet for token in ("from flask import", "flask(", "socketio(", "applicationbuilder(", "uvicorn")):
        bonus += 55
    if "compatibility wrapper" in snippet or "legacy imports" in snippet:
        bonus -= 35
    return bonus


class AppTableModel(QAbstractTableModel):
    columns = ["App", "Type", "Status"]

    def __init__(self, snapshots: list[dict[str, Any]], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: list[str] = []
        self._row_lookup: dict[str, int] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self.replace_snapshots(snapshots)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.DisplayRole) -> Any:
        if role != Qt.DisplayRole:
            return None
        if orientation == Qt.Horizontal:
            return self.columns[section]
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole) -> Any:
        if not index.isValid():
            return None

        record = self._records[self._rows[index.row()]]
        if role == Qt.ToolTipRole:
            if index.column() == 0:
                return str(record.get("name") or "")
            if index.column() == 1:
                return str(record.get("runner_path") or "")
        if role == Qt.DecorationRole and index.column() == 2:
            return status_icon(str(record.get("status") or "Stopped"))
        if role == Qt.DisplayRole:
            column = index.column()
            if column == 0:
                return record["name"]
            if column == 1:
                suffix = Path(str(record.get("runner_path") or "")).suffix.lower()
                return {
                    ".py": "Python",
                    ".js": "Node.js",
                    ".mjs": "Node.js",
                    ".bat": "Batch",
                    ".cmd": "Batch",
                    ".exe": "EXE",
                }.get(suffix, suffix.lstrip(".").upper() or "App")
            if column == 2:
                return record["status"]
        if role == Qt.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignCenter)
        return None

    def app_id_at(self, row: int) -> str | None:
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def update_snapshot(self, app_id: str, snapshot: dict[str, Any]) -> None:
        if app_id not in self._records:
            return
        previous = self._records[app_id]
        self._records[app_id] = snapshot
        row = self._row_lookup[app_id]
        keys = ["name", "runner_path", "status"]
        for column, key in enumerate(keys):
            if previous.get(key) != snapshot.get(key):
                model_index = self.index(row, column)
                self.dataChanged.emit(model_index, model_index, [Qt.DisplayRole])

    def replace_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        self.beginResetModel()
        self._rows = [snapshot["id"] for snapshot in snapshots]
        self._row_lookup = {app_id: index for index, app_id in enumerate(self._rows)}
        self._records = {snapshot["id"]: snapshot for snapshot in snapshots}
        self.endResetModel()


class ElidedLabel(QLabel):
    """Single-line detail value that keeps its complete text in a tooltip."""

    def __init__(self, text: str = "N/A", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.setFullText(text)

    def setFullText(self, text: str) -> None:
        self._full_text = str(text)
        self.setToolTip("" if self._full_text == "N/A" else self._full_text)
        self._refresh_elision()

    def fullText(self) -> str:
        return self._full_text

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._refresh_elision()

    def _refresh_elision(self) -> None:
        available = max(20, self.width() - 4)
        QLabel.setText(self, self.fontMetrics().elidedText(self._full_text, Qt.ElideMiddle, available))


class StartupSwitch(QAbstractButton):
    """Compact, accessible switch with an explicit ON/OFF state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAccessibleName("Start with Windows")

    def sizeHint(self) -> QSize:
        return QSize(188, 34)

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        hovered = self.underMouse()
        track = QRectF(8, 8, 38, 18)
        if self.isChecked():
            track_color = QColor("#397fce" if not hovered else "#4992e3")
            border_color = QColor("#67a8ef")
            thumb_x = track.right() - 16
        else:
            track_color = QColor("#26313a" if not hovered else "#303d47")
            border_color = QColor("#52616d")
            thumb_x = track.left() + 2

        painter.setPen(QPen(border_color, 1))
        painter.setBrush(track_color)
        painter.drawRoundedRect(track, 9, 9)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#ffffff" if self.isChecked() else "#aeb9c2"))
        painter.drawEllipse(QRectF(thumb_x, track.top() + 2, 14, 14))

        painter.setPen(QColor("#e7edf2"))
        painter.drawText(QRectF(55, 0, 108, self.height()), Qt.AlignVCenter | Qt.AlignLeft, "Start with Windows")
        painter.setPen(QColor("#74d69b" if self.isChecked() else "#9aa6b1"))
        painter.drawText(QRectF(158, 0, 28, self.height()), Qt.AlignVCenter | Qt.AlignRight, "ON" if self.isChecked() else "OFF")

        if self.hasFocus():
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#6aa9ef"), 1))
            painter.drawRoundedRect(QRectF(1, 1, self.width() - 2, self.height() - 2), 6, 6)


class BusyOverlay(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setVisible(False)
        self.setStyleSheet("background-color: rgba(15, 23, 42, 120);")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addStretch(1)
        self.label = QLabel("", self)
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setStyleSheet(
            "color: white; background-color: rgba(15, 23, 42, 180);"
            "padding: 18px 28px; border-radius: 10px; font-size: 15px;"
        )
        layout.addWidget(self.label, alignment=Qt.AlignCenter)
        layout.addStretch(1)

    def show_message(self, text: str) -> None:
        self.label.setText(text)
        self.raise_()
        self.show()


class AppEditorDialog(QDialog):
    def __init__(self, parent: QWidget | None = None, initial: dict[str, Any] | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("App Editor" if initial else "Add App")
        self.resize(620, 280)
        initial = initial or {}

        layout = QVBoxLayout(self)
        form = QFormLayout()
        layout.addLayout(form)

        self.name_input = QLineEdit(initial.get("name", ""), self)
        form.addRow("App Name", self.name_input)

        self.folder_input = QLineEdit(initial.get("cwd", ""), self)
        self.folder_input.editingFinished.connect(self._autofill_from_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_input)
        folder_button = QPushButton("Browse", self)
        folder_button.clicked.connect(self._browse_folder)
        folder_row.addWidget(folder_button)
        form.addRow("Folder", self._wrap_row(folder_row))

        self.runner_input = QLineEdit(initial.get("runner_path", ""), self)
        self.runner_input.editingFinished.connect(self._autofill_name_from_runner)
        runner_row = QHBoxLayout()
        runner_row.addWidget(self.runner_input)
        runner_button = QPushButton("Browse", self)
        runner_button.clicked.connect(self._browse_runner)
        runner_row.addWidget(runner_button)
        form.addRow("Runner File", self._wrap_row(runner_row))

        args_text = subprocess.list2cmdline(initial.get("args", [])) if initial.get("args") else ""
        self.args_input = QLineEdit(args_text, self)
        form.addRow("Arguments", self.args_input)

        self.startup_input = QPlainTextEdit(self)
        self.startup_input.setPlainText(initial.get("startup_input", ""))
        self.startup_input.setPlaceholderText("Optional startup inputs. Use one line per answer.")
        self.startup_input.setFixedHeight(78)
        self.startup_input.textChanged.connect(self._sync_interactive_from_startup)
        form.addRow("Startup Input", self.startup_input)

        self.interactive_input = QCheckBox("Requires stdin / menu input", self)
        self.interactive_input.setChecked(bool(initial.get("interactive", False)))
        form.addRow("Interactive", self.interactive_input)

        self.visible_console_input = QCheckBox("Open visible console window", self)
        self.visible_console_input.setChecked(bool(initial.get("visible_console", False)))
        self.visible_console_input.toggled.connect(self._sync_interactive_from_console)
        form.addRow("Visible Console", self.visible_console_input)

        self.auto_start_input = QCheckBox("Start when Runner opens", self)
        self.auto_start_input.setChecked(bool(initial.get("auto_start", False)))
        form.addRow("Auto Start", self.auto_start_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def app_data(self) -> dict[str, Any]:
        args_text = self.args_input.text().strip()
        args = shlex.split(args_text, posix=False) if args_text else []
        return {
            "name": self.name_input.text().strip(),
            "cwd": self.folder_input.text().strip(),
            "runner_path": self.runner_input.text().strip(),
            "args": args,
            "startup_input": self.startup_input.toPlainText(),
            "interactive": self.interactive_input.isChecked() or self.visible_console_input.isChecked(),
            "visible_console": self.visible_console_input.isChecked(),
            "auto_start": self.auto_start_input.isChecked(),
            "env": {},
        }

    def _browse_folder(self) -> None:
        selected = QFileDialog.getExistingDirectory(self, "Select App Folder", self.folder_input.text().strip())
        if selected:
            self.folder_input.setText(selected)
            self._autofill_from_folder()

    def _browse_runner(self) -> None:
        start_dir = self.folder_input.text().strip() or str(Path.home())
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Runner File",
            start_dir,
            "Runner Files (*.exe *.py *.js *.mjs *.bat *.cmd);;All Files (*)",
        )
        if file_path:
            self.runner_input.setText(file_path)
            if not self.folder_input.text().strip():
                self.folder_input.setText(str(Path(file_path).parent))
            self._autofill_name_from_runner()

    def _autofill_from_folder(self) -> None:
        folder = self.folder_input.text().strip()
        if not folder:
            return
        if not self.name_input.text().strip():
            self.name_input.setText(Path(folder).name)
        if not self.runner_input.text().strip():
            detected = detect_runner_file(folder)
            if detected:
                self.runner_input.setText(detected)
                self._autofill_name_from_runner()

    def _autofill_name_from_runner(self) -> None:
        runner_text = self.runner_input.text().strip()
        if not runner_text or self.name_input.text().strip():
            return
        self.name_input.setText(Path(runner_text).stem)

    def _sync_interactive_from_startup(self) -> None:
        if self.startup_input.toPlainText().strip():
            self.interactive_input.setChecked(True)

    def _sync_interactive_from_console(self, checked: bool) -> None:
        if checked:
            self.interactive_input.setChecked(True)

    @staticmethod
    def _wrap_row(layout: QHBoxLayout) -> QWidget:
        widget = QWidget()
        widget.setLayout(layout)
        return widget


class MainWindow(QMainWindow):
    def __init__(self, manager: Any, config_path: str | Path) -> None:
        super().__init__()
        self.manager = manager
        self.config_path = Path(config_path)
        self.ui_state_path = self.config_path.parent / "ui_state.json"
        self.selected_app_id: str | None = None
        self.batch_active = False
        self._pending_select_app_id: str | None = None
        self._last_action_state: tuple[bool, ...] | None = None
        self._log_autoscroll = True
        self.model = AppTableModel(manager.app_definitions(), self)
        self.proxy_model = QSortFilterProxyModel(self)
        self.proxy_model.setSourceModel(self.model)
        self.proxy_model.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy_model.setFilterKeyColumn(-1)

        self.setWindowTitle("Runner")
        self.resize(1280, 760)

        self._build_toolbar()
        self._build_layout()
        self._connect_signals()
        self._restore_ui_state()
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)
        QTimer.singleShot(0, self._update_app_count)

        self.log_timer = QTimer(self)
        self.log_timer.setInterval(200)
        self.log_timer.timeout.connect(self._flush_selected_logs)
        self.log_timer.start()

        self.detail_timer = QTimer(self)
        self.detail_timer.setInterval(1000)
        self.detail_timer.timeout.connect(self._tick_selected_details)
        self.detail_timer.start()

        if self.model.rowCount() > 0:
            self.table.selectRow(0)

    def _build_toolbar(self) -> None:
        toolbar = QToolBar("Controls", self)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(toolbar)

        self.add_action = QAction("Add App", self)
        self.edit_action = QAction("Edit App", self)
        self.delete_action = QAction("Delete App", self)
        self.start_all_action = QAction("Start All", self)
        self.stop_all_action = QAction("Stop All", self)
        self.start_with_windows_action = StartupSwitch(toolbar)
        self.start_with_windows_action.setChecked(self._startup_enabled())
        self.start_with_windows_action.setObjectName("startupToggle")
        self._refresh_start_with_windows_action()

        for action in [self.add_action, self.edit_action, self.delete_action]:
            toolbar.addAction(action)
        toolbar.addSeparator()
        toolbar.addAction(self.start_all_action)
        toolbar.addAction(self.stop_all_action)
        self.app_filter = QLineEdit(toolbar)
        self.app_filter.setPlaceholderText("Search apps, type, or status...")
        self.app_filter.setClearButtonEnabled(True)
        self.app_filter.setMaximumWidth(260)
        self.app_filter.setToolTip("Filter by app name, type, or status")
        toolbar.addWidget(self.app_filter)
        self.app_count_label = QLabel("0 apps", toolbar)
        self.app_count_label.setObjectName("appCountLabel")
        toolbar.addWidget(self.app_count_label)
        spacer = QWidget(toolbar)
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        toolbar.addWidget(spacer)
        toolbar.addWidget(self.start_with_windows_action)

    def _build_layout(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.splitter = QSplitter(Qt.Horizontal, central)
        layout.addWidget(self.splitter)
        self.setCentralWidget(central)

        self.table = QTableView(self.splitter)
        self.table.setModel(self.proxy_model)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.SingleSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(30)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setMinimumSectionSize(56)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 76)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setMinimumWidth(390)
        self.table.setMaximumWidth(520)
        self.no_matches_label = QLabel("No matching apps", self.table.viewport())
        self.no_matches_label.setAlignment(Qt.AlignCenter)
        self.no_matches_label.setObjectName("emptySearchLabel")
        self.no_matches_label.hide()

        right_panel = QWidget(self.splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        details_card = QFrame(right_panel)
        self.details_card = details_card
        details_card.setFrameShape(QFrame.StyledPanel)
        details_layout = QVBoxLayout(details_card)
        details_layout.setContentsMargins(10, 10, 10, 10)
        details_layout.setSpacing(8)

        self.summary_label = QLabel("Select an app", details_card)
        self.summary_label.setStyleSheet("font-size: 18px; font-weight: 600;")
        details_layout.addWidget(self.summary_label)

        summary_grid = QGridLayout()
        summary_grid.setHorizontalSpacing(8)
        summary_grid.setVerticalSpacing(8)
        self.status_value = QLabel("N/A", details_card)
        self.pid_value = QLabel("N/A", details_card)
        self.cpu_value = QLabel("N/A", details_card)
        self.ram_value = QLabel("N/A", details_card)
        self.uptime_value = QLabel("N/A", details_card)
        self.exit_code_value = QLabel("N/A", details_card)
        self.status_value.setObjectName("statusBadge")
        self.last_error_value = ElidedLabel("N/A", details_card)
        self.folder_value = ElidedLabel("N/A", details_card)
        self.runner_file_value = ElidedLabel("N/A", details_card)
        self.full_command_value = ElidedLabel("N/A", details_card)
        self.log_file_value = ElidedLabel("N/A", details_card)

        metrics = [
            ("Status", self.status_value),
            ("PID", self.pid_value),
            ("Exit result", self.exit_code_value),
            ("CPU", self.cpu_value),
            ("RAM", self.ram_value),
            ("Uptime", self.uptime_value),
        ]
        for index, (title, value) in enumerate(metrics):
            metric = QWidget(details_card)
            metric_layout = QVBoxLayout(metric)
            metric_layout.setContentsMargins(8, 5, 8, 5)
            metric_layout.setSpacing(2)
            label = QLabel(title, metric)
            label.setObjectName("fieldLabel")
            metric_layout.addWidget(label)
            metric_layout.addWidget(value)
            summary_grid.addWidget(metric, index // 3, index % 3)
        for column in range(3):
            summary_grid.setColumnStretch(column, 1)
        details_layout.addLayout(summary_grid)

        path_rows = [
            ("Folder", self.folder_value),
            ("Runner", self.runner_file_value),
            ("Command", self.full_command_value),
            ("Log file", self.log_file_value),
            ("Last error", self.last_error_value),
        ]
        self.technical_details_toggle = QToolButton(details_card)
        self.technical_details_toggle.setText("Technical details")
        self.technical_details_toggle.setCheckable(True)
        self.technical_details_toggle.setObjectName("disclosureButton")
        self.technical_details_toggle.setChecked(False)
        self.technical_details_toggle.setArrowType(Qt.RightArrow)
        self.technical_details_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        details_layout.addWidget(self.technical_details_toggle, alignment=Qt.AlignLeft)

        self.technical_details_widget = QWidget(details_card)
        technical_grid = QGridLayout(self.technical_details_widget)
        technical_grid.setContentsMargins(8, 0, 8, 4)
        technical_grid.setHorizontalSpacing(12)
        technical_grid.setVerticalSpacing(4)
        for offset, (title, value) in enumerate(path_rows):
            label = QLabel(title, details_card)
            label.setObjectName("fieldLabel")
            technical_grid.addWidget(label, offset, 0)
            technical_grid.addWidget(value, offset, 1)
        technical_grid.setColumnStretch(1, 1)
        self.technical_details_widget.setVisible(False)
        details_layout.addWidget(self.technical_details_widget)

        detail_actions = QHBoxLayout()
        self.detail_start_button = QPushButton("Start", details_card)
        self.detail_start_button.setObjectName("primaryAction")
        self.detail_stop_button = QPushButton("Stop", details_card)
        self.detail_force_stop_button = QPushButton("Force Stop", details_card)
        self.detail_force_stop_button.setStyleSheet(
            "QPushButton { border-color: #8f3f3f; color: #ffb4a8; }"
            "QPushButton:hover { background-color: #3a2424; }"
            "QPushButton:disabled { color: #7b8794; border-color: #202830; background-color: #151b20; }"
        )
        self.detail_restart_button = QPushButton("Restart", details_card)
        detail_actions.addWidget(self.detail_start_button)
        detail_actions.addWidget(self.detail_stop_button)
        detail_actions.addWidget(self.detail_force_stop_button)
        detail_actions.addWidget(self.detail_restart_button)
        detail_actions.addStretch(1)
        details_layout.addLayout(detail_actions)

        self.console_input_toggle = QToolButton(details_card)
        self.console_input_toggle.setText("Console input")
        self.console_input_toggle.setCheckable(True)
        self.console_input_toggle.setObjectName("disclosureButton")
        self.console_input_toggle.setChecked(False)
        self.console_input_toggle.setArrowType(Qt.RightArrow)
        self.console_input_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        details_layout.addWidget(self.console_input_toggle, alignment=Qt.AlignLeft)

        self.input_row_widget = QWidget(details_card)
        input_row = QHBoxLayout(self.input_row_widget)
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)
        self.input_line = QLineEdit(self.input_row_widget)
        self.input_line.setClearButtonEnabled(True)
        self.send_input_button = QPushButton("Send Input", self.input_row_widget)
        input_row.addWidget(self.input_line)
        input_row.addWidget(self.send_input_button)
        self.input_row_widget.setVisible(False)
        details_layout.addWidget(self.input_row_widget)

        self.input_hint = QLabel("", details_card)
        self.input_hint.setWordWrap(True)
        self.input_hint.setStyleSheet("color: #8b98a5; font-size: 12px;")
        self.input_hint.setVisible(False)
        details_layout.addWidget(self.input_hint)

        self.detail_log_splitter = QSplitter(Qt.Vertical, right_panel)
        self.detail_log_splitter.setChildrenCollapsible(False)
        self.detail_log_splitter.addWidget(details_card)

        log_panel = QWidget(self.detail_log_splitter)
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_actions = QHBoxLayout()
        log_title = QLabel("Logs", log_panel)
        log_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.clear_log_button = QPushButton("Clear", log_panel)
        self.copy_log_button = QPushButton("Copy", log_panel)
        self.open_log_button = QPushButton("Open Log", log_panel)
        log_actions.addWidget(log_title)
        log_actions.addStretch(1)
        log_actions.addWidget(self.clear_log_button)
        log_actions.addWidget(self.copy_log_button)
        log_actions.addWidget(self.open_log_button)
        log_layout.addLayout(log_actions)

        self.log_view = QPlainTextEdit(log_panel)
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Logs appear here.")
        self.log_view.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.log_view.setFont(QFont("Consolas", 9))
        self.log_view.document().setMaximumBlockCount(UI_LOG_BLOCK_LIMIT)
        log_layout.addWidget(self.log_view, stretch=1)
        self.detail_log_splitter.addWidget(log_panel)
        self.detail_log_splitter.setSizes([270, 420])
        self.detail_log_splitter.setStretchFactor(0, 0)
        self.detail_log_splitter.setStretchFactor(1, 1)
        right_layout.addWidget(self.detail_log_splitter, stretch=1)

        self.splitter.setSizes([430, 850])
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)

        self.overlay = BusyOverlay(central)
        self.overlay.setGeometry(self.centralWidget().rect())

    def _connect_signals(self) -> None:
        self.table.selectionModel().selectionChanged.connect(self._handle_selection_changed)

        self.add_action.triggered.connect(self._add_app)
        self.edit_action.triggered.connect(self._edit_selected)
        self.delete_action.triggered.connect(self._delete_selected)
        self.start_all_action.triggered.connect(self.manager.start_all)
        self.stop_all_action.triggered.connect(self.manager.stop_all)
        self.start_with_windows_action.toggled.connect(self._set_start_with_windows)
        self.app_filter.textChanged.connect(self._apply_app_filter)
        self.proxy_model.rowsInserted.connect(self._update_app_count)
        self.proxy_model.rowsRemoved.connect(self._update_app_count)
        self.proxy_model.modelReset.connect(self._update_app_count)
        self.technical_details_toggle.toggled.connect(self._toggle_technical_details)
        self.console_input_toggle.toggled.connect(self._toggle_console_input)
        self.detail_start_button.clicked.connect(self._start_selected)
        self.detail_stop_button.clicked.connect(self._stop_selected)
        self.detail_force_stop_button.clicked.connect(self._force_stop_selected)
        self.detail_restart_button.clicked.connect(self._restart_selected)
        self.send_input_button.clicked.connect(self._send_selected_input)
        self.input_line.returnPressed.connect(self._send_selected_input)
        self.clear_log_button.clicked.connect(self._clear_selected_log_view)
        self.copy_log_button.clicked.connect(self._copy_selected_log_view)
        self.open_log_button.clicked.connect(self._open_selected_log_file)

        self.manager.state_changed.connect(self._apply_snapshot)
        self.manager.batch_state_changed.connect(self._set_batch_state)
        self.manager.error_occurred.connect(self._show_error)
        self.manager.registry_changed.connect(self._replace_snapshots)
        self.log_view.verticalScrollBar().valueChanged.connect(self._track_log_scroll)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.overlay.setGeometry(self.centralWidget().rect())
        if hasattr(self, "no_matches_label"):
            self.no_matches_label.setGeometry(self.table.viewport().rect())

    def closeEvent(self, event: Any) -> None:
        try:
            self._save_ui_state()
            self.manager.shutdown()
        finally:
            super().closeEvent(event)

    def _handle_selection_changed(self, *_: Any) -> None:
        indexes = self.table.selectionModel().selectedRows()
        source_index = self.proxy_model.mapToSource(indexes[0]) if indexes else QModelIndex()
        self.selected_app_id = self.model.app_id_at(source_index.row()) if source_index.isValid() else None
        self._refresh_details_panel()
        self._reload_selected_log_view()
        self._update_actions()

    def _apply_app_filter(self, text: str) -> None:
        self.proxy_model.setFilterFixedString(text.strip())
        self._update_app_count()

    def _update_app_count(self, *_: Any) -> None:
        total = self.model.rowCount()
        visible = self.proxy_model.rowCount()
        noun = "app" if total == 1 else "apps"
        self.app_count_label.setText(f"{visible} of {total} {noun}" if visible != total else f"{total} {noun}")
        show_empty = bool(self.app_filter.text().strip()) and visible == 0
        self.no_matches_label.setGeometry(self.table.viewport().rect())
        self.no_matches_label.setVisible(show_empty)
        if show_empty:
            self.no_matches_label.raise_()

    def _apply_snapshot(self, app_id: str, snapshot: dict[str, Any]) -> None:
        self.model.update_snapshot(app_id, snapshot)
        if self.selected_app_id == app_id:
            if snapshot["status"] == "Starting":
                self._reload_selected_log_view()
            self._refresh_details_panel(snapshot)
        self._update_actions()

    def _replace_snapshots(self, snapshots: list[dict[str, Any]]) -> None:
        desired = self._pending_select_app_id or self.selected_app_id
        self.model.replace_snapshots(snapshots)
        self._pending_select_app_id = None

        if not snapshots:
            self.selected_app_id = None
            self._refresh_details_panel()
            self.log_view.clear()
            self._update_actions()
            return

        if desired and any(snapshot["id"] == desired for snapshot in snapshots):
            self._select_app(desired)
        else:
            self.table.selectRow(0)
        self._update_actions()

    def _select_app(self, app_id: str) -> None:
        for row in range(self.model.rowCount()):
            if self.model.app_id_at(row) == app_id:
                proxy_index = self.proxy_model.mapFromSource(self.model.index(row, 0))
                if proxy_index.isValid():
                    self.table.selectRow(proxy_index.row())
                    self.table.scrollTo(proxy_index)
                break

    def _refresh_details_panel(self, snapshot: dict[str, Any] | None = None) -> None:
        if not self.selected_app_id:
            self._set_text_if_changed(self.summary_label, "Select an app")
            self._style_status_badge("")
            for widget in [
                self.status_value,
                self.pid_value,
                self.cpu_value,
                self.ram_value,
                self.uptime_value,
                self.exit_code_value,
                self.last_error_value,
                self.folder_value,
                self.runner_file_value,
                self.full_command_value,
                self.log_file_value,
            ]:
                self._set_text_if_changed(widget, "N/A")
            self.exit_code_value.setToolTip("No app selected.")
            if self.input_line.text():
                self.input_line.clear()
            self._set_text_if_changed(self.input_hint, "")
            self._set_text_if_changed(self.input_line, "")
            return

        if snapshot is None:
            snapshot = self.manager.snapshot(self.selected_app_id)

        self._set_text_if_changed(self.summary_label, snapshot["name"])
        self._set_text_if_changed(self.status_value, snapshot["status"])
        self._style_status_badge(snapshot["status"])
        self._set_text_if_changed(self.pid_value, str(snapshot["pid"] or "N/A"))
        self._set_text_if_changed(
            self.cpu_value,
            "N/A" if snapshot["cpu_percent"] is None else f"{snapshot['cpu_percent']:.1f}%",
        )
        self._set_text_if_changed(
            self.ram_value,
            "N/A" if snapshot["ram_mb"] is None else f"{snapshot['ram_mb']:.1f} MB",
        )
        self._set_text_if_changed(self.uptime_value, format_uptime(snapshot["uptime_seconds"]))
        exit_code = snapshot.get("last_exit_code")
        if snapshot.get("status") == "Stopped by Runner":
            exit_result = "Intentional stop"
            exit_tooltip = "Runner intentionally stopped this process."
            if exit_code is not None:
                exit_tooltip += f" OS exit code: {exit_code}."
        elif exit_code is None:
            exit_result = "N/A"
            exit_tooltip = "No exit result is available yet."
        elif exit_code == 0:
            exit_result = "Clean exit (0)"
            exit_tooltip = "The process exited normally with code 0."
        else:
            exit_result = f"Crashed ({exit_code})"
            exit_tooltip = f"The process exited unexpectedly with code {exit_code}."
        self._set_text_if_changed(self.exit_code_value, exit_result)
        self.exit_code_value.setToolTip(exit_tooltip)
        self._set_text_if_changed(self.last_error_value, snapshot.get("last_error") or "N/A")
        self._set_text_if_changed(self.folder_value, snapshot.get("cwd") or "N/A")
        self._set_text_if_changed(self.runner_file_value, snapshot.get("runner_path") or "N/A")
        self._set_text_if_changed(self.full_command_value, snapshot.get("full_command") or "N/A")
        self._set_text_if_changed(self.log_file_value, snapshot.get("log_file_path") or "N/A")
        self._set_text_if_changed(self.input_hint, self._input_hint_for_snapshot(snapshot))

    def _reload_selected_log_view(self) -> None:
        if not self.selected_app_id:
            self.log_view.clear()
            return
        self.manager.drain_pending_log_lines(self.selected_app_id)
        self.log_view.setPlainText(self.manager.get_log_cache_text(self.selected_app_id))
        self._refresh_log_highlights()
        self._log_autoscroll = True
        self._scroll_log_to_bottom()
        self._update_log_buttons()

    def _flush_selected_logs(self) -> None:
        if not self.selected_app_id:
            return
        lines = self.manager.drain_pending_log_lines(self.selected_app_id)
        if not lines:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText("".join(lines))
        self._refresh_log_highlights()
        if self._log_autoscroll:
            self._scroll_log_to_bottom()
        self._update_log_buttons()

    def _scroll_log_to_bottom(self) -> None:
        scroll_bar = self.log_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _track_log_scroll(self, value: int) -> None:
        scroll_bar = self.log_view.verticalScrollBar()
        self._log_autoscroll = value >= max(0, scroll_bar.maximum() - 6)

    def _refresh_log_highlights(self) -> None:
        selections: list[QTextEdit.ExtraSelection] = []
        block = self.log_view.document().firstBlock()
        while block.isValid():
            if block.text().lstrip().startswith("[stderr]"):
                selection = QTextEdit.ExtraSelection()
                selection.cursor = QTextCursor(block)
                selection.cursor.select(QTextCursor.LineUnderCursor)
                selection.format = QTextCharFormat()
                selection.format.setForeground(QColor("#ff9189"))
                selection.format.setBackground(QColor("#2c1b1d"))
                selections.append(selection)
            block = block.next()
        self.log_view.setExtraSelections(selections)

    def _clear_selected_log_view(self) -> None:
        if not self.selected_app_id:
            self.statusBar().showMessage("Select an app before clearing logs.", 3500)
            return
        if not self.manager.has_live_log_output(self.selected_app_id):
            self.statusBar().showMessage("There is no recent live output to clear.", 3500)
            return
        self.manager.clear_log_cache(self.selected_app_id)
        self.log_view.setPlainText("Recent live output cleared. The log file on disk was not deleted.\n")
        self._update_log_buttons()

    def _copy_selected_log_view(self) -> None:
        if not self.selected_app_id or not self.manager.has_live_log_output(self.selected_app_id):
            self.statusBar().showMessage("There is no recent live output to copy.", 3500)
            return
        QApplication.clipboard().setText(self.log_view.toPlainText())
        self.statusBar().showMessage("Recent log output copied.", 2500)

    def _open_selected_log_file(self) -> None:
        snapshot = self._selected_snapshot()
        if not snapshot:
            self.statusBar().showMessage("Select an app before opening its log.", 3500)
            return
        log_file = str(snapshot.get("log_file_path") or "")
        if not log_file or not Path(log_file).is_file():
            self.statusBar().showMessage("No log file is available yet for this app.", 4000)
            return
        try:
            os.startfile(log_file)  # type: ignore[attr-defined]
        except OSError as exc:
            self.statusBar().showMessage(f"Could not open log file: {exc}", 5000)

    def _set_batch_state(self, active: bool, message: str) -> None:
        self.batch_active = active
        if active:
            self.overlay.show_message(message)
        else:
            self.overlay.hide()
        self._update_actions()

    def _show_error(self, app_id: str, message: str) -> None:
        if app_id and app_id == self.selected_app_id:
            self._refresh_details_panel()
        QMessageBox.warning(self, "Runner", message)

    def _selected_snapshot(self) -> dict[str, Any] | None:
        if not self.selected_app_id:
            return None
        return self.manager.snapshot(self.selected_app_id)

    def _update_actions(self) -> None:
        snapshot = self._selected_snapshot()
        has_selection = snapshot is not None
        pending = bool(snapshot and snapshot["pending_action"])
        status = snapshot["status"] if snapshot else ""
        running = bool(snapshot and status in {"Running", "Already Running", "Waiting Input"})
        force_stoppable = bool(snapshot and status in {"Starting", "Stopping", "Running", "Already Running", "Waiting Input"})
        disabled = self.batch_active or pending
        can_send_input = bool(
            snapshot
            and snapshot.get("can_accept_input")
            and not snapshot.get("visible_console")
            and not disabled
            and not snapshot.get("pending_action")
            and snapshot["status"] in {"Starting", "Running", "Waiting Input"}
        )
        action_state = (
            not self.batch_active,
            has_selection and not disabled and not running,
            has_selection and not disabled and not running,
            not self.batch_active,
            not self.batch_active,
            has_selection and not disabled and not running,
            has_selection and not disabled and running,
            has_selection and not self.batch_active and force_stoppable,
            has_selection and not disabled,
            can_send_input,
        )
        edit_tooltip = "Stop the app before editing." if has_selection and running else "Edit the selected app."
        delete_tooltip = "Stop the app before deleting." if has_selection and running else "Delete the selected app."
        self.edit_action.setToolTip(edit_tooltip)
        self.delete_action.setToolTip(delete_tooltip)
        self.add_action.setToolTip("Wait for the current bulk action to finish." if self.batch_active else "Add an app.")
        self.start_all_action.setToolTip("Wait for the current bulk action to finish." if self.batch_active else "Start every app that is currently stopped.")
        self.stop_all_action.setToolTip("Wait for the current bulk action to finish." if self.batch_active else "Stop every app that is currently running.")
        if not has_selection:
            start_tip = stop_tip = force_tip = restart_tip = "Select an app first."
        elif pending or self.batch_active:
            start_tip = stop_tip = force_tip = restart_tip = "Wait for the current action to finish."
        else:
            start_tip = "The selected app is already running." if running else "Start the selected app."
            stop_tip = "Stop the selected app." if running else "The selected app is not running."
            force_tip = "Force-stop the selected process tree." if force_stoppable else "There is no active process to force-stop."
            restart_tip = "Restart the selected app."
        self.detail_start_button.setToolTip(start_tip)
        self.detail_stop_button.setToolTip(stop_tip)
        self.detail_force_stop_button.setToolTip(force_tip)
        self.detail_restart_button.setToolTip(restart_tip)
        self.console_input_toggle.setToolTip("Show console input controls." if has_selection else "Select an app to use console input.")
        if action_state == self._last_action_state:
            self._refresh_input_controls(snapshot, can_send_input)
            return

        self._last_action_state = action_state
        self.add_action.setEnabled(action_state[0])
        self.edit_action.setEnabled(action_state[1])
        self.delete_action.setEnabled(action_state[2])
        self.start_all_action.setEnabled(action_state[3])
        self.stop_all_action.setEnabled(action_state[4])
        self.detail_start_button.setEnabled(action_state[5])
        self.detail_stop_button.setEnabled(action_state[6])
        self.detail_force_stop_button.setEnabled(action_state[7])
        self.detail_restart_button.setEnabled(action_state[8])
        self._update_log_buttons(has_selection)
        self._refresh_input_controls(snapshot, can_send_input)

    def _update_log_buttons(self, has_selection: bool | None = None) -> None:
        if has_selection is None:
            has_selection = self.selected_app_id is not None
        has_live_output = bool(has_selection and self.selected_app_id and self.manager.has_live_log_output(self.selected_app_id))
        snapshot = self._selected_snapshot() if has_selection else None
        log_path = Path(str(snapshot.get("log_file_path") or "")) if snapshot else None
        try:
            has_log_file = bool(log_path and log_path.is_file() and log_path.stat().st_size > 0)
        except OSError:
            has_log_file = False
        self.clear_log_button.setEnabled(has_live_output)
        self.copy_log_button.setEnabled(has_live_output)
        self.open_log_button.setEnabled(has_log_file)
        self.clear_log_button.setToolTip("Clear recent live output (the disk log is kept)." if has_live_output else "No recent live output to clear.")
        self.copy_log_button.setToolTip("Copy recent live output." if has_live_output else "No recent live output to copy.")
        self.open_log_button.setToolTip("Open the complete log file." if has_log_file else "No log file is available yet.")

    def _add_app(self) -> None:
        dialog = AppEditorDialog(self)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            app_data = dialog.app_data()
            validated = self._validated_app_data(app_data)
            validated["id"] = self._make_app_id(validated["name"])
            self._pending_select_app_id = validated["id"]
            snapshot = self.manager.add_app(validated)
            self._persist_apps()
        except Exception as exc:
            self._pending_select_app_id = None
            QMessageBox.warning(self, "Runner", str(exc))

    def _edit_selected(self) -> None:
        snapshot = self._selected_snapshot()
        if not snapshot:
            return
        if snapshot["status"] in {"Running", "Already Running", "Waiting Input"} or snapshot["pending_action"]:
            QMessageBox.warning(self, "Runner", "Stop the app before editing it.")
            return

        dialog = AppEditorDialog(self, initial=snapshot)
        if dialog.exec() != QDialog.Accepted:
            return
        try:
            app_data = dialog.app_data()
            validated = self._validated_app_data(app_data)
            self._pending_select_app_id = snapshot["id"]
            updated = self.manager.update_app(snapshot["id"], validated)
            self._persist_apps()
        except Exception as exc:
            self._pending_select_app_id = None
            QMessageBox.warning(self, "Runner", str(exc))

    def _delete_selected(self) -> None:
        snapshot = self._selected_snapshot()
        if not snapshot:
            return
        if snapshot["status"] in {"Running", "Already Running", "Waiting Input"} or snapshot["pending_action"]:
            QMessageBox.warning(self, "Runner", "Running apps cannot be deleted.")
            return
        answer = QMessageBox.question(
            self,
            "Delete App",
            f"Delete '{snapshot['name']}' from Runner?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self.manager.remove_app(snapshot["id"])
            self._persist_apps()
            self.selected_app_id = None
        except Exception as exc:
            QMessageBox.warning(self, "Runner", str(exc))

    def _persist_apps(self) -> None:
        save_apps_file(self.config_path, self.manager.export_apps())

    def _validated_app_data(self, app_data: dict[str, Any]) -> dict[str, Any]:
        name = app_data["name"].strip()
        cwd = str(Path(app_data["cwd"]).expanduser()) if app_data["cwd"] else ""
        runner_path = str(Path(app_data["runner_path"]).expanduser()) if app_data["runner_path"] else ""

        if not name:
            raise ValueError("App name is required.")
        if not cwd:
            raise ValueError("Folder is required.")
        if not runner_path:
            raise ValueError("Runner file is required.")
        if app_data.get("visible_console") and str(app_data.get("startup_input") or "").strip():
            raise ValueError("Visible Console cannot be combined with Startup Input. Use one input mode.")

        folder = Path(cwd)
        runner_file = Path(runner_path)
        if not folder.is_dir():
            raise ValueError("Selected folder does not exist.")
        if not runner_file.is_file():
            raise ValueError("Selected runner file does not exist.")
        if runner_file.suffix.lower() not in RUNNER_FILE_SUFFIXES:
            raise ValueError("Runner file must be a .exe, .py, .js, .mjs, .bat, or .cmd file.")

        return {
            "name": name,
            "cwd": str(folder.resolve()),
            "runner_path": str(runner_file.resolve()),
            "args": list(app_data["args"]),
            "startup_input": app_data["startup_input"],
            "interactive": bool(app_data.get("interactive", False)),
            "visible_console": bool(app_data.get("visible_console", False)),
            "auto_start": bool(app_data.get("auto_start", False)),
            "env": {},
        }

    def _make_app_id(self, name: str) -> str:
        base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "app"
        existing = {app["id"] for app in self.manager.export_apps()}
        candidate = base
        suffix = 2
        while candidate in existing:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _start_selected(self) -> None:
        if self.selected_app_id:
            self.manager.start_app(self.selected_app_id)

    def _stop_selected(self) -> None:
        if self.selected_app_id:
            self.manager.stop_app(self.selected_app_id)

    def _force_stop_selected(self) -> None:
        if self.selected_app_id:
            self.manager.force_stop_app(self.selected_app_id)

    def _restart_selected(self) -> None:
        if self.selected_app_id:
            self.manager.restart_app(self.selected_app_id)

    def _send_selected_input(self) -> None:
        if not self.selected_app_id:
            return
        text = self.input_line.text()
        self.manager.send_input(self.selected_app_id, text)
        self.input_line.clear()

    def _tick_selected_details(self) -> None:
        if not self.selected_app_id:
            return
        snapshot = self.manager.snapshot(self.selected_app_id)
        if snapshot["status"] in {"Starting", "Stopping", "Running", "Already Running", "Waiting Input"}:
            self._refresh_details_panel(snapshot)

    def _refresh_input_controls(self, snapshot: dict[str, Any] | None, can_send_input: bool) -> None:
        self.console_input_toggle.setEnabled(snapshot is not None)
        self.input_row_widget.setVisible(self.console_input_toggle.isChecked() and snapshot is not None)
        self.input_hint.setVisible(self.console_input_toggle.isChecked() and bool(self.input_hint.text()))
        self.send_input_button.setEnabled(can_send_input)
        self.input_line.setEnabled(can_send_input)
        placeholder = "Send input to selected app."
        tooltip = ""
        if not snapshot:
            placeholder = "Select an app to send input."
        elif snapshot.get("visible_console"):
            placeholder = "Visible Console mode is active. Type in that window."
            tooltip = "Runner does not own stdin while Visible Console mode is active."
        elif snapshot["status"] == "Already Running":
            placeholder = "External instance detected. Runner cannot send stdin to it."
            tooltip = "Stop the external instance and start it from Runner to use Send Input."
        elif snapshot["status"] == "Waiting Input":
            placeholder = "Type input for this prompt. Leave blank to send Enter."
        elif snapshot.get("can_accept_input"):
            placeholder = "Type menu input and press Enter."
        elif snapshot["status"] in {"Starting", "Stopping"}:
            placeholder = f"Send Input is unavailable while the app is {snapshot['status'].lower()}."
        else:
            placeholder = "Start this app from Runner to send input."
        self.input_line.setPlaceholderText(placeholder)
        self.input_line.setToolTip(tooltip)
        self.send_input_button.setToolTip(tooltip or placeholder)

    def _toggle_technical_details(self, expanded: bool) -> None:
        self.technical_details_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.technical_details_widget.setVisible(expanded)
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)

    def _toggle_console_input(self, expanded: bool) -> None:
        self.console_input_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.input_row_widget.setVisible(expanded and self.selected_app_id is not None)
        self.input_hint.setVisible(expanded and bool(self.input_hint.text()))
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)

    def _rebalance_detail_log_splitter(self) -> None:
        total = sum(self.detail_log_splitter.sizes())
        if total <= 0:
            return
        desired_details = min(max(self.details_card.sizeHint().height(), 210), max(210, total - 180))
        self.detail_log_splitter.setSizes([desired_details, max(180, total - desired_details)])

    @staticmethod
    def _set_text_if_changed(widget: Any, text: str) -> None:
        if isinstance(widget, ElidedLabel):
            if widget.fullText() != text:
                widget.setFullText(text)
            return
        if widget.text() != text:
            widget.setText(text)

    def _style_status_badge(self, status: str) -> None:
        colors = {
            "Running": ("#163c2a", "#75d69c"),
            "Already Running": ("#173556", "#8fc5ff"),
            "Starting": ("#493814", "#ffd479"),
            "Waiting Input": ("#493814", "#ffd479"),
            "Stopping": ("#493814", "#ffd479"),
            "Crashed": ("#4a2020", "#ffaaa0"),
        }
        background, foreground = colors.get(status, ("#303a43", "#d7dee5"))
        self.status_value.setStyleSheet(
            f"background-color: {background}; color: {foreground}; border-radius: 5px; padding: 2px 7px;"
        )

    @staticmethod
    def _detail_value_label(parent: QWidget) -> QLabel:
        label = QLabel("N/A", parent)
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        return label

    @staticmethod
    def _input_hint_for_snapshot(snapshot: dict[str, Any]) -> str:
        if not snapshot.get("interactive") and not snapshot.get("can_accept_input"):
            return ""
        if snapshot.get("visible_console"):
            return "Visible Console mode is active. Type in that window. Runner input and live pipe logs stay disabled."
        if snapshot["status"] == "Waiting Input":
            return "Prompt detected. Use Send Input or leave the field blank to send Enter."
        if snapshot.get("can_accept_input"):
            return "Send menu answers or command input to this app from Runner."
        if snapshot["status"] == "Already Running":
            return "A matching process was detected outside Runner. Send Input is disabled because Runner did not start its stdin pipe."
        if snapshot["status"] in {"Starting", "Stopping"}:
            return f"Interactive app is {snapshot['status'].lower()}. Send Input will re-enable if a prompt appears."
        return "Interactive app is doing background work. Send Input stays disabled until Runner detects the next prompt."

    def start_configured_apps(self) -> None:
        self.manager.start_auto_start_apps()

    def _set_start_with_windows(self, enabled: bool) -> None:
        try:
            if enabled:
                self._write_startup_command()
                message = (
                    "Runner will open automatically when this Windows user logs in.\n\n"
                    "Only apps checked with 'Start when Runner opens' will be started."
                )
            else:
                self._startup_command_path().unlink(missing_ok=True)
                message = "Runner will no longer open automatically when Windows starts."
        except OSError as exc:
            self.start_with_windows_action.blockSignals(True)
            self.start_with_windows_action.setChecked(not enabled)
            self.start_with_windows_action.blockSignals(False)
            self._refresh_start_with_windows_action()
            self.statusBar().showMessage(f"Could not update Windows startup: {exc}", 6000)
            return
        self._refresh_start_with_windows_action()
        self.statusBar().showMessage(" ".join(message.split()), 5000)

    def _refresh_start_with_windows_action(self) -> None:
        enabled = self.start_with_windows_action.isChecked()
        label = "Start with Windows: ON" if enabled else "Start with Windows: OFF"
        self.start_with_windows_action.setText(label)
        self.start_with_windows_action.setToolTip(
            "When ON, Windows opens Runner after login. Runner then starts apps checked with 'Start when Runner opens'."
        )

    def _startup_enabled(self) -> bool:
        return self._startup_command_path().exists()

    def _startup_command_path(self) -> Path:
        appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        return appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_CMD_NAME

    def _write_startup_command(self) -> None:
        path = self._startup_command_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        command_parts, workdir = self._runner_start_command()
        quoted = " ".join(subprocess.list2cmdline([part]) for part in command_parts)
        content = f'@echo off\ncd /d "{workdir}"\nstart "" {quoted}\n'
        write_text_atomic(path, content)

    def _runner_start_command(self) -> tuple[list[str], Path]:
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
            return [str(exe)], exe.parent

        project_root = Path(__file__).resolve().parents[1]
        script = project_root / "launcher" / "run.py"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python_exe = pythonw if pythonw.exists() else Path(sys.executable)
        return [str(python_exe), str(script)], project_root

    def _restore_ui_state(self) -> None:
        try:
            state = json.loads(self.ui_state_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            return
        geometry = state.get("geometry")
        if isinstance(geometry, dict):
            width = int(geometry.get("width", self.width()))
            height = int(geometry.get("height", self.height()))
            x = geometry.get("x")
            y = geometry.get("y")
            self.resize(width, height)
            if x is not None and y is not None:
                self.move(int(x), int(y))
        splitter_sizes = state.get("splitter_sizes")
        if isinstance(splitter_sizes, list) and splitter_sizes:
            self.splitter.setSizes([int(size) for size in splitter_sizes])
        detail_log_sizes = state.get("detail_log_splitter_sizes")
        if isinstance(detail_log_sizes, list) and detail_log_sizes:
            self.detail_log_splitter.setSizes([int(size) for size in detail_log_sizes])

    def _save_ui_state(self) -> None:
        state = {
            "geometry": {
                "x": self.x(),
                "y": self.y(),
                "width": self.width(),
                "height": self.height(),
            },
            "splitter_sizes": self.splitter.sizes(),
            "detail_log_splitter_sizes": self.detail_log_splitter.sizes(),
        }
        try:
            write_text_atomic(self.ui_state_path, json.dumps(state, indent=2))
        except OSError:
            pass
