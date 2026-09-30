from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from cluster.updates import ReleaseClient, SemanticVersion, UpdateError, UpdateSettings, backup_runtime, update_plan
from cluster.version import AGENT_VERSION, BUILD_NUMBER, RUNNER_VERSION
from cluster.models import PROTOCOL_VERSION
from ui.background import BackgroundTaskPool, timed_ui_operation

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QObject, QRectF, QSize, QSortFilterProxyModel, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QIcon, QPainter, QPen, QPixmap, QTextCharFormat, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QAbstractButton,
    QApplication,
    QCheckBox,
    QComboBox,
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QHeaderView,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTableView,
    QTextEdit,
    QToolBar,
    QToolButton,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)


UI_LOG_BLOCK_LIMIT = 300
RUNNER_FILE_SUFFIXES = {".exe", ".py", ".js", ".mjs", ".bat", ".cmd", ".sh", ".bash"}
STARTUP_CMD_NAME = "Runner Auto Start.cmd"
STARTUP_TASK_NAME = "Runner Auto Start"
ANSI_CONTROL_SEQUENCE = re.compile(r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-_])")


def clean_log_display_text(value: str) -> str:
    """Remove terminal control sequences from displayed text, not saved logs."""
    return ANSI_CONTROL_SEQUENCE.sub("", str(value))


def configure_log_wrapping(editor: QPlainTextEdit, enabled: bool) -> None:
    if enabled:
        editor.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        editor.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    else:
        editor.setLineWrapMode(QPlainTextEdit.NoWrap)
        editor.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
STATUS_COLORS = {
    "Running": "#55c982",
    "Already Running": "#69aaf2",
    "Starting": "#e8b85c",
    "Waiting Input": "#e8b85c",
    "Stopping": "#e8b85c",
    "Crashed": "#f07970",
    "Stopped": "#8996a1",
    "Stopped by Runner": "#8996a1",
    "Degraded": "#e8b85c",
}


class ClusterOverviewCard(QFrame):
    """Compact native-Qt cluster summary; it only renders existing state."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("clusterOverview")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 7, 10, 7)
        layout.setSpacing(5)
        title_row = QHBoxLayout()
        title = QLabel("CLUSTER OVERVIEW", self)
        title.setObjectName("overviewTitle")
        self.summary = QLabel("", self)
        self.summary.setObjectName("overviewSummary")
        self.summary.setVisible(False)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self.summary)
        layout.addLayout(title_row)
        self.tiles_grid = QGridLayout()
        self.tiles_grid.setContentsMargins(0, 0, 0, 0)
        self.tiles_grid.setHorizontalSpacing(8)
        self.tiles_grid.setVerticalSpacing(5)
        self.tiles: dict[str, tuple[QFrame, QLabel, QLabel, QLabel]] = {}
        for key, caption in (("primary", "PRIMARY"), ("backup", "BACKUP"), ("sync", "SYNC"), ("failover", "FAILOVER")):
            tile = QFrame(self)
            tile.setObjectName("overviewTile")
            tile_layout = QVBoxLayout(tile)
            tile_layout.setContentsMargins(9, 6, 9, 6)
            tile_layout.setSpacing(2)
            heading = QLabel(caption, tile)
            heading.setObjectName("overviewHeading")
            value = QLabel("—", tile)
            value.setObjectName("overviewValue")
            detail = QLabel("", tile)
            detail.setObjectName("overviewDetail")
            detail.setWordWrap(True)
            tile_layout.addWidget(heading)
            tile_layout.addWidget(value)
            tile_layout.addWidget(detail)
            self.tiles[key] = (tile, heading, value, detail)
        layout.addLayout(self.tiles_grid)
        self.actions_widget = QWidget(self)
        self.actions = QHBoxLayout(self.actions_widget)
        self.actions.setContentsMargins(0, 0, 0, 0)
        self.actions.setSpacing(6)
        self.actions.addStretch(1)
        self.servers_button = QPushButton("Servers", self)
        self.setup_button = QPushButton("Finish HA Setup", self)
        self.details_button = QPushButton("Cluster Details", self)
        self.actions.addWidget(self.servers_button)
        self.actions.addWidget(self.setup_button)
        self.actions.addWidget(self.details_button)
        layout.addWidget(self.actions_widget)
        self.set_compact(False, 4)

    def set_tile(self, key: str, value: str, detail: str, tone: str) -> None:
        tile, _heading, value_label, detail_label = self.tiles[key]
        tile.setProperty("tone", tone)
        tile.style().unpolish(tile)
        tile.style().polish(tile)
        value_label.setText(value)
        detail_label.setText(detail)

    def set_overview(self, summary: str, values: dict[str, tuple[str, str, str]], *, needs_setup: bool) -> None:
        self.summary.setText(summary)
        for key, item in values.items():
            self.set_tile(key, *item)
        self.setup_button.setVisible(needs_setup)
        if self.summary.isVisible():
            self._refresh_compact_summary()

    def set_compact(self, compact: bool, columns: int = 4) -> None:
        self.summary.setVisible(compact)
        self.actions_widget.setVisible(not compact)
        for tile, _heading, _value, _detail in self.tiles.values():
            self.tiles_grid.removeWidget(tile)
            tile.setVisible(not compact)
        if compact:
            self._refresh_compact_summary()
            return
        columns = max(1, min(4, columns))
        for index, (tile, _heading, _value, _detail) in enumerate(self.tiles.values()):
            self.tiles_grid.addWidget(tile, index // columns, index % columns)
            tile.setVisible(True)

    def _refresh_compact_summary(self) -> None:
        parts = []
        for key, label in (("primary", "Primary"), ("backup", "Backup"), ("sync", "Sync"), ("failover", "Failover")):
            _tile, _heading, value, _detail = self.tiles[key]
            parts.append(f"{label}: {value.text()}")
        self.summary.setText("   ·   ".join(parts))


class AppRowDelegate(QStyledItemDelegate):
    """Paint one selection accent per row instead of one per selected cell."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        super().paint(painter, option, index)
        if index.column() != 0 or not (option.state & QStyle.StateFlag.State_Selected):
            return
        painter.save()
        painter.fillRect(option.rect.adjusted(0, 1, -option.rect.width() + 3, -1), QColor("#58a6e7"))
        painter.restore()
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
            if index.column() == 2 and record.get("status") == "Already Running":
                return "External process: already running outside Runner."
            if index.column() == 2:
                return str(record.get("status") or "Unknown")
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
                return {
                    "Already Running": "External",
                    "Waiting Input": "Input",
                    "Stopped by Runner": "Stopped",
                }.get(record["status"], record["status"])
        if role == Qt.TextAlignmentRole and index.column() > 0:
            return int(Qt.AlignCenter)
        return None

    def app_id_at(self, row: int) -> str | None:
        if row < 0 or row >= len(self._rows):
            return None
        return self._rows[row]

    def snapshot_for_app(self, app_id: str) -> dict[str, Any] | None:
        record = self._records.get(app_id)
        return dict(record) if record is not None else None

    def snapshots(self) -> list[dict[str, Any]]:
        return [dict(self._records[app_id]) for app_id in self._rows]

    def update_snapshot(self, app_id: str, snapshot: dict[str, Any]) -> None:
        if app_id not in self._records:
            return
        previous = self._records[app_id]
        self._records[app_id] = snapshot
        row = self._row_lookup[app_id]
        keys = ["name", "runner_path", "status", "app_type"]
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
    def __init__(self, parent: QWidget | None = None, initial: dict[str, Any] | None = None, workers: BackgroundTaskPool | None = None) -> None:
        super().__init__(parent)
        self.workers = workers or BackgroundTaskPool(Path.cwd() / "logs", self)
        self.setWindowTitle("App Editor" if initial else "Add App")
        self.resize(620, 330)
        initial = initial or {}
        self.initial = initial

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(10)
        intro = QLabel("Add a local application for Runner to manage.", self)
        intro.setObjectName("appSubtitle")
        layout.addWidget(intro)

        form = QFormLayout()
        self._basic_form = form
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(9)
        layout.addLayout(form)

        self.name_input = QLineEdit(initial.get("name", ""), self)
        self.name_input.setPlaceholderText("e.g. Website or AI Bridge")
        form.addRow("Name", self.name_input)

        self.folder_input = QLineEdit(initial.get("cwd", ""), self)
        self.folder_input.setPlaceholderText("Choose the project folder")
        self.folder_input.editingFinished.connect(self._autofill_from_folder)
        folder_row = QHBoxLayout()
        folder_row.addWidget(self.folder_input)
        folder_button = QPushButton("Browse", self)
        folder_button.clicked.connect(self._browse_folder)
        folder_row.addWidget(folder_button)
        form.addRow("Project folder", self._wrap_row(folder_row))

        self.runner_input = QLineEdit(initial.get("runner_path", ""), self)
        self.runner_input.editingFinished.connect(self._autofill_name_from_runner)
        runner_row = QHBoxLayout()
        runner_row.addWidget(self.runner_input)
        runner_button = QPushButton("Browse", self)
        runner_button.clicked.connect(self._browse_runner)
        runner_row.addWidget(runner_button)
        self.runner_row_widget = self._wrap_row(runner_row)
        form.addRow("Entry file", self.runner_row_widget)

        self.auto_start_input = QCheckBox("Start this app when Runner opens", self)
        self.auto_start_input.setChecked(bool(initial.get("auto_start", False)))
        form.addRow("Startup", self.auto_start_input)

        self.advanced_toggle = QToolButton(self)
        self.advanced_toggle.setObjectName("disclosureButton")
        self.advanced_toggle.setText("▸  Advanced settings")
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.advanced_toggle)

        self.advanced_scroll = QScrollArea(self)
        self.advanced_scroll.setWidgetResizable(True)
        self.advanced_scroll.setFrameShape(QFrame.NoFrame)
        self.advanced_widget = QWidget(self.advanced_scroll)
        advanced_form = QFormLayout(self.advanced_widget)
        advanced_form.setContentsMargins(8, 4, 8, 4)
        advanced_form.setHorizontalSpacing(14)
        advanced_form.setVerticalSpacing(8)
        self.advanced_scroll.setWidget(self.advanced_widget)
        layout.addWidget(self.advanced_scroll, stretch=1)

        args_text = subprocess.list2cmdline(initial.get("args", [])) if initial.get("args") else ""
        self.args_input = QLineEdit(args_text, self)
        self.args_input.setPlaceholderText("Optional command-line arguments")
        advanced_form.addRow("Arguments", self.args_input)

        self.startup_input = QPlainTextEdit(self)
        self.startup_input.setPlainText(initial.get("startup_input", ""))
        self.startup_input.setPlaceholderText("Optional startup inputs. Use one line per answer.")
        self.startup_input.setFixedHeight(78)
        self.startup_input.textChanged.connect(self._sync_interactive_from_startup)
        advanced_form.addRow("Startup input", self.startup_input)

        self.interactive_input = QCheckBox("Requires stdin / menu input", self)
        self.interactive_input.setChecked(bool(initial.get("interactive", False)))
        advanced_form.addRow("Interactive", self.interactive_input)

        self.visible_console_input = QCheckBox("Open visible console window", self)
        self.visible_console_input.setChecked(bool(initial.get("visible_console", False)))
        self.visible_console_input.toggled.connect(self._sync_interactive_from_console)
        advanced_form.addRow("Visible console", self.visible_console_input)

        self.protected_input = QCheckBox("Require cluster ownership lease", self)
        self.protected_input.setChecked(bool(initial.get("protected", False)))
        advanced_form.addRow("Cluster protection", self.protected_input)

        self.dependencies_input = QLineEdit(", ".join(initial.get("dependencies", [])), self)
        self.dependencies_input.setPlaceholderText("Application IDs, separated by commas")
        advanced_form.addRow("Dependencies", self.dependencies_input)

        self.health_type_input = QComboBox(self)
        self.health_type_input.addItems(["process", "tcp", "http", "https"])
        health = dict(initial.get("health_check") or {"type": "process"})
        self.health_type_input.setCurrentText(str(health.get("type", "process")))
        advanced_form.addRow("Health check", self.health_type_input)

        self.health_target_input = QLineEdit(str(health.get("url") or health.get("port") or ""), self)
        self.health_target_input.setPlaceholderText("URL for HTTP(S), or TCP port")
        advanced_form.addRow("Health target", self.health_target_input)

        self.persistence_input = QComboBox(self)
        self.persistence_input.addItems(["stateless", "external", "replicated", "sqlite", "custom", "unsupported"])
        self.persistence_input.setCurrentText(str(initial.get("persistence", {}).get("strategy", "stateless")))
        advanced_form.addRow("Persistence", self.persistence_input)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.advanced_scroll.setVisible(False)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)

    def app_data(self) -> dict[str, Any]:
        args_text = self.args_input.text().strip()
        args = shlex.split(args_text, posix=False) if args_text else []
        health_type = self.health_type_input.currentText()
        target = self.health_target_input.text().strip()
        health_check: dict[str, Any] = {"type": health_type, "timeout_seconds": 5}
        if health_type == "tcp" and target:
            health_check.update({"host": "127.0.0.1", "port": int(target)})
        elif health_type in {"http", "https"} and target:
            health_check.update({"url": target, "expected_status": 200})
        return {
            "name": self.name_input.text().strip(),
            "cwd": self.folder_input.text().strip(),
            "runner_path": self.runner_input.text().strip(),
            "args": args,
            "startup_input": self.startup_input.toPlainText(),
            "interactive": self.interactive_input.isChecked() or self.visible_console_input.isChecked(),
            "visible_console": self.visible_console_input.isChecked(),
            "auto_start": self.auto_start_input.isChecked(),
            "protected": self.protected_input.isChecked(),
            "service_group": str(self.initial.get("service_group") or self.initial.get("id") or ""),
            "dependencies": [value.strip() for value in self.dependencies_input.text().split(",") if value.strip()],
            "health_check": health_check,
            "persistence": {"strategy": self.persistence_input.currentText(), "paths": list(self.initial.get("persistence", {}).get("paths", []))},
            "sync": dict(self.initial.get("sync") or {"enabled": False, "include": [], "exclude": [], "status": "not_configured"}),
            "deployments": dict(self.initial.get("deployments") or {}),
            "env": dict(self.initial.get("env") or {}),
            "app_type": "process",
        }

    def _toggle_advanced(self, expanded: bool) -> None:
        self.advanced_scroll.setVisible(expanded)
        self.advanced_toggle.setText("▾  Advanced settings" if expanded else "▸  Advanced settings")
        available_height = self.screen().availableGeometry().height() if self.screen() else 800
        self.resize(self.width(), min(700, available_height - 72) if expanded else 330)

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
            "Runner Files (*.exe *.py *.js *.mjs *.bat *.cmd *.sh *.bash);;All Files (*)",
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
        if self.runner_input.text().strip():
            return
        def detect() -> str:
            return detect_runner_file(folder) or ""
        def apply(value: Any, error: BaseException | None) -> None:
            if error or self.folder_input.text().strip() != folder:
                return
            if value and not self.runner_input.text().strip():
                self.runner_input.setText(str(value))
                self._autofill_name_from_runner()
        self.workers.submit(f"app-folder-detect-{id(self)}", detect, apply)

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


class PairingDialog(QDialog):
    completed = Signal(bool, str)

    def __init__(self, manager, parent=None, *, setup_mode: bool = False, workers: BackgroundTaskPool | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.workers = workers or BackgroundTaskPool(Path.cwd() / "logs", self)
        self.setup_mode = setup_mode
        self._connection_test_passed = False
        self.setWindowTitle("Finish HA Setup" if setup_mode else "Cluster / Servers")
        self.resize(680, 520 if setup_mode else 560)
        layout = QVBoxLayout(self)
        if setup_mode:
            intro = QLabel(
                "Connect a backup server and verify the secure connection. Runner will show what is still needed before failover can be enabled.",
                self,
            )
            intro.setWordWrap(True)
            intro.setObjectName("appSubtitle")
            layout.addWidget(intro)
            self.setup_progress = QLabel(self)
            self.setup_progress.setObjectName("setupProgress")
            self.setup_progress.setWordWrap(True)
            self.setup_progress.setStyleSheet(
                "QLabel#setupProgress { background:#171e25; border:1px solid #303b46; "
                "border-radius:7px; padding:10px 12px; line-height:1.45; }"
            )
            layout.addWidget(self.setup_progress)
        else:
            self.setup_progress = None
        self.summary = QPlainTextEdit(self)
        self.summary.setReadOnly(True)
        self.summary.setVisible(not setup_mode)
        layout.addWidget(self.summary)
        options = QHBoxLayout()
        self.failover = QCheckBox("Automatic failover", self)
        self.failback = QCheckBox("Automatic failback", self)
        options.addWidget(self.failover)
        options.addWidget(self.failback)
        options.addStretch(1)
        layout.addLayout(options)
        form = QFormLayout()
        self.endpoint = QLineEdit(self)
        self.endpoint.setPlaceholderText("Select a detected computer below")
        self.detected_peers = QComboBox(self)
        self.detected_peers.addItem("Choose a detected Runner computer…", "")
        self.code = QLineEdit(self)
        self.code.setPlaceholderText("000-000-000")
        self.witness_url = QLineEdit(self)
        self.witness_url.setPlaceholderText("Advanced: https://witness-name.tailnet.ts.net")
        self.witness_secret = QLineEdit(self)
        self.witness_secret.setEchoMode(QLineEdit.Password)
        self.witness_secret.setPlaceholderText("Advanced self-hosted witness setup key")
        form.addRow("Advanced Agent address", self.endpoint)
        form.addRow("Backup computer" if setup_mode else "Detected computers", self.detected_peers)
        form.addRow("One-time pairing code", self.code)
        form.addRow("Witness address", self.witness_url)
        form.addRow("Witness setup key", self.witness_secret)
        layout.addLayout(form)
        self.form = form
        self.setup_advanced_toggle = QToolButton(self)
        self.setup_advanced_toggle.setObjectName("disclosureButton")
        self.setup_advanced_toggle.setText("▸  Advanced connection and witness settings")
        self.setup_advanced_toggle.setCheckable(True)
        self.setup_advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.setup_advanced_toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        layout.addWidget(self.setup_advanced_toggle)
        buttons = QHBoxLayout()
        self.offer_button = QPushButton("Generate pairing code", self)
        self.pair_button = QPushButton("Pair with selected server" if setup_mode else "Pair", self)
        self.test_button = QPushButton("Test Connection", self)
        self.repair_button = QPushButton("Repair Agent & Firewall", self)
        self.witness_button = QPushButton("Connect Witness", self)
        self.remove_button = QPushButton("Remove selected/entered Node ID", self)
        close = QPushButton("Close", self)
        buttons.addWidget(self.offer_button)
        buttons.addWidget(self.pair_button)
        buttons.addWidget(self.test_button)
        buttons.addWidget(self.repair_button)
        buttons.addWidget(self.witness_button)
        buttons.addWidget(self.remove_button)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.offer_button.clicked.connect(self._offer)
        self.pair_button.clicked.connect(self._pair)
        self.test_button.clicked.connect(self._test_connection)
        self.repair_button.clicked.connect(self._repair_agent)
        self.witness_button.clicked.connect(self._configure_witness)
        self.detected_peers.currentIndexChanged.connect(self._select_detected_peer)
        self.remove_button.clicked.connect(self._remove)
        close.clicked.connect(self.accept)
        self.completed.connect(self._completed)
        self.failover.toggled.connect(lambda value: self._set_cluster_option("automatic_failover", value))
        self.failback.toggled.connect(lambda value: self._set_cluster_option("automatic_failback", value))
        self.setup_advanced_toggle.toggled.connect(self._toggle_setup_advanced)
        if setup_mode:
            self.failover.setVisible(False)
            self.failback.setVisible(False)
            self.repair_button.setVisible(False)
            self.remove_button.setVisible(False)
            self.witness_button.setVisible(False)
            self._set_setup_advanced_visible(False)
            pairing_hint = QLabel(
                "Pairing uses a one-time code: generate it on the server you want to trust, then select that computer here and enter the code.",
                self,
            )
            pairing_hint.setObjectName("appSubtitle")
            pairing_hint.setWordWrap(True)
            layout.insertWidget(layout.indexOf(self.setup_advanced_toggle), pairing_hint)
        else:
            self.setup_advanced_toggle.setVisible(False)
            self._set_setup_advanced_visible(True)
        self._refresh()
        self._load_detected_peers()

    def _load_detected_peers(self) -> None:
        self.detected_peers.setEnabled(False)
        self.detected_peers.setItemText(0, "Searching for Runner computers…")
        def find_peers() -> list[Any]:
            from cluster.tailscale import available_peers
            return available_peers()
        def apply(peers: Any, error: BaseException | None) -> None:
            self.detected_peers.setEnabled(True)
            self.detected_peers.clear()
            self.detected_peers.addItem("No Runner computers found" if error or not peers else "Choose a detected Runner computer…", "")
            for peer in peers or []:
                if peer.online and peer.endpoint_host:
                    self.detected_peers.addItem(peer.hostname, f"https://{peer.endpoint_host}:47473")
        self.workers.submit("tailscale-peer-discovery", find_peers, apply)

    def _select_detected_peer(self, index: int) -> None:
        endpoint = self.detected_peers.itemData(index)
        if endpoint:
            self.endpoint.setText(str(endpoint))

    def _refresh(self) -> None:
        status = self.manager.agent_status()
        self.failover.blockSignals(True)
        self.failback.blockSignals(True)
        self.failover.setChecked(bool(status.get("automatic_failover", False)))
        self.failback.setChecked(bool(status.get("automatic_failback", False)))
        self.failover.setEnabled(bool(status.get("automatic_failover_eligible", False)))
        self.failover.setToolTip("; ".join(status.get("automatic_failover_reasons", [])) or "All prerequisites are satisfied")
        self.failback.setEnabled(bool(status.get("automatic_failover_eligible", False)))
        self.failback.blockSignals(False)
        self.failover.blockSignals(False)
        lines = [
            f"Cluster: {status.get('cluster_name', 'Production')}",
            "Runner Agent: Running",
            f"This computer: {status.get('node_name') or status.get('node_id', 'unknown')}",
            f"Witness: {'Connected' if status.get('automatic_failover_eligible') or status.get('leases') else 'Needs setup'}",
            "",
        ]
        peers = status.get("peers", {})
        if not peers:
            lines.append("No backup server is paired.")
        for node_id, peer in peers.items():
            lines.extend([
                f"{peer.get('name') or node_id}",
                f"  Node ID: {node_id}",
                f"  OS / Runner: {peer.get('os_name', 'unknown')} / {peer.get('agent_version', 'unknown')}",
                f"  Connection: {str(peer.get('connection', 'unknown')).upper()}",
                f"  Latency: {peer.get('latency_ms', 'n/a')} ms",
                f"  Last heartbeat age: {peer.get('last_heartbeat_age', 'n/a')} s",
                f"  Sync: {peer.get('sync_status', 'unknown')}",
                "",
            ])
        self.summary.setPlainText("\n".join(lines))
        if self.setup_mode and self.setup_progress is not None:
            connected_peers = [peer for peer in peers.values() if peer.get("connection") == "connected"]
            backup_text = "Connected" if connected_peers else ("Paired · reconnecting" if peers else "Choose and pair a backup server")
            is_cluster = str(status.get("mode", "standalone")).lower() == "cluster"
            reasons = status.get("automatic_failover_reasons", [])
            witness_missing = (not is_cluster) or any("witness" in str(reason).lower() for reason in reasons)
            witness_text = "Needs setup" if witness_missing else "Connected"
            protection = "Ready" if is_cluster and status.get("automatic_failover_eligible") else "Needs attention"
            self.setup_progress.setText(
                f"BACKUP SERVER     {backup_text}\n"
                f"FAILOVER WITNESS  {witness_text}\n"
                f"SECURE CONNECTION {'Passed' if self._connection_test_passed else ('Connected' if connected_peers else 'Not tested')}\n\n"
                f"Protection: {protection}"
            )

    def _set_cluster_option(self, key: str, value: bool) -> None:
        def apply(result: Any, error: BaseException | None) -> None:
            if error or not result.get("success", False):
                QMessageBox.warning(self, "Cluster configuration", str(error or result.get("reason", "Cluster configuration was rejected")))
            self._refresh()
        self.workers.submit(f"cluster-config-{key}", lambda: self.manager.update_cluster({key: value}), apply)

    def _offer(self) -> None:
        self.offer_button.setEnabled(False)
        def apply(offer: Any, error: BaseException | None) -> None:
            self.offer_button.setEnabled(True)
            if error:
                QMessageBox.warning(self, "Pairing", str(error))
                return
            self.code.setText(str(offer["code"]))
            QMessageBox.information(self, "Pairing code", f"Enter this one-time code on the other Runner:\n\n{offer['code']}\n\nIt expires in five minutes.")
        self.workers.submit("pairing-offer", self.manager.create_pairing_offer, apply)

    def _pair(self) -> None:
        endpoint, code = self.endpoint.text().strip(), self.code.text().strip()
        if not endpoint or not code:
            QMessageBox.warning(self, "Pairing", "Enter the peer's Tailscale HTTPS address and pairing code.")
            return
        self.pair_button.setEnabled(False)
        self.workers.submit("pairing-connect", lambda: self.manager.pair_remote(endpoint, code), self._pair_result)

    def _pair_result(self, node: Any, error: BaseException | None) -> None:
        self._completed(not error, str(error or f"Connected to {node.get('name') or node.get('node_id')}"))

    def _pair_worker(self, endpoint: str, code: str) -> None:
        try:
            node = self.manager.pair_remote(endpoint, code)
            self.completed.emit(True, f"Connected to {node.get('name') or node.get('node_id')}")
        except Exception as exc:
            self.completed.emit(False, str(exc))

    def _remove(self) -> None:
        node_id = self.endpoint.text().strip()
        if not node_id:
            QMessageBox.warning(self, "Remove server", "Enter the exact Node ID in the address field.")
            return
        if QMessageBox.question(self, "Remove trusted server", "Remove this trusted peer? Ownership will not be changed.") != QMessageBox.Yes:
            return
        self.workers.submit("pairing-remove", lambda: self.manager.remove_peer(node_id), self._result_message)

    def _remove_worker(self, node_id: str) -> None:
        try:
            result = self.manager.remove_peer(node_id)
            self.completed.emit(bool(result.get("success", True)), str(result.get("reason", "Peer removed")))
        except Exception as exc:
            self.completed.emit(False, str(exc))

    def _configure_witness(self) -> None:
        if not self.witness_url.text().strip() or not self.witness_secret.text().strip():
            QMessageBox.information(self, "Witness", "Use this advanced option only for a self-hosted witness. Enter its HTTPS address and the one-time setup key generated by the witness host.")
            return
        self.witness_button.setEnabled(False)
        url, secret = self.witness_url.text().strip(), self.witness_secret.text().strip()
        self.workers.submit("witness-configure", lambda: self.manager.configure_witness(url, secret), self._result_message)

    def _result_message(self, result: Any, error: BaseException | None) -> None:
        self._completed(not error and bool(result.get("success", True)), str(error or result.get("reason", "Completed")))

    def _witness_worker(self, url: str, secret: str) -> None:
        try:
            result = self.manager.configure_witness(url, secret)
            self.completed.emit(bool(result.get("success")), str(result.get("reason", "Witness configured")))
        except Exception as exc:
            self.completed.emit(False, str(exc))

    def _completed(self, success: bool, message: str) -> None:
        self.pair_button.setEnabled(True)
        self.witness_button.setEnabled(True)
        (QMessageBox.information if success else QMessageBox.warning)(self, "Pairing", message)
        self._refresh()

    def _test_connection(self) -> None:
        status = self.manager.agent_status()
        peers = status.get("peers", {})
        connected = [node for node, peer in peers.items() if peer.get("connection") == "connected"]
        if connected:
            self._connection_test_passed = True
            QMessageBox.information(self, "Connection test", "Secure Agent authentication and heartbeat are working with: " + ", ".join(connected))
        elif peers:
            self._connection_test_passed = False
            QMessageBox.warning(self, "Connection test", "Runner can see paired servers, but no authenticated heartbeat is currently healthy. Check Tailscale and retry.")
        else:
            self._connection_test_passed = False
            QMessageBox.information(self, "Connection test", "Runner Agent is working locally. Pair a backup server to test the secure remote connection.")
        self._refresh()

    def _set_row_visible(self, field: QWidget, visible: bool) -> None:
        field.setVisible(visible)
        label = self.form.labelForField(field)
        if label:
            label.setVisible(visible)

    def _toggle_setup_advanced(self, expanded: bool) -> None:
        self.setup_advanced_toggle.setText(
            "▾  Advanced connection and witness settings" if expanded
            else "▸  Advanced connection and witness settings"
        )
        self._set_setup_advanced_visible(expanded)

    def _set_setup_advanced_visible(self, visible: bool) -> None:
        self._set_row_visible(self.endpoint, visible)
        self._set_row_visible(self.witness_url, visible)
        self._set_row_visible(self.witness_secret, visible)
        self.witness_button.setVisible(visible)

    def _repair_agent(self) -> None:
        if os.name != "nt":
            QMessageBox.information(self, "Repair", "On Linux, repair the Runner Agent from your system service settings.")
            return
        root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        script = root / "tools" / "repair-runner.ps1" if getattr(sys, "frozen", False) else root / "build" / "scripts" / "repair-runner.ps1"
        def apply(_value: Any, error: BaseException | None) -> None:
            if error:
                QMessageBox.warning(self, "Repair", str(error))
            else:
                QMessageBox.information(self, "Repair", "Windows will ask for approval, then Runner Agent and Tailscale firewall access will be repaired.")
        def repair() -> None:
            if not script.is_file():
                raise FileNotFoundError("Runner repair files are missing. Reinstall Runner.")
            os.startfile("powershell.exe", "runas", f'-NoProfile -ExecutionPolicy Bypass -File "{script}"')
        self.workers.submit("repair-agent", repair, apply)


class UpdateNotifier(QObject):
    available = Signal(str)


class UpdateDialog(QDialog):
    """Non-blocking release check UI; download/install only after verification."""
    checked = Signal(object, str)
    settings_loaded = Signal(object, str)

    def __init__(self, manager: Any, runtime_root: Path, parent: QWidget | None = None, workers: BackgroundTaskPool | None = None) -> None:
        super().__init__(parent)
        self.manager = manager
        self.workers = workers or BackgroundTaskPool(runtime_root / "logs", self)
        self.runtime_root = runtime_root
        self.settings = UpdateSettings(runtime_root)
        self.release = None
        self.setWindowTitle("Runner Updates")
        self.resize(600, 410)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.addRow("Installed version", QLabel(RUNNER_VERSION))
        self.latest = QLabel("Not checked")
        form.addRow("Latest version", self.latest)
        self.manifest_url = QLineEdit()
        self.manifest_url.setPlaceholderText("https://releases.example.com/runner/manifest.json")
        self.manifest_url.setToolTip("HTTPS signed release manifest. Leave blank until your release service is configured.")
        form.addRow("Release manifest", self.manifest_url)
        self.auto_check = QCheckBox("Automatically check for updates")
        self.auto_download = QCheckBox("Download updates automatically")
        form.addRow("", self.auto_check)
        form.addRow("", self.auto_download)
        layout.addLayout(form)
        layout.addWidget(QLabel("Release Notes"))
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setPlaceholderText("Configure a signed HTTPS release manifest, then select Check for Updates.")
        layout.addWidget(self.notes)
        self.message = QLabel("Online updates are not configured for this installation.")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QHBoxLayout()
        self.check_button = QPushButton("Check for Updates")
        self.download_button = QPushButton("Download Update")
        self.update_button = QPushButton("Update Now")
        self.download_button.setEnabled(False)
        self.update_button.setEnabled(False)
        buttons.addWidget(self.check_button)
        buttons.addWidget(self.download_button)
        buttons.addWidget(self.update_button)
        layout.addLayout(buttons)
        self.check_button.clicked.connect(self.check)
        self.download_button.clicked.connect(self.download)
        self.update_button.clicked.connect(self.apply_update)
        self.checked.connect(self._checked)
        self.settings_loaded.connect(self._apply_settings)
        self.workers.submit(
            "update-settings-load",
            self.settings.load,
            lambda values, error: self.settings_loaded.emit(values or {}, str(error) if error else ""),
        )

    def _apply_settings(self, values: object, error: str) -> None:
        if error:
            self.message.setText(f"Update settings could not be loaded: {error}")
            return
        data = dict(values) if isinstance(values, dict) else {}
        self.manifest_url.setText(str(data.get("manifest_url") or ""))
        self.auto_check.setChecked(bool(data.get("automatically_check", False)))
        self.auto_download.setChecked(bool(data.get("download_automatically", False)))

    def _save_settings(self) -> None:
        values = self._settings_values()
        self.workers.submit("update-settings-save", lambda: self.settings.save(values), lambda *_: None)

    def _settings_values(self) -> dict[str, Any]:
        return {
            "manifest_url": self.manifest_url.text().strip(),
            "automatically_check": self.auto_check.isChecked(),
            "download_automatically": self.auto_download.isChecked(),
        }

    def check(self) -> None:
        url = self.manifest_url.text().strip()
        settings_values = self._settings_values()
        if not url:
            self.workers.submit("update-settings-save", lambda: self.settings.save(settings_values), lambda *_: None)
            self.message.setText("No release source is configured. Online update checks remain disabled.")
            return
        self.check_button.setEnabled(False)
        self.message.setText("Checking signed release manifest…")
        def check_release() -> Any:
            self.settings.save(settings_values)
            return self._fetch_release(url)
        self.workers.submit("update-check", check_release, lambda release, error: self.checked.emit(release, str(error) if error else ""))

    @staticmethod
    def _fetch_release(url: str) -> Any:
        key = os.environ.get("RUNNER_RELEASE_PUBLIC_KEY", "")
        return ReleaseClient(key).fetch(url)

    def _check_worker(self, url: str) -> None:
        try:
            # A distributor supplies this public key with its signed build; an
            # environment override is useful for private, controlled releases.
            key = os.environ.get("RUNNER_RELEASE_PUBLIC_KEY", "")
            release = ReleaseClient(key).fetch(url)
            self.checked.emit(release, "")
        except Exception as exc:
            self.checked.emit(None, str(exc))

    def _checked(self, release: object, error: str) -> None:
        self.check_button.setEnabled(True)
        if error == "downloaded" and isinstance(release, Path):
            self._set_download(release)
            return
        if error:
            self.message.setText(error)
            return
        self.release = release
        self.latest.setText(release.version)
        self.notes.setPlainText(release.notes or "No release notes were supplied.")
        available = SemanticVersion.parse(release.version) > SemanticVersion.parse(RUNNER_VERSION)
        self.message.setText("Update available. Verification is required before install." if available else "Runner is up to date.")
        self.download_button.setEnabled(available)

    def download(self) -> None:
        if not self.release:
            return
        platform_key = "windows" if os.name == "nt" else "linux"
        artifact = self.release.artifacts.get(platform_key)
        if not artifact:
            self.message.setText(f"This release has no {platform_key} package.")
            return
        self.download_button.setEnabled(False)
        self.message.setText("Downloading and verifying update…")
        version = self.release.version
        def download() -> Path:
            suffix = ".exe" if os.name == "nt" else ".pkg"
            target = self.settings.path.parent / "updates" / f"Runner-{version}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            ReleaseClient(os.environ.get("RUNNER_RELEASE_PUBLIC_KEY", "")).download(artifact, target)
            return target
        self.workers.submit("update-download", download, lambda target, error: self.checked.emit(target, "downloaded") if not error else self.checked.emit(None, str(error)))

    def _download_worker(self, artifact: dict[str, str]) -> None:
        try:
            suffix = ".exe" if os.name == "nt" else ".pkg"
            target = self.runtime_root / "updates" / f"Runner-{self.release.version}{suffix}"
            target.parent.mkdir(parents=True, exist_ok=True)
            ReleaseClient(os.environ.get("RUNNER_RELEASE_PUBLIC_KEY", "")).download(artifact, target)
            self.checked.emit(target, "downloaded")
        except Exception as exc:
            self.checked.emit(None, str(exc))

    def apply_update(self) -> None:
        package = getattr(self, "package", None)
        if not package:
            self.message.setText("Download and verification must complete before updating.")
            return
        allowed, reason = update_plan(self.manager.agent_status())
        if not allowed:
            QMessageBox.warning(self, "Safe rolling update required", reason)
            return
        if QMessageBox.question(self, "Install verified update", "Runner will back up its local configuration, install the verified package, and restart its Agent. Continue?") != QMessageBox.Yes:
            return
        install_dir = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parents[2]
        updater = install_dir / "UpdateRunner.exe"
        self.update_button.setEnabled(False)
        self.message.setText("Backing up configuration before update…")
        def launch_update() -> None:
            if not updater.is_file():
                raise FileNotFoundError("Installed update helper is missing. Reinstall Runner.")
            backup_runtime(self.runtime_root)
            subprocess.Popen([str(updater), "--package", str(package), "--install-dir", str(install_dir), "--runtime-root", str(self.runtime_root)], cwd=str(install_dir))
        def ready(_value: Any, error: BaseException | None) -> None:
            if error:
                self.update_button.setEnabled(True)
                self.message.setText(f"Update could not start: {error}")
                return
            QApplication.quit()
        self.workers.submit("apply-verified-update", launch_update, ready)

    def _set_download(self, package: Path) -> None:
        self.package = package
        self.download_button.setEnabled(False)
        self.update_button.setEnabled(True)
        self.message.setText("Download verified. Ready to install safely.")


class MainWindow(QMainWindow):
    agent_logs_ready = Signal(str, str, str)

    def __init__(self, manager: Any, config_path: str | Path) -> None:
        super().__init__()
        self.manager = manager
        self.config_path = Path(config_path)
        self.workers = BackgroundTaskPool(self.config_path.parent / "logs", self, workers=2, capacity=12)
        self.ui_state_path = self.config_path.parent / "ui_state.json"
        self.selected_app_id: str | None = None
        self.batch_active = False
        self._pending_select_app_id: str | None = None
        self._last_action_state: tuple[bool, ...] | None = None
        self._log_autoscroll = True
        self._agent_log_fetch_lock = threading.Lock()
        self._agent_log_fetch_inflight: set[str] = set()
        self._last_agent_log_text: dict[str, str] = {}
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
        if getattr(self.manager, "remote_managed", False):
            self._agent_connection_changed(
                str(getattr(self.manager, "connection_state", "starting")),
                str(getattr(self.manager, "connection_message", "Runner Agent starting")),
            )
        self._restore_ui_state()
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)
        QTimer.singleShot(0, self._update_app_count)

        self.log_timer = QTimer(self)
        # Log output is appended only when pending lines exist.  A modest
        # interval keeps high-volume consoles responsive without waking the
        # GUI five times per second while it is idle.
        self.log_timer.setInterval(500)
        self.log_timer.timeout.connect(self._flush_selected_logs)
        self.log_timer.start()

        self.detail_timer = QTimer(self)
        self.detail_timer.setInterval(1000)
        self.detail_timer.timeout.connect(self._tick_selected_details)
        self.detail_timer.start()

        self.cluster_timer = QTimer(self)
        self.cluster_timer.setInterval(2000)
        self.cluster_timer.timeout.connect(self._refresh_cluster_banner)
        self.cluster_timer.start()
        self.agent_log_timer = QTimer(self)
        self.agent_log_timer.setInterval(1800)
        self.agent_log_timer.timeout.connect(self._poll_selected_agent_logs)
        self.agent_log_timer.start()
        self._refresh_cluster_banner()
        self._update_notifier = UpdateNotifier(self)
        self._update_notifier.available.connect(self._show_update_notice)
        QTimer.singleShot(2500, self._check_updates_in_background)

        if self.model.rowCount() > 0:
            self.table.selectRow(0)

    def _check_updates_in_background(self) -> None:
        def worker() -> None:
            try:
                settings = UpdateSettings(self.config_path.parent).load()
                if not settings.get("automatically_check") or not settings.get("manifest_url"):
                    return
                release = ReleaseClient(os.environ.get("RUNNER_RELEASE_PUBLIC_KEY", "")).fetch(str(settings["manifest_url"]))
                if SemanticVersion.parse(release.version) > SemanticVersion.parse(RUNNER_VERSION):
                    if settings.get("download_automatically"):
                        platform_key = "windows" if os.name == "nt" else "linux"
                        artifact = release.artifacts.get(platform_key)
                        if artifact:
                            target = self.config_path.parent / "updates" / f"Runner-{release.version}{'.exe' if os.name == 'nt' else '.pkg'}"
                            target.parent.mkdir(parents=True, exist_ok=True)
                            ReleaseClient().download(artifact, target)
                            self._update_notifier.available.emit(f"{release.version} (downloaded and verified)")
                            return
                    self._update_notifier.available.emit(release.version)
            except Exception:
                # A silent background check must never distract from normal work.
                pass
        self.workers.submit("automatic-update-check", worker, lambda _result, _error: None)

    def _show_update_notice(self, version: str) -> None:
        self.statusBar().showMessage(f"Update available: Runner {version}. Open Updates to review release notes.", 15000)

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
        self.cluster_action = QAction("Cluster Status", self)
        self.servers_action = QAction("Servers", self)
        self.updates_action = QAction("Updates", self)
        self.about_action = QAction("About", self)
        self.start_with_windows_action = StartupSwitch(toolbar)
        self.start_with_windows_action.setChecked(False)
        self.start_with_windows_action.setObjectName("startupToggle")
        self._refresh_start_with_windows_action()
        QTimer.singleShot(0, self._query_startup_state)

        toolbar.addAction(self.add_action)
        toolbar.addAction(self.start_all_action)
        toolbar.addAction(self.stop_all_action)
        self.more_menu = QMenu("More", toolbar)
        for action in (
            self.edit_action, self.delete_action, self.servers_action,
            self.cluster_action, self.updates_action, self.about_action,
        ):
            self.more_menu.addAction(action)
        self.startup_menu_action = self.more_menu.addAction("Start with Windows")
        self.startup_menu_action.setCheckable(True)
        self.startup_menu_action.setChecked(self.start_with_windows_action.isChecked())
        self.startup_menu_action.toggled.connect(self._set_start_with_windows)
        self.more_button = QToolButton(toolbar)
        self.more_button.setText("More")
        self.more_button.setPopupMode(QToolButton.InstantPopup)
        self.more_button.setMenu(self.more_menu)
        self.more_button.setToolTip("Less frequently used Runner actions")
        toolbar.addWidget(self.more_button)
        self.app_filter = QLineEdit(toolbar)
        self.app_filter.setPlaceholderText("Search apps…")
        self.app_filter.setClearButtonEnabled(True)
        self.app_filter.setMinimumWidth(135)
        self.app_filter.setMaximumWidth(280)
        self.app_filter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.app_filter.setToolTip("Filter by app name, type, or status")
        toolbar.addWidget(self.app_filter)
        self.app_count_label = QLabel("0 apps", toolbar)
        self.app_count_label.setObjectName("appCountLabel")
        toolbar.addWidget(self.app_count_label)
        toolbar.addWidget(self.start_with_windows_action)
        add_button = toolbar.widgetForAction(self.add_action)
        if add_button:
            add_button.setObjectName("toolbarPrimary")
        self.setMinimumSize(680, 440)

    def _build_layout(self) -> None:
        central = QWidget(self)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        self.cluster_overview = ClusterOverviewCard(central)
        self.cluster_overview.servers_button.clicked.connect(self._show_servers)
        self.cluster_overview.setup_button.clicked.connect(lambda: self._show_servers(setup_mode=True))
        self.cluster_overview.details_button.clicked.connect(self._show_cluster_status)
        layout.addWidget(self.cluster_overview)

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
        header.setMinimumSectionSize(42)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.Fixed)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 78)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setMinimumWidth(280)
        self.table.setItemDelegate(AppRowDelegate(self.table))
        self.no_matches_label = QLabel("No matching apps", self.table.viewport())
        self.no_matches_label.setAlignment(Qt.AlignCenter)
        self.no_matches_label.setObjectName("emptySearchLabel")
        self.no_matches_label.hide()

        right_panel = QWidget(self.splitter)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        details_card = QFrame(right_panel)
        details_card.setObjectName("detailsCard")
        self.details_card = details_card
        details_card.setFrameShape(QFrame.StyledPanel)
        details_layout = QVBoxLayout(details_card)
        details_layout.setContentsMargins(14, 12, 14, 12)
        details_layout.setSpacing(8)

        header_row = QHBoxLayout()
        header_row.setSpacing(10)
        self.summary_label = QLabel("Select an app", details_card)
        self.summary_label.setObjectName("appTitle")
        self.app_type_label = QLabel("Choose an application to see its status and logs", details_card)
        self.app_type_label.setObjectName("appSubtitle")
        header_row.addWidget(self.summary_label)
        self.status_value = QLabel("N/A", details_card)
        self.status_value.setObjectName("statusPill")
        header_row.addWidget(self.status_value, alignment=Qt.AlignRight | Qt.AlignVCenter)
        header_row.addStretch(1)
        details_layout.addLayout(header_row)
        details_layout.addWidget(self.app_type_label)

        self.metric_strip = QFrame(details_card)
        self.metric_strip.setObjectName("metricStrip")
        summary_grid = QGridLayout(self.metric_strip)
        summary_grid.setContentsMargins(8, 6, 8, 6)
        summary_grid.setHorizontalSpacing(8)
        summary_grid.setVerticalSpacing(5)
        self.pid_value = QLabel("N/A", details_card)
        self.cpu_value = QLabel("N/A", details_card)
        self.ram_value = QLabel("N/A", details_card)
        self.uptime_value = QLabel("N/A", details_card)
        self.exit_code_value = QLabel("N/A", details_card)
        self.last_error_value = ElidedLabel("N/A", details_card)
        self.folder_value = ElidedLabel("N/A", details_card)
        self.runner_file_value = ElidedLabel("N/A", details_card)
        self.full_command_value = ElidedLabel("N/A", details_card)
        self.log_file_value = ElidedLabel("N/A", details_card)

        metrics = [
            ("PID", self.pid_value),
            ("CPU", self.cpu_value),
            ("RAM", self.ram_value),
            ("Uptime", self.uptime_value),
            ("Exit", self.exit_code_value),
        ]
        for index, (title, value) in enumerate(metrics):
            metric_cell = QWidget(self.metric_strip)
            metric_cell_layout = QHBoxLayout(metric_cell)
            metric_cell_layout.setContentsMargins(4, 1, 4, 1)
            metric_cell_layout.setSpacing(7)
            label = QLabel(title, metric_cell)
            label.setObjectName("metricLabel")
            value.setObjectName("metricValue")
            metric_cell_layout.addWidget(label)
            metric_cell_layout.addStretch(1)
            metric_cell_layout.addWidget(value)
            summary_grid.addWidget(metric_cell, 0, index)
        for column in range(len(metrics)):
            summary_grid.setColumnStretch(column, 1)
        details_layout.addWidget(self.metric_strip)

        path_rows = [
            ("Folder", self.folder_value),
            ("Runner", self.runner_file_value),
            ("Command", self.full_command_value),
            ("Log file", self.log_file_value),
            ("Last error", self.last_error_value),
        ]
        self.technical_details_toggle = QToolButton(details_card)
        self.technical_details_toggle.setText("›  Technical details")
        self.technical_details_toggle.setCheckable(True)
        self.technical_details_toggle.setObjectName("disclosureButton")
        self.technical_details_toggle.setChecked(False)
        self.technical_details_toggle.setArrowType(Qt.NoArrow)
        self.technical_details_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.technical_details_toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
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
        self.detail_restart_button = QPushButton("Restart", details_card)
        self.detail_more_button = QToolButton(details_card)
        self.detail_more_button.setText("More")
        self.detail_more_button.setObjectName("detailMore")
        self.detail_more_button.setPopupMode(QToolButton.InstantPopup)
        self.detail_more_menu = QMenu(self.detail_more_button)
        self.detail_force_stop_action = self.detail_more_menu.addAction("Force Stop…")
        self.detail_force_stop_action.setToolTip("Immediately terminate the selected process tree.")
        self.detail_more_button.setMenu(self.detail_more_menu)
        detail_actions.addWidget(self.detail_start_button)
        detail_actions.addWidget(self.detail_restart_button)
        detail_actions.addWidget(self.detail_stop_button)
        detail_actions.addWidget(self.detail_more_button)
        detail_actions.addStretch(1)
        details_layout.addLayout(detail_actions)

        self.console_input_toggle = QToolButton(details_card)
        self.console_input_toggle.setText("›  Console input")
        self.console_input_toggle.setCheckable(True)
        self.console_input_toggle.setObjectName("disclosureButton")
        self.console_input_toggle.setChecked(False)
        self.console_input_toggle.setArrowType(Qt.NoArrow)
        self.console_input_toggle.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.console_input_toggle.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        details_layout.addWidget(self.console_input_toggle, alignment=Qt.AlignLeft)

        self.input_row_widget = QWidget(details_card)
        input_row = QHBoxLayout(self.input_row_widget)
        input_row.setContentsMargins(0, 0, 0, 0)
        input_row.setSpacing(8)
        self.input_line = QLineEdit(self.input_row_widget)
        self.input_line.setClearButtonEnabled(True)
        self.input_line.setObjectName("consoleInput")
        self.input_line.setToolTip("Clear the pending input text")
        self.send_input_button = QPushButton("Send Input", self.input_row_widget)
        self.send_input_button.setObjectName("compactAction")
        input_row.addWidget(self.input_line)
        input_row.addWidget(self.send_input_button)
        self.input_row_widget.setVisible(False)
        details_layout.addWidget(self.input_row_widget)

        self.input_hint = QLabel("", details_card)
        self.input_hint.setWordWrap(True)
        self.input_hint.setStyleSheet("color: #8b98a5; font-size: 12px; margin-top: 3px;")
        self.input_hint.setVisible(False)
        details_layout.addWidget(self.input_hint)

        self.detail_log_splitter = QSplitter(Qt.Vertical, right_panel)
        self.detail_log_splitter.setChildrenCollapsible(False)
        self.detail_log_splitter.addWidget(details_card)

        log_panel = QFrame(self.detail_log_splitter)
        log_panel.setObjectName("logPanel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(10, 8, 10, 10)
        log_layout.setSpacing(6)
        log_actions = QHBoxLayout()
        log_title = QLabel("Logs", log_panel)
        log_title.setStyleSheet("font-size: 15px; font-weight: 600;")
        self.clear_log_button = QPushButton("Clear", log_panel)
        self.copy_log_button = QPushButton("Copy", log_panel)
        self.log_more_button = QToolButton(log_panel)
        self.log_more_button.setText("More")
        self.log_more_button.setObjectName("logMore")
        self.log_more_menu = QMenu(self.log_more_button)
        self.open_log_action = self.log_more_menu.addAction("Open log file…")
        self.log_more_button.setMenu(self.log_more_menu)
        self.log_more_button.setPopupMode(QToolButton.InstantPopup)
        self.follow_log_button = QToolButton(log_panel)
        self.follow_log_button.setText("Follow")
        self.follow_log_button.setObjectName("followLog")
        self.follow_log_button.setCheckable(True)
        self.follow_log_button.setChecked(True)
        self.wrap_log_button = QToolButton(log_panel)
        self.wrap_log_button.setText("Wrap")
        self.wrap_log_button.setObjectName("wrapLog")
        self.wrap_log_button.setCheckable(True)
        self.wrap_log_button.setChecked(True)
        log_actions.addWidget(log_title)
        log_actions.addStretch(1)
        log_actions.addWidget(self.follow_log_button)
        log_actions.addWidget(self.wrap_log_button)
        log_actions.addWidget(self.copy_log_button)
        log_actions.addWidget(self.clear_log_button)
        log_actions.addWidget(self.log_more_button)
        log_layout.addLayout(log_actions)

        self.log_view = QPlainTextEdit(log_panel)
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("Logs appear here.")
        self.log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self.log_view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.log_view.setFont(QFont("Consolas", 10))
        self.log_view.setViewportMargins(2, 2, 2, 2)
        self.log_view.document().setMaximumBlockCount(UI_LOG_BLOCK_LIMIT)
        log_layout.addWidget(self.log_view, stretch=1)
        self.detail_log_splitter.addWidget(log_panel)
        self.detail_log_splitter.setSizes([240, 500])
        self.detail_log_splitter.setStretchFactor(0, 0)
        self.detail_log_splitter.setStretchFactor(1, 1)
        right_layout.addWidget(self.detail_log_splitter, stretch=1)

        self.splitter.setSizes([420, 800])
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 2)

        self.overlay = BusyOverlay(central)
        self.overlay.setGeometry(self.centralWidget().rect())

    def _connect_signals(self) -> None:
        self.agent_logs_ready.connect(self._apply_agent_logs)
        self.table.selectionModel().selectionChanged.connect(self._handle_selection_changed)

        self.add_action.triggered.connect(self._add_app)
        self.edit_action.triggered.connect(self._edit_selected)
        self.delete_action.triggered.connect(self._delete_selected)
        self.start_all_action.triggered.connect(self.manager.start_all)
        self.stop_all_action.triggered.connect(self.manager.stop_all)
        self.cluster_action.triggered.connect(self._show_cluster_status)
        self.servers_action.triggered.connect(self._show_servers)
        self.updates_action.triggered.connect(self._show_updates)
        self.about_action.triggered.connect(self._show_about)
        self.start_with_windows_action.toggled.connect(self._set_start_with_windows)
        self.app_filter.textChanged.connect(self._apply_app_filter)
        self.proxy_model.rowsInserted.connect(self._update_app_count)
        self.proxy_model.rowsRemoved.connect(self._update_app_count)
        self.proxy_model.modelReset.connect(self._update_app_count)
        self.technical_details_toggle.toggled.connect(self._toggle_technical_details)
        self.console_input_toggle.toggled.connect(self._toggle_console_input)
        self.detail_start_button.clicked.connect(self._start_selected)
        self.detail_stop_button.clicked.connect(self._stop_selected)
        self.detail_force_stop_action.triggered.connect(self._force_stop_selected)
        self.detail_restart_button.clicked.connect(self._restart_selected)
        self.send_input_button.clicked.connect(self._send_selected_input)
        self.input_line.returnPressed.connect(self._send_selected_input)
        self.clear_log_button.clicked.connect(self._clear_selected_log_view)
        self.copy_log_button.clicked.connect(self._copy_selected_log_view)
        self.open_log_action.triggered.connect(self._open_selected_log_file)
        self.follow_log_button.toggled.connect(self._set_log_follow)
        self.wrap_log_button.toggled.connect(self._set_log_wrap)
        self._set_log_wrap(self.wrap_log_button.isChecked())

        self.manager.state_changed.connect(self._apply_snapshot)
        self.manager.batch_state_changed.connect(self._set_batch_state)
        self.manager.error_occurred.connect(self._show_error)
        self.manager.registry_changed.connect(self._replace_snapshots)
        connection_signal = getattr(self.manager, "connection_changed", None)
        if connection_signal is not None:
            connection_signal.connect(self._agent_connection_changed)
        self.log_view.verticalScrollBar().valueChanged.connect(self._track_log_scroll)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.overlay.setGeometry(self.centralWidget().rect())
        if hasattr(self, "no_matches_label"):
            self.no_matches_label.setGeometry(self.table.viewport().rect())
        if hasattr(self, "cluster_overview"):
            width = self.centralWidget().width()
            height = self.centralWidget().height()
            compact = width < 800 or height < 570
            columns = 2 if width < 1120 else 4
            self.cluster_overview.set_compact(compact, columns)
            if hasattr(self, "start_with_windows_action"):
                self.start_with_windows_action.setVisible(width >= 930)
                self.startup_menu_action.setVisible(width < 930)
            if hasattr(self, "app_filter"):
                self.app_filter.setMinimumWidth(110 if width < 900 else 150)

    def closeEvent(self, event: Any) -> None:
        try:
            self._save_ui_state()
            self.workers.shutdown()
        finally:
            super().closeEvent(event)

    @timed_ui_operation
    def _handle_selection_changed(self, *_: Any) -> None:
        indexes = self.table.selectionModel().selectedRows()
        source_index = self.proxy_model.mapToSource(indexes[0]) if indexes else QModelIndex()
        self.selected_app_id = self.model.app_id_at(source_index.row()) if source_index.isValid() else None
        self._refresh_details_panel()
        self._reload_selected_log_view()
        self._update_actions()

    @timed_ui_operation
    def _apply_app_filter(self, text: str) -> None:
        self.proxy_model.setFilterFixedString(text.strip())
        self._update_app_count()

    @timed_ui_operation
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

    @timed_ui_operation
    def _apply_snapshot(self, app_id: str, snapshot: dict[str, Any]) -> None:
        self.model.update_snapshot(app_id, snapshot)
        if self.selected_app_id == app_id:
            if snapshot["status"] == "Starting":
                self._reload_selected_log_view()
            self._refresh_details_panel(snapshot)
        self._update_actions()

    @timed_ui_operation
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

    @timed_ui_operation
    def _refresh_details_panel(self, snapshot: dict[str, Any] | None = None) -> None:
        if not self.selected_app_id:
            self._set_text_if_changed(self.summary_label, "Select an app")
            self._set_text_if_changed(self.app_type_label, "Choose an application to see its status and logs")
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
            snapshot = self._selected_snapshot()

        remote_owner = bool(snapshot.get("protected") and snapshot.get("owner_node_id") and not snapshot.get("local_owner"))
        self.detail_start_button.setText("Transfer Here" if remote_owner else "Start")
        self._set_text_if_changed(self.summary_label, snapshot["name"])
        app_type = str(snapshot.get("app_type") or "process").replace("_", " ").title()
        if snapshot.get("status") == "Already Running":
            type_text = f"{app_type} · External process detected"
        else:
            type_text = app_type
        self._set_text_if_changed(self.app_type_label, type_text)
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
        if getattr(self.manager, "remote_managed", False):
            if not self.log_view.toPlainText():
                self.log_view.setPlainText("Loading recent output…\n")
            self._request_agent_log_refresh(self.selected_app_id)
            self._update_log_buttons()
            return
        self._request_agent_log_refresh(self.selected_app_id)
        self._refresh_log_highlights()
        self._log_autoscroll = self.follow_log_button.isChecked()
        if self._log_autoscroll:
            self._scroll_log_to_bottom()
        self._update_log_buttons()

    def _poll_selected_agent_logs(self) -> None:
        if not getattr(self.manager, "remote_managed", False) or not self.selected_app_id:
            return
        snapshot = self._selected_snapshot()
        if snapshot and snapshot.get("status") in {
            "Starting", "Running", "Already Running", "Waiting Input", "Degraded"
        }:
            self._request_agent_log_refresh(self.selected_app_id)

    def _request_agent_log_refresh(self, app_id: str) -> None:
        def apply(text: Any, error: BaseException | None) -> None:
            self.agent_logs_ready.emit(app_id, str(text or ""), str(error) if error else "")
        self.workers.submit(f"selected-log-{app_id}", lambda: self.manager.get_log_cache_text(app_id), apply)

    @timed_ui_operation
    def _apply_agent_logs(self, app_id: str, text: str, error: str) -> None:
        if app_id != self.selected_app_id:
            return
        if error:
            # Keep the last useful output visible through a transient Agent/API
            # hiccup; do not replace it with a connection error.
            if not self.log_view.toPlainText():
                self.log_view.setPlainText("Runner Agent logs are temporarily unavailable.\n")
            self._update_log_buttons()
            return
        text = clean_log_display_text(text)
        previous = self._last_agent_log_text.get(app_id, "")
        current = self.log_view.toPlainText()
        if text == previous and current == text:
            return
        follow = self.follow_log_button.isChecked() and self._log_autoscroll
        old_scroll = self.log_view.verticalScrollBar().value()
        if previous and text.startswith(previous) and current == previous:
            cursor = self.log_view.textCursor()
            cursor.movePosition(QTextCursor.End)
            cursor.insertText(text[len(previous):])
        else:
            self.log_view.setPlainText(text or "No output has been captured yet.\n")
        self._last_agent_log_text[app_id] = text
        self._refresh_log_highlights()
        if follow:
            self._scroll_log_to_bottom()
        else:
            self.log_view.verticalScrollBar().setValue(old_scroll)
        self._update_log_buttons()

    @timed_ui_operation
    def _flush_selected_logs(self) -> None:
        if not self.selected_app_id:
            return
        lines = self.manager.drain_pending_log_lines(self.selected_app_id)
        if not lines:
            return
        cursor = self.log_view.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(clean_log_display_text("".join(lines)))
        self._refresh_log_highlights()
        if self._log_autoscroll:
            self._scroll_log_to_bottom()
        self._update_log_buttons()

    def _scroll_log_to_bottom(self) -> None:
        scroll_bar = self.log_view.verticalScrollBar()
        scroll_bar.setValue(scroll_bar.maximum())

    def _track_log_scroll(self, value: int) -> None:
        scroll_bar = self.log_view.verticalScrollBar()
        at_bottom = value >= max(0, scroll_bar.maximum() - 6)
        self._log_autoscroll = self.follow_log_button.isChecked() and at_bottom
        if not at_bottom and self.follow_log_button.isChecked():
            self.follow_log_button.blockSignals(True)
            self.follow_log_button.setChecked(False)
            self.follow_log_button.blockSignals(False)

    def _set_log_follow(self, enabled: bool) -> None:
        self._log_autoscroll = enabled
        if enabled:
            self._scroll_log_to_bottom()

    def _set_log_wrap(self, enabled: bool) -> None:
        configure_log_wrapping(self.log_view, enabled)

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
        if not log_file:
            self.statusBar().showMessage("No log file is available yet for this app.", 4000)
            return
        def open_file() -> None:
            if not Path(log_file).is_file():
                raise FileNotFoundError("No log file is available yet for this app.")
            os.startfile(log_file)  # type: ignore[attr-defined]
        self.workers.submit("open-selected-log", open_file, lambda _value, error: self.statusBar().showMessage(str(error), 5000) if error else None)

    @timed_ui_operation
    def _set_batch_state(self, active: bool, message: str) -> None:
        self.batch_active = active
        if active:
            self.overlay.show_message(message)
        else:
            self.overlay.hide()
        self._update_actions()

    @timed_ui_operation
    def _show_error(self, app_id: str, message: str) -> None:
        if getattr(self.manager, "remote_managed", False) and "runner agent is unavailable" in message.lower():
            self.statusBar().showMessage("Runner Agent reconnecting — " + " ".join(message.split()), 0)
            self._refresh_cluster_banner()
            self._update_actions()
            return
        if app_id and app_id == self.selected_app_id:
            self._refresh_details_panel()
        QMessageBox.warning(self, "Runner", message)

    @timed_ui_operation
    def _agent_connection_changed(self, state: str, message: str) -> None:
        if state == "connected":
            self.statusBar().showMessage("Runner Agent connected", 5000)
        else:
            self.statusBar().showMessage(message or "Runner Agent reconnecting", 0)
        self._refresh_cluster_banner()
        self._update_actions()

    def _selected_snapshot(self) -> dict[str, Any] | None:
        if not self.selected_app_id:
            return None
        # When connected, the Agent's snapshot is authoritative for managed
        # process state. ClusterClient.snapshot() may prefer its local
        # degraded-mode fallback manager, whose observation can lag the Agent
        # and disagree with the state currently rendered in the table/card.
        if (
            getattr(self.manager, "remote_managed", False)
            and getattr(self.manager, "connection_state", "connected") == "connected"
            and bool(getattr(self.manager, "snapshots_ready", True))
        ):
            return self.model.snapshot_for_app(self.selected_app_id)
        return self.manager.snapshot(self.selected_app_id)

    def _action_snapshots(self) -> list[dict[str, Any]]:
        if (
            getattr(self.manager, "remote_managed", False)
            and getattr(self.manager, "connection_state", "connected") == "connected"
            and bool(getattr(self.manager, "snapshots_ready", True))
        ):
            return self.model.snapshots()
        return self.manager.app_definitions()

    @timed_ui_operation
    def _update_actions(self) -> None:
        snapshot = self._selected_snapshot()
        has_selection = snapshot is not None
        pending = bool(snapshot and snapshot["pending_action"])
        status = snapshot["status"] if snapshot else ""
        # A verified PID in the bounded startup/unknown window is an active
        # process for action safety: do not offer a second Start, but keep
        # Stop/Restart available while state verification catches up.
        pid_backed_transition = bool(snapshot and snapshot.get("pid") and status in {"Starting", "Unknown"})
        running = bool(snapshot and (status in {"Running", "Already Running", "Waiting Input", "Degraded"} or pid_backed_transition))
        state_unknown = bool(snapshot and status == "Unknown")
        unknown_local_start = bool(state_unknown and snapshot and snapshot.get("local_control") and not snapshot.get("pid"))
        remote_owner = bool(snapshot and snapshot.get("protected") and snapshot.get("owner_node_id") and not snapshot.get("local_owner"))
        force_stoppable = bool(snapshot and (status in {"Starting", "Stopping", "Running", "Already Running", "Waiting Input", "Degraded"} or pid_backed_transition))
        agent_ready = (
            not getattr(self.manager, "remote_managed", False)
            or getattr(self.manager, "connection_state", "connected") == "connected"
            and bool(getattr(self.manager, "snapshots_ready", True))
        )
        needs_agent = bool(snapshot and snapshot.get("protected"))
        operation_available = not needs_agent or agent_ready
        disabled = self.batch_active or pending or not operation_available
        can_send_input = bool(
            snapshot
            and snapshot.get("can_accept_input")
            and not snapshot.get("visible_console")
            and not disabled
            and not snapshot.get("pending_action")
            and snapshot["status"] in {"Starting", "Running", "Waiting Input"}
        )
        start_allowed = bool(not snapshot or snapshot.get("start_allowed", True))
        records = self._action_snapshots()
        can_start_any, can_stop_any = self._bulk_actions_available(records, agent_ready)
        action_state = (
            not self.batch_active and agent_ready,
            has_selection and agent_ready and not running,
            has_selection and not disabled and not running and (not state_unknown or unknown_local_start) and start_allowed,
            not self.batch_active and can_start_any,
            not self.batch_active and can_stop_any,
            has_selection and not disabled and not running,
            has_selection and not disabled and running,
            has_selection and not self.batch_active and operation_available and force_stoppable,
            has_selection and not disabled,
            can_send_input,
        )
        edit_tooltip = "Stop the app before editing." if has_selection and running else "Edit the selected app."
        delete_tooltip = "Stop the app before deleting." if has_selection and running else "Delete the selected app."
        self.edit_action.setToolTip(edit_tooltip)
        self.delete_action.setToolTip(delete_tooltip)
        availability_tip = "Runner Agent is reconnecting. Server and HA settings are temporarily unavailable." if not agent_ready else ""
        self.add_action.setToolTip(availability_tip or ("Wait for the current bulk action to finish." if self.batch_active else "Add an app."))
        self.start_all_action.setToolTip("Wait for the current bulk action to finish." if self.batch_active else "Start locally controlled apps confirmed stopped; unknown states are reconciled first.")
        self.stop_all_action.setToolTip("Wait for the current bulk action to finish." if self.batch_active else "Stop locally controlled apps that are confirmed running.")
        if needs_agent and not agent_ready:
            start_tip = stop_tip = force_tip = restart_tip = availability_tip
        elif not has_selection:
            start_tip = stop_tip = force_tip = restart_tip = "Select an app first."
        elif pending or self.batch_active:
            start_tip = stop_tip = force_tip = restart_tip = "Wait for the current action to finish."
        elif state_unknown:
            start_tip = (
                "Runner will first check for an already-running matching process before starting this local app."
                if unknown_local_start else
                "Runner is verifying this app's state. It will become startable after reconciliation confirms it is stopped."
            )
            stop_tip = "Stop the app if its process is visible below." if snapshot and snapshot.get("pid") else "No verified process is available to stop."
            force_tip = "Force-stop the selected process tree." if force_stoppable else "There is no verified process to force-stop."
            restart_tip = "Restart is unavailable until Runner verifies the process state."
        else:
            if snapshot and snapshot.get("protected") and not start_allowed:
                owner = snapshot.get("owner_node_id") or "another node"
                reasons = "; ".join(snapshot.get("readiness_reasons", []))
                start_tip = f"Protected application. Current owner: {owner}. {reasons}".strip()
            else:
                start_tip = "The selected app is already running." if running else "Start the selected app."
            stop_tip = "Stop the selected app." if running else "The selected app is not running."
            force_tip = "Force-stop the selected process tree." if force_stoppable else "There is no active process to force-stop."
            restart_tip = "Restart the selected app."
        self.detail_start_button.setToolTip(start_tip)
        self.detail_stop_button.setToolTip(stop_tip)
        self.detail_more_button.setToolTip(force_tip)
        self.detail_restart_button.setToolTip(restart_tip)
        self.console_input_toggle.setToolTip("Show console input controls." if has_selection else "Select an app to use console input.")
        # Contextual controls make the safe, normal action obvious.  Force
        # Stop remains in More so it cannot be clicked accidentally.
        self.detail_start_button.setVisible(bool(snapshot) and (not state_unknown or unknown_local_start) and (not running or remote_owner))
        self.detail_stop_button.setVisible(running)
        self.detail_restart_button.setVisible(running)
        self.detail_more_button.setVisible(force_stoppable)
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
        self.detail_force_stop_action.setEnabled(action_state[7])
        self.detail_more_button.setEnabled(action_state[7])
        self.detail_restart_button.setEnabled(action_state[8])
        self._update_log_buttons(has_selection)
        self._refresh_input_controls(snapshot, can_send_input)

    @staticmethod
    def _bulk_actions_available(records: list[dict[str, Any]], agent_ready: bool) -> tuple[bool, bool]:
        local_running = {"Running", "Already Running", "Waiting Input", "Degraded", "Starting", "Stopping"}
        local_stopped = {"Stopped", "Stopped by Runner", "Crashed", "Exited"}
        can_start = any(
            (not record.get("protected") and (
                record.get("status") in local_stopped
                or record.get("status") == "Unknown" and record.get("local_control") and not record.get("pid")
            ))
            or (record.get("protected") and agent_ready and record.get("status") in local_stopped and record.get("start_allowed", True))
            for record in records
        )
        can_stop = any(
            (not record.get("protected") or agent_ready) and (
                record.get("status") in local_running
                or record.get("status") == "Unknown" and bool(record.get("pid"))
            )
            for record in records
        )
        return can_start, can_stop

    @timed_ui_operation
    def _refresh_cluster_banner(self) -> None:
        getter = getattr(self.manager, "agent_status", None)
        if getter is None:
            self.cluster_overview.set_overview("Standalone · local process management", {
                "primary": ("Local", "This computer", "neutral"),
                "backup": ("Not configured", "Standalone mode", "neutral"),
                "sync": ("Local", "No replication configured", "neutral"),
                "failover": ("Off", "High availability is not enabled", "neutral"),
            }, needs_setup=False)
            return
        if getattr(self.manager, "remote_managed", False) and (
            getattr(self.manager, "connection_state", "connected") != "connected"
            or not bool(getattr(self.manager, "snapshots_ready", True))
        ):
            detail = str(getattr(self.manager, "connection_message", "Runner Agent reconnecting"))
            self.cluster_overview.set_overview(
                "Agent reconnecting" if getattr(self.manager, "connection_state", "connected") != "connected" else "Loading app status",
                {
                    "primary": ("Checking", "Runner Agent connection", "warn"),
                    "backup": ("Unavailable", "Waiting for local Agent", "neutral"),
                    "sync": ("Paused", "Management data reconnecting", "neutral"),
                    "failover": ("Checking", detail, "warn"),
                },
                needs_setup=False,
            )
            return
        status = getter()
        role = str(status.get("role", "unknown")).upper()
        peers = status.get("peers", {})
        peer_connected = any(value.get("connection") == "connected" for value in peers.values())
        peer = next((value for value in peers.values() if value.get("connection") == "connected"), None)
        apps = status.get("applications", [])
        protected = [app for app in apps if app.get("protected")]
        backup_ready = bool(protected) and all(app.get("backup_ready") for app in protected)
        sync_current = bool(protected) and all(str(app.get("sync", {}).get("status", "synced")) in {"synced", "not_configured"} for app in protected)
        eligible = bool(status.get("automatic_failover_eligible"))
        transition = str(status.get("last_transition_reason") or "")
        taking_over = any(word in transition.lower() for word in ("taking", "starting", "failover", "handoff", "transfer"))
        primary_value = "Offline" if role == "OFFLINE" else ("Active" if role == "ACTIVE" else "Online")
        backup_value = "Ready" if peer_connected and backup_ready else ("Connected" if peer_connected else "Not ready")
        self.cluster_overview.set_overview(
            "Healthy" if eligible and backup_ready else ("Failover in progress" if taking_over else "Protection needs attention"),
            {
                "primary": (primary_value, f"{status.get('node_name') or status.get('node_id') or 'Local server'} / {status.get('os') or ''}", "bad" if role == "OFFLINE" else ("good" if role == "ACTIVE" else "neutral")),
                "backup": (backup_value, str(peer.get("node_name") or peer.get("hostname") or "Backup server") if peer else "No backup paired", "good" if peer_connected and backup_ready else "warn"),
                "sync": ("Current" if sync_current else ("Synchronizing" if taking_over else "Checking"), "Backup deployment current" if sync_current else "Waiting for readiness", "good" if sync_current else "warn"),
                "failover": ("Protected" if eligible else ("Transitioning" if taking_over else "Needs setup"), transition if taking_over and transition else ("Witness and readiness verified" if eligible else "Pair a backup and configure witness"), "good" if eligible else ("warn" if taking_over else "bad")),
            }, needs_setup=not eligible)
        return
        self.cluster_banner.setText(
            f"Primary: {role if role == 'ACTIVE' else 'Online'}   ·   "
            f"Backup: {'Ready' if peer_connected and backup_ready else 'Not ready'}   ·   "
            f"Sync: {'Current' if sync_current else 'Checking'}   ·   "
            f"Failover: {failover}"
        )
        self.cluster_banner.setVisible(True)

    def _show_updates(self) -> None:
        UpdateDialog(self.manager, self.config_path.parent, self, workers=self.workers).exec()

    def _show_about(self) -> None:
        QMessageBox.information(
            self,
            "About Runner",
            f"Runner GUI: {RUNNER_VERSION}\nAgent: {AGENT_VERSION}\nProtocol: {PROTOCOL_VERSION}\nBuild: {BUILD_NUMBER}\n\n"
            "Online updates require a configured signed HTTPS release manifest.",
        )

    def _show_cluster_status(self) -> None:
        getter = getattr(self.manager, "agent_status", None)
        if getter is None:
            QMessageBox.information(
                self,
                "Runner Cluster",
                "Standalone mode is active. Projects keep the original Runner v1.4 behavior.",
            )
            return
        status = getter()
        leases = status.get("leases", {})
        lease_text = "\n".join(
            f"{group}: owner {lease.get('owner_node_id')} / epoch {lease.get('epoch')}"
            for group, lease in leases.items()
        ) or "No protected service-group lease is currently held."
        QMessageBox.information(
            self,
            "Runner Cluster",
            f"Node: {status.get('node_id', 'unknown')}\n"
            f"Role: {str(status.get('role', 'unknown')).upper()}\n"
            f"Agent: {status.get('agent_version', 'unknown')} / protocol {status.get('protocol_version', 'unknown')}\n"
            f"OS: {status.get('os', 'unknown')}\n\n{lease_text}\n\n"
            f"Last decision: {status.get('last_transition_reason', '')}",
        )

    def _show_servers(self, setup_mode: bool = False) -> None:
        if not hasattr(self.manager, "create_pairing_offer"):
            title = "Finish HA Setup" if setup_mode else "Cluster / Servers"
            QMessageBox.information(self, title, "Start or repair the Runner Agent before setting up server connections.")
            return
        PairingDialog(self.manager, self, setup_mode=setup_mode, workers=self.workers).exec()

    @timed_ui_operation
    def _update_log_buttons(self, has_selection: bool | None = None) -> None:
        if has_selection is None:
            has_selection = self.selected_app_id is not None
        has_live_output = bool(has_selection and self.selected_app_id and self.manager.has_live_log_output(self.selected_app_id))
        snapshot = self._selected_snapshot() if has_selection else None
        log_path = Path(str(snapshot.get("log_file_path") or "")) if snapshot else None
        has_log_file = bool(log_path and str(log_path) not in {"", "."})
        self.clear_log_button.setEnabled(has_live_output)
        self.copy_log_button.setEnabled(has_live_output)
        self.open_log_action.setEnabled(has_log_file)
        self.clear_log_button.setToolTip("Clear recent live output (the disk log is kept)." if has_live_output else "No recent live output to clear.")
        self.copy_log_button.setToolTip("Copy recent live output." if has_live_output else "No recent live output to copy.")
        self.open_log_action.setToolTip("Open the complete log file." if has_log_file else "No log file is available yet.")

    def _add_app(self) -> None:
        dialog = AppEditorDialog(self, workers=self.workers)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        app_data = dialog.app_data()
        self.add_action.setEnabled(False)
        def save() -> tuple[str, dict[str, Any]]:
            validated = self._validated_app_data(app_data)
            validated["id"] = self._make_app_id(validated["name"])
            snapshot = self.manager.add_app(validated)
            self._persist_apps()
            return validated["id"], snapshot
        def saved(value: Any, error: BaseException | None) -> None:
            self.add_action.setEnabled(True)
            if error:
                self._show_operation_error("Save application", error if isinstance(error, Exception) else RuntimeError(str(error)))
                return
            self._pending_select_app_id = value[0]
            self._select_app(value[0])
        if not self.workers.submit("app-add", save, saved):
            self.add_action.setEnabled(True)
            self.statusBar().showMessage("Runner is busy completing other background work. Try again shortly.", 5000)

    def _edit_selected(self) -> None:
        snapshot = self._selected_snapshot()
        if not snapshot:
            return
        if snapshot["status"] in {"Running", "Already Running", "Waiting Input"} or snapshot["pending_action"]:
            QMessageBox.warning(self, "Runner", "Stop the app before editing it.")
            return

        dialog = AppEditorDialog(self, initial=snapshot, workers=self.workers)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        app_data = dialog.app_data()
        app_id = str(snapshot["id"])
        self.edit_action.setEnabled(False)
        def save() -> dict[str, Any]:
            validated = self._validated_app_data(app_data)
            result = self.manager.update_app(app_id, validated)
            self._persist_apps()
            return result
        def saved(_value: Any, error: BaseException | None) -> None:
            self.edit_action.setEnabled(True)
            if error:
                self._show_operation_error("Save application", error if isinstance(error, Exception) else RuntimeError(str(error)))
            else:
                self._pending_select_app_id = app_id
                self._select_app(app_id)
        self.workers.submit(f"app-edit-{app_id}", save, saved)

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
        app_id = str(snapshot["id"])
        self.delete_action.setEnabled(False)
        def remove() -> None:
            self.manager.remove_app(app_id)
            self._persist_apps()
        def removed(_value: Any, error: BaseException | None) -> None:
            self.delete_action.setEnabled(True)
            if error:
                self._show_operation_error("Delete application", error if isinstance(error, Exception) else RuntimeError(str(error)))
            else:
                self.selected_app_id = None
        self.workers.submit(f"app-delete-{app_id}", remove, removed)

    def _persist_apps(self) -> None:
        if getattr(self.manager, "remote_managed", False):
            return
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
        runner_file = Path(runner_path) if runner_path else None
        if not folder.is_dir():
            raise ValueError("Selected folder does not exist.")
        if not runner_file.is_file():
            raise ValueError("Selected runner file does not exist.")
        if runner_file.suffix.lower() not in RUNNER_FILE_SUFFIXES:
            raise ValueError("Runner file must be a supported Python, Node.js, shell, Batch, command, or executable file.")

        return {
            "name": name,
            "cwd": str(folder.resolve()),
            "runner_path": str(runner_file.resolve()),
            "args": list(app_data["args"]),
            "startup_input": app_data["startup_input"],
            "interactive": bool(app_data.get("interactive", False)),
            "visible_console": bool(app_data.get("visible_console", False)),
            "auto_start": bool(app_data.get("auto_start", False)),
            "protected": bool(app_data.get("protected", False)),
            "service_group": str(app_data.get("service_group") or ""),
            "dependencies": list(app_data.get("dependencies", [])),
            "health_check": dict(app_data.get("health_check") or {"type": "process"}),
            "persistence": dict(app_data.get("persistence") or {"strategy": "stateless"}),
            "sync": dict(app_data.get("sync") or {"enabled": False}),
            "deployments": dict(app_data.get("deployments") or {}),
            "env": dict(app_data.get("env") or {}),
            "app_type": "process",
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

    def _show_operation_error(self, operation: str, exc: Exception) -> None:
        """Give operators a supportable error ID without exposing a traceback."""
        error_id = uuid.uuid4().hex[:12]
        log_path = self.config_path.parent / "logs" / "runner-ui-errors.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8", errors="replace") as handle:
                handle.write(f"\n[{error_id}] {operation}\n{traceback.format_exc()}\n")
        except OSError:
            log_path = self.config_path.parent / "runner-ui-errors.log"
        QMessageBox.warning(
            self,
            "Runner",
            f"{operation} could not be completed.\n\nError ID: {error_id}\nLog: {log_path}\n\n{exc}",
        )

    def _start_selected(self) -> None:
        if self.selected_app_id:
            self.manager.start_app(self.selected_app_id)

    def _stop_selected(self) -> None:
        if self.selected_app_id:
            self.manager.stop_app(self.selected_app_id)

    def _force_stop_selected(self) -> None:
        if not self.selected_app_id:
            return
        snapshot = self._selected_snapshot()
        name = str(snapshot.get("name") if snapshot else "selected application")
        answer = QMessageBox.warning(
            self,
            "Force stop application",
            f"Force stop {name}?\n\nThis immediately terminates its process tree and may interrupt work.",
            QMessageBox.Cancel | QMessageBox.Yes,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Yes:
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

    @timed_ui_operation
    def _tick_selected_details(self) -> None:
        if not self.selected_app_id:
            return
        snapshot = self.manager.snapshot(self.selected_app_id)
        if snapshot["status"] in {"Starting", "Stopping", "Running", "Already Running", "Waiting Input", "Degraded"}:
            self._refresh_details_panel(snapshot)

    def _refresh_input_controls(self, snapshot: dict[str, Any] | None, can_send_input: bool) -> None:
        supported = bool(snapshot and (snapshot.get("can_accept_input") or snapshot.get("visible_console")))
        self.console_input_toggle.setVisible(supported)
        self.console_input_toggle.setEnabled(supported)
        if not supported:
            self.console_input_toggle.setChecked(False)
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
        self.technical_details_toggle.setText("▾  Technical details" if expanded else "▸  Technical details")
        self.technical_details_widget.setVisible(expanded)
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)

    def _toggle_console_input(self, expanded: bool) -> None:
        self.console_input_toggle.setText("▾  Console input" if expanded else "▸  Console input")
        self.input_row_widget.setVisible(expanded and self.selected_app_id is not None)
        self.input_hint.setVisible(expanded and bool(self.input_hint.text()))
        QTimer.singleShot(0, self._rebalance_detail_log_splitter)

    def _rebalance_detail_log_splitter(self) -> None:
        total = sum(self.detail_log_splitter.sizes())
        if total <= 0:
            return
        # Keep logs as the dominant surface.  Details remain readable, while
        # the splitter still lets a user expand either section when needed.
        desired_details = min(max(self.details_card.sizeHint().height(), 185), max(185, int(total * 0.40)))
        self.detail_log_splitter.setSizes([desired_details, max(220, total - desired_details)])

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
        self.start_with_windows_action.setEnabled(False)
        def update() -> str:
            if enabled:
                self._write_startup_command()
                self._install_startup_task()
                return "Runner will open automatically after this Windows user signs in. Only apps checked with 'Start when Runner opens' will be started."
            else:
                self._startup_command_path().unlink(missing_ok=True)
                self._remove_startup_task()
                return "Runner will no longer open automatically when Windows starts."
        def complete(message: Any, error: BaseException | None) -> None:
            self.start_with_windows_action.setEnabled(True)
            if error:
                exc = error
                self.start_with_windows_action.blockSignals(True)
                self.start_with_windows_action.setChecked(not enabled)
                self.start_with_windows_action.blockSignals(False)
                self._refresh_start_with_windows_action()
                self.statusBar().showMessage(f"Could not update Windows startup: {exc}", 6000)
                return
            self._refresh_start_with_windows_action()
            self.statusBar().showMessage(" ".join(str(message).split()), 5000)
        if not self.workers.submit("windows-startup-config", update, complete):
            self.start_with_windows_action.blockSignals(True)
            self.start_with_windows_action.setChecked(not enabled)
            self.start_with_windows_action.blockSignals(False)
            self.start_with_windows_action.setEnabled(True)
            self._refresh_start_with_windows_action()
            self.statusBar().showMessage("Runner is busy. Try changing startup settings again shortly.", 5000)

    def _refresh_start_with_windows_action(self) -> None:
        enabled = self.start_with_windows_action.isChecked()
        if hasattr(self, "startup_menu_action"):
            self.startup_menu_action.blockSignals(True)
            self.startup_menu_action.setChecked(enabled)
            self.startup_menu_action.blockSignals(False)
        label = "Start with Windows: ON" if enabled else "Start with Windows: OFF"
        self.start_with_windows_action.setText(label)
        self.start_with_windows_action.setToolTip(
            "When ON, Windows schedules Runner for this user's next sign-in and also keeps a Startup-folder backup. "
            "Runner then starts apps checked with 'Start when Runner opens'."
        )

    def _startup_enabled(self) -> bool:
        return self._startup_command_path().exists() or self._startup_task_exists()

    def _query_startup_state(self) -> None:
        def apply(enabled: Any, error: BaseException | None) -> None:
            if error:
                self.statusBar().showMessage(f"Could not check Windows startup settings: {error}", 5000)
                return
            self.start_with_windows_action.blockSignals(True)
            self.start_with_windows_action.setChecked(bool(enabled))
            self.start_with_windows_action.blockSignals(False)
            self._refresh_start_with_windows_action()
        self.workers.submit("windows-startup-query", self._startup_enabled, apply)

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

    def _startup_task_exists(self) -> bool:
        if os.name != "nt":
            return False
        result = subprocess.run(
            ["schtasks", "/Query", "/TN", STARTUP_TASK_NAME],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0

    def _install_startup_task(self) -> None:
        """Schedule this exact Runner copy at user logon (more reliable than Startup alone)."""
        if os.name != "nt" or self._startup_task_exists():
            return
        command_parts, workdir = self._runner_start_command()
        runtime_root = workdir / ".runner_runtime"
        task_command = subprocess.list2cmdline([*command_parts, "--runtime-root", str(runtime_root)])
        result = subprocess.run(
            [
                "schtasks", "/Create", "/F", "/TN", STARTUP_TASK_NAME,
                "/SC", "ONLOGON", "/RL", "HIGHEST", "/TR", task_command,
            ],
            capture_output=True,
            text=True,
            timeout=8,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            detail = (result.stderr or result.stdout).strip()
            raise OSError(detail or "Windows could not create the Runner startup task.")

    def _remove_startup_task(self) -> None:
        if os.name != "nt":
            return
        result = subprocess.run(
            ["schtasks", "/Delete", "/F", "/TN", STARTUP_TASK_NAME],
            capture_output=True,
            timeout=8,
            text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # A missing task is already the requested end state.
        if result.returncode and self._startup_task_exists():
            detail = (result.stderr or result.stdout).strip()
            raise OSError(detail or "Windows could not remove the Runner startup task.")

    def _runner_start_command(self) -> tuple[list[str], Path]:
        if getattr(sys, "frozen", False):
            exe = Path(sys.executable).resolve()
            return [str(exe)], exe.parent

        project_root = Path(__file__).resolve().parents[2]
        script = project_root / "src" / "launcher" / "run.py"
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        python_exe = pythonw if pythonw.exists() else Path(sys.executable)
        return [str(python_exe), str(script)], project_root

    def _restore_ui_state(self) -> None:
        def read_state() -> dict[str, Any]:
            try:
                value = json.loads(self.ui_state_path.read_text(encoding="utf-8-sig"))
                return value if isinstance(value, dict) else {}
            except (OSError, json.JSONDecodeError):
                return {}
        self.workers.submit("ui-state-load", read_state, self._apply_ui_state)

    def _apply_ui_state(self, state: Any, _error: BaseException | None = None) -> None:
        if not isinstance(state, dict):
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

    @timed_ui_operation
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
        serialized = json.dumps(state, indent=2)
        self.workers.submit(
            "ui-state-save-close",
            lambda: write_text_atomic(self.ui_state_path, serialized),
            lambda *_: None,
        )
