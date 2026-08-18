from __future__ import annotations

import difflib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psutil

PROJECT_NAME = "Runner_V4"
RUNTIME_DIR_NAME = ".runner_runtime"

STATE_FILE_NAME = ".runner_update_state.json"
REPORT_FILE_NAME = "last_update_report.txt"
SOURCE_EXTENSIONS = {".py", ".json", ".ps1", ".bat", ".txt"}
HISTORY_LIMIT = 25


def find_project_root() -> Path:
    candidates = [
        Path(__file__).resolve().parent,
        Path.cwd(),
        Path(sys.argv[0]).resolve().parent,
    ]
    for candidate in candidates:
        for probe in [candidate, *candidate.parents]:
            if (probe / "launcher" / "run.py").exists() and (probe / "build" / "build.ps1").exists():
                return probe
    raise FileNotFoundError(f"Could not locate {PROJECT_NAME} project root.")


def runtime_artifact_dir(root: Path) -> Path:
    target = root / RUNTIME_DIR_NAME / "updater"
    target.mkdir(parents=True, exist_ok=True)
    return target


def main() -> int:
    try:
        root = find_project_root()
    except Exception as exc:
        print(f"Update failed: {exc}")
        wait_for_close()
        return 1

    artifact_dir = runtime_artifact_dir(root)
    state_path = artifact_dir / STATE_FILE_NAME
    report_path = artifact_dir / REPORT_FILE_NAME
    build_script = root / "build" / "build-runner-only.ps1"
    runner_exe = root / "Runner.exe"

    previous_state = load_state(state_path)
    current_snapshot = snapshot_source_tree(root)
    file_changes, added_lines, removed_lines = diff_snapshots(previous_state.get("snapshot", {}), current_snapshot)

    command = [
        "powershell",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(build_script),
    ]

    print(f"Project: {root}")
    print("Building Runner.exe...")

    stop_running_runner(runner_exe)
    result = subprocess.run(command, cwd=root, text=True, capture_output=True)

    build_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    runner_exists = runner_exe.exists()
    runner_timestamp = (
        runner_exe.stat().st_mtime if runner_exists else None
    )
    runner_timestamp_text = (
        datetime.fromtimestamp(runner_timestamp).strftime("%Y-%m-%d %H:%M:%S")
        if runner_timestamp
        else "missing"
    )

    next_update_count = int(previous_state.get("update_count", 0))
    if result.returncode == 0:
        next_update_count += 1

    summary = {
        "build_time": build_time,
        "success": result.returncode == 0,
        "update_count": next_update_count,
        "changed_files": len(file_changes),
        "added_lines": added_lines,
        "removed_lines": removed_lines,
        "runner_timestamp": runner_timestamp_text,
    }

    report_text = build_report(
        root=root,
        summary=summary,
        file_changes=file_changes,
        stdout_text=result.stdout,
        stderr_text=result.stderr,
    )
    write_text_atomic(report_path, report_text)

    if result.returncode == 0:
        print("")
        print("Build complete.")
        print(f"Update number: {next_update_count}")
        print(f"Changed files: {len(file_changes)}")
        print(f"Runner.exe time: {runner_timestamp_text}")
        save_state(
            state_path,
            previous_state,
            current_snapshot,
            summary,
            file_changes,
        )
    else:
        print("")
        print(f"Build failed with exit code {result.returncode}.")

    maybe_open_report(report_path)
    wait_for_close()
    return result.returncode


def snapshot_source_tree(root: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if should_skip_path(root, path):
            continue
        if path.suffix.lower() not in SOURCE_EXTENSIONS:
            continue
        relative_path = path.relative_to(root).as_posix()
        snapshot[relative_path] = path.read_text(encoding="utf-8", errors="replace")
    return snapshot


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    temp_path.write_text(text, encoding="utf-8")
    temp_path.replace(path)


def should_skip_path(root: Path, path: Path) -> bool:
    relative = path.relative_to(root)
    parts = {part.lower() for part in relative.parts}
    if "__pycache__" in parts or "dist" in parts or RUNTIME_DIR_NAME.lower() in parts:
        return True
    if relative.name in {STATE_FILE_NAME, REPORT_FILE_NAME}:
        return True
    if relative.suffix.lower() == ".spec":
        return True
    if len(relative.parts) >= 2 and relative.parts[0].lower() == "build" and relative.parts[1] in {"Runner", "UpdateRunner"}:
        return True
    return False


def diff_snapshots(old_snapshot: dict[str, str], new_snapshot: dict[str, str]) -> tuple[list[dict[str, object]], int, int]:
    changes: list[dict[str, object]] = []
    total_added = 0
    total_removed = 0

    all_paths = sorted(set(old_snapshot) | set(new_snapshot))
    for path in all_paths:
        old_text = old_snapshot.get(path)
        new_text = new_snapshot.get(path)
        if old_text == new_text:
            continue

        added, removed = diff_line_counts(old_text or "", new_text or "")
        status = "modified"
        if old_text is None:
            status = "added"
        elif new_text is None:
            status = "deleted"

        changes.append(
            {
                "path": path,
                "status": status,
                "added": added,
                "removed": removed,
            }
        )
        total_added += added
        total_removed += removed

    return changes, total_added, total_removed


def diff_line_counts(old_text: str, new_text: str) -> tuple[int, int]:
    old_lines = old_text.splitlines()
    new_lines = new_text.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines)
    added = 0
    removed = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "insert":
            added += j2 - j1
        elif tag == "delete":
            removed += i2 - i1
        elif tag == "replace":
            removed += i2 - i1
            added += j2 - j1
    return added, removed


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"update_count": 0, "history": [], "snapshot": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8-sig"))
    except Exception:
        return {"update_count": 0, "history": [], "snapshot": {}}


def save_state(
    state_path: Path,
    previous_state: dict,
    snapshot: dict[str, str],
    summary: dict[str, object],
    file_changes: list[dict[str, object]],
) -> None:
    history = list(previous_state.get("history", []))
    history.append(
        {
            "build_time": summary["build_time"],
            "success": summary["success"],
            "update_count": summary["update_count"],
            "changed_files": summary["changed_files"],
            "added_lines": summary["added_lines"],
            "removed_lines": summary["removed_lines"],
            "runner_timestamp": summary["runner_timestamp"],
            "files": file_changes,
        }
    )
    state = {
        "update_count": summary["update_count"],
        "history": history[-HISTORY_LIMIT:],
        "snapshot": snapshot,
    }
    write_text_atomic(state_path, json.dumps(state, indent=2))


def build_report(
    root: Path,
    summary: dict[str, object],
    file_changes: list[dict[str, object]],
    stdout_text: str,
    stderr_text: str,
) -> str:
    lines = [
        "Runner Update Report",
        "====================",
        f"Project: {root}",
        f"Build Time: {summary['build_time']}",
        f"Build Success: {'YES' if summary['success'] else 'NO'}",
        f"Update Number: {summary['update_count']}",
        f"Runner.exe Updated At: {summary['runner_timestamp']}",
        f"Changed Files: {summary['changed_files']}",
        f"Lines Added: {summary['added_lines']}",
        f"Lines Removed: {summary['removed_lines']}",
        "",
        "Changed Files Detail",
        "--------------------",
    ]

    if file_changes:
        for change in file_changes:
            lines.append(
                f"{change['status'].upper():8} {change['path']}  (+{change['added']} / -{change['removed']})"
            )
    else:
        lines.append("No source file changes detected since the last successful update.")

    lines.extend(
        [
            "",
            "Build Output",
            "------------",
            stdout_text.strip() or "(no stdout)",
        ]
    )

    if stderr_text.strip():
        lines.extend(
            [
                "",
                "Build Errors",
                "------------",
                stderr_text.strip(),
            ]
        )

    lines.append("")
    return "\n".join(lines)


def stop_running_runner(runner_exe: Path) -> None:
    runner_exe = runner_exe.resolve()
    targets: list[psutil.Process] = []
    for process in psutil.process_iter(["pid", "name", "exe"]):
        try:
            exe = process.info.get("exe")
            if exe and Path(exe).resolve() == runner_exe:
                targets.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
            continue

    if not targets:
        return

    for process in targets:
        try:
            process.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    _, alive = psutil.wait_procs(targets, timeout=5.0)
    for process in alive:
        try:
            process.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def maybe_open_report(report_path: Path) -> None:
    if os.environ.get("RUNNER_UPDATER_NOOPEN") == "1":
        return
    try:
        subprocess.Popen(["notepad.exe", str(report_path)])
    except Exception:
        pass


def wait_for_close() -> None:
    try:
        if sys.stdin and sys.stdin.isatty():
            input("Press Enter to close...")
    except EOFError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
