from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

import psutil


FENCED_EXIT_CODE = 77


def _kill_tree(pid: int) -> None:
    try:
        root = psutil.Process(pid)
    except psutil.Error:
        return
    processes = [*root.children(recursive=True), root]
    for process in processes:
        try:
            process.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(processes, timeout=3)


def run_guard(guard_file: str, command: list[str], poll_seconds: float = 0.25) -> int:
    """Run a child only while the independent Agent authorization deadline is fresh."""
    if not command:
        raise ValueError("Guarded command is empty")
    try:
        initial_deadline = float(open(guard_file, encoding="ascii").read().strip())
    except (OSError, ValueError):
        return FENCED_EXIT_CODE
    if time.monotonic() >= initial_deadline:
        return FENCED_EXIT_CODE
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    process = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, creationflags=flags)
    while process.poll() is None:
        try:
            deadline = float(open(guard_file, encoding="ascii").read().strip())
        except (OSError, ValueError):
            deadline = 0.0
        if time.monotonic() >= deadline:
            print("[Runner Guard] Authorization expired; fencing protected process tree", file=sys.stderr, flush=True)
            _kill_tree(process.pid)
            return FENCED_EXIT_CODE
        time.sleep(poll_seconds)
    return int(process.returncode or 0)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--guard-file", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    return run_guard(args.guard_file, command)


if __name__ == "__main__":
    raise SystemExit(main())
