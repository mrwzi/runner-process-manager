from __future__ import annotations

import os
import re
import shlex
import sys


def build_start_command(app_record: dict) -> tuple[list[str], str, str]:
    folder = _clean_text(app_record.get("folder") or app_record.get("cwd"))
    runner_file = _clean_text(app_record.get("runner_file") or app_record.get("runner_path"))
    arguments = app_record.get("arguments", app_record.get("args", ""))
    startup_input = _clean_text(app_record.get("startup_input"))

    if not runner_file:
        raise ValueError("runner_file is required")

    runner_path = os.path.abspath(os.path.expanduser(runner_file))
    folder_path = os.path.abspath(os.path.expanduser(folder)) if folder else os.path.dirname(runner_path)
    cwd_folder = folder_path if os.path.isdir(folder_path) else os.path.dirname(runner_path)

    runner_lower = runner_path.lower()
    if runner_lower.endswith(".py"):
        runner_cmd = _display_path(runner_path, cwd_folder)
        python_executable = _detect_python_interpreter(cwd_folder, runner_path)
        cmd_list = [python_executable, "-u", runner_cmd]
    elif runner_lower.endswith((".js", ".mjs")):
        runner_cmd = _display_path(runner_path, cwd_folder)
        cmd_list = [_detect_node_interpreter(), runner_cmd]
    elif runner_lower.endswith((".bat", ".cmd")):
        runner_cmd = _display_path(runner_path, cwd_folder)
        cmd_executable = os.environ.get("COMSPEC") or "cmd.exe"
        cmd_list = [cmd_executable, "/D", "/C", runner_cmd]
    else:
        cmd_list = [runner_path]

    cmd_list.extend(_normalize_arguments(arguments))

    stdin_text = startup_input
    if stdin_text and not stdin_text.endswith("\n"):
        stdin_text += "\n"

    return cmd_list, cwd_folder, stdin_text


def _normalize_arguments(arguments: object) -> list[str]:
    if arguments is None:
        return []
    if isinstance(arguments, (list, tuple)):
        return [str(argument) for argument in arguments if str(argument)]

    arguments_text = _clean_text(arguments)
    if not arguments_text:
        return []

    posix_mode = os.name != "nt"
    return shlex.split(arguments_text, posix=posix_mode)


def _display_path(target_path: str, cwd_folder: str) -> str:
    try:
        relative_path = os.path.relpath(target_path, cwd_folder)
    except ValueError:
        return target_path
    if relative_path.startswith(".."):
        return target_path
    return relative_path


def _detect_python_interpreter(cwd_folder: str, runner_path: str) -> str:
    search_roots: list[str] = []
    for candidate in [cwd_folder, os.path.dirname(runner_path)]:
        candidate = _clean_text(candidate)
        if candidate and candidate not in search_roots:
            search_roots.append(candidate)

    for root in search_roots:
        venv_candidates = _find_local_python_candidates(root)
        if venv_candidates:
            return venv_candidates[0]
        direct_python = os.path.join(root, "python.exe")
        if _is_usable_executable(direct_python):
            return direct_python

    base_executable = _clean_text(getattr(sys, "_base_executable", ""))
    if _looks_like_python_executable(base_executable) and _is_usable_executable(base_executable):
        return base_executable

    if _looks_like_python_executable(sys.executable) and _is_usable_executable(sys.executable):
        return sys.executable

    path_python = _find_python_on_path()
    if path_python:
        return path_python

    windows_python = _find_windows_python_install()
    if windows_python:
        return windows_python

    raise ValueError(f"Could not find a Python interpreter for {runner_path}")


def _detect_node_interpreter() -> str:
    path_value = os.environ.get("PATH", "")
    for folder in path_value.split(os.pathsep):
        folder = _clean_text(folder)
        if not folder:
            continue
        for executable in ("node.exe", "node"):
            candidate = os.path.join(folder, executable)
            if _is_usable_executable(candidate):
                return candidate
    raise ValueError("Could not find Node.js. Install Node.js or add node.exe to PATH.")


def _find_local_python_candidates(root: str) -> list[str]:
    candidates: list[tuple[int, str]] = []
    try:
        entries = list(os.scandir(root))
    except OSError:
        return []

    for entry in entries:
        if not entry.is_dir():
            continue
        name = entry.name.lower()
        python_path = os.path.join(entry.path, "Scripts", "python.exe")
        if not _is_usable_executable(python_path):
            continue
        score = 0
        if name == ".venv":
            score = 500
        elif name.startswith(".venv"):
            score = 450
        elif name == "venv":
            score = 400
        elif name.startswith("venv"):
            score = 350
        elif name == "env":
            score = 300
        elif name.startswith("env"):
            score = 250
        elif os.path.isfile(os.path.join(entry.path, "pyvenv.cfg")):
            score = 200
        else:
            continue
        candidates.append((score, python_path))

    candidates.sort(key=lambda item: item[0], reverse=True)
    return [path for _, path in candidates]


def _looks_like_python_executable(path: str) -> bool:
    name = os.path.basename(path).lower()
    return name.startswith("python") and name.endswith(".exe")


def _find_python_on_path() -> str:
    path_value = os.environ.get("PATH", "")
    for folder in path_value.split(os.pathsep):
        folder = _clean_text(folder)
        if not folder:
            continue
        candidate = os.path.join(folder, "python.exe")
        if _is_usable_executable(candidate):
            return candidate
    return ""


def _find_windows_python_install() -> str:
    roots = [
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Python"),
        os.path.join(os.environ.get("ProgramFiles", ""), "Python"),
        os.path.join(os.environ.get("ProgramFiles(x86)", ""), "Python"),
    ]
    candidates: list[tuple[tuple[int, ...], str]] = []
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        try:
            for entry in os.scandir(root):
                if not entry.is_dir():
                    continue
                candidate = os.path.join(entry.path, "python.exe")
                if _is_usable_executable(candidate):
                    candidates.append((_python_install_version(entry.name), candidate))
        except OSError:
            continue
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1] if candidates else ""


def _python_install_version(folder_name: str) -> tuple[int, ...]:
    numbers = re.findall(r"\d+", folder_name)
    if not numbers:
        return (0,)
    if len(numbers) == 1 and len(numbers[0]) > 1:
        digits = numbers[0]
        return (int(digits[0]), int(digits[1:]))
    return tuple(int(number) for number in numbers)


def _is_usable_executable(path: str) -> bool:
    """Reject missing files and zero-byte Windows Store execution aliases."""
    try:
        return os.path.isfile(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def _clean_text(value: object) -> str:
    return str(value or "").strip()
