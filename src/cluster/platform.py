from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import psutil


class PlatformAdapter(ABC):
    name: str

    @abstractmethod
    def build_command(self, runner_path: str, cwd: str, args: list[str]) -> list[str]: ...

    @abstractmethod
    def process_flags(self, visible_console: bool) -> int: ...

    @abstractmethod
    def terminate_tree(self, pid: int, force: bool = False) -> None: ...

    @abstractmethod
    def service_install_instructions(self, executable: Path, runtime_root: Path) -> str: ...

    def find_python(self, cwd: str) -> str:
        root = Path(cwd)
        candidates = [root / name / ("Scripts/python.exe" if os.name == "nt" else "bin/python") for name in (".venv", "venv", "env")]
        candidates.extend(sorted(root.glob(".venv*/" + ("Scripts/python.exe" if os.name == "nt" else "bin/python"))))
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        executable = shutil.which("python3") or shutil.which("python")
        if executable:
            return executable
        raise FileNotFoundError(f"No Python interpreter is available for {cwd}")


class WindowsPlatformAdapter(PlatformAdapter):
    name = "windows"

    def build_command(self, runner_path: str, cwd: str, args: list[str]) -> list[str]:
        suffix = Path(runner_path).suffix.lower()
        if suffix == ".py":
            return [self.find_python(cwd), "-u", runner_path, *args]
        if suffix in {".js", ".mjs"}:
            node = shutil.which("node")
            if not node:
                raise FileNotFoundError("Node.js is not installed or not in PATH")
            return [node, runner_path, *args]
        if suffix in {".bat", ".cmd"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/D", "/C", runner_path, *args]
        return [runner_path, *args]

    def process_flags(self, visible_console: bool) -> int:
        group = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        return group | (getattr(subprocess, "CREATE_NEW_CONSOLE", 0) if visible_console else getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def terminate_tree(self, pid: int, force: bool = False) -> None:
        _terminate_psutil_tree(pid, force)

    def service_install_instructions(self, executable: Path, runtime_root: Path) -> str:
        return f'Install the bundled Windows service for "{executable}" --agent --runtime-root "{runtime_root}".'


class LinuxPlatformAdapter(PlatformAdapter):
    name = "linux"

    def build_command(self, runner_path: str, cwd: str, args: list[str]) -> list[str]:
        suffix = Path(runner_path).suffix.lower()
        if suffix == ".py":
            return [self.find_python(cwd), "-u", runner_path, *args]
        if suffix in {".js", ".mjs"}:
            node = shutil.which("node")
            if not node:
                raise FileNotFoundError("Node.js is not installed or not in PATH")
            return [node, runner_path, *args]
        if suffix in {".sh", ".bash"}:
            return [shutil.which("bash") or "/bin/bash", runner_path, *args]
        if suffix in {".bat", ".cmd", ".exe"}:
            raise OSError(f"{suffix} applications are not deployable on Linux")
        return [runner_path, *args]

    def process_flags(self, visible_console: bool) -> int:
        return 0

    def terminate_tree(self, pid: int, force: bool = False) -> None:
        _terminate_psutil_tree(pid, force)

    def service_install_instructions(self, executable: Path, runtime_root: Path) -> str:
        return f"Use the bundled runner-agent.service with ExecStart={executable} --agent --runtime-root {runtime_root}."


def current_platform() -> PlatformAdapter:
    if sys.platform == "win32":
        return WindowsPlatformAdapter()
    if sys.platform.startswith("linux"):
        return LinuxPlatformAdapter()
    raise OSError(f"Unsupported platform: {sys.platform}")


def _terminate_psutil_tree(pid: int, force: bool) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    processes = [*root.children(recursive=True), root]
    for process in processes:
        try:
            process.kill() if force else process.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(processes, timeout=1.5 if force else 5.0)
    for process in alive:
        try:
            process.kill()
        except psutil.Error:
            pass
