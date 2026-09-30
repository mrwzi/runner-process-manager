"""Small installed update helper for an already verified Runner package."""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def backup_binaries(install_dir: Path, backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name in ("Runner.exe", "UpdateRunner.exe"):
        source = install_dir / name
        if source.exists():
            shutil.copy2(source, backup_dir / name)


def restore_binaries(install_dir: Path, backup_dir: Path) -> None:
    for name in ("Runner.exe", "UpdateRunner.exe"):
        source = backup_dir / name
        if source.exists():
            shutil.copy2(source, install_dir / name)


def restart_agent() -> None:
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/End", "/TN", "Runner Agent"], capture_output=True)
        subprocess.run(["schtasks", "/Run", "/TN", "Runner Agent"], capture_output=True)


def install(package: Path, install_dir: Path, runtime_root: Path, restart_gui: bool = True) -> int:
    if not package.is_file():
        raise FileNotFoundError(package)
    backup = runtime_root / "config-backups" / f"binary-update-{time.strftime('%Y%m%d-%H%M%S')}"
    backup_binaries(install_dir, backup)
    result = subprocess.run(
        # RunnerSetup owns process handoff. Do not delegate to Inno's generic
        # close/skip prompt: it cannot distinguish Runner from managed apps.
        # Keep the bounded, actionable Setup error visible. Suppressing its
        # message boxes hid safe-abort/lock details from normal users.
        [str(package), "/SILENT", "/NORESTART"],
        cwd=str(install_dir), capture_output=True, text=True,
    )
    if result.returncode != 0:
        # Setup owns the atomic binary replacement and rollback. Do not try to
        # overwrite an executable after Setup reports a failure: it may still
        # be mapped/locked, and a second copy attempt could create a mixed
        # version. Keep the timestamped backup for explicit recovery instead.
        return result.returncode
    restart_agent()
    if restart_gui and (runner := install_dir / "Runner.exe").exists():
        subprocess.Popen([str(runner)], cwd=str(install_dir))
    return 0


def launch_detached_worker(argv: list[str]) -> int:
    """Run the actual installer from a temporary updater image.

    The installed UpdateRunner.exe cannot replace itself while its image is
    mapped. The short-lived front process therefore copies itself to a private
    temp directory, starts that copy, and exits before Setup begins. The worker
    waits for Setup and performs the normal post-upgrade checks/restart.
    """
    work_dir = Path(tempfile.mkdtemp(prefix="RunnerUpdateBootstrap-"))
    worker = work_dir / "UpdateRunner.exe"
    try:
        shutil.copy2(sys.executable, worker)
        command = [str(worker), "--worker", *argv]
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen(command, cwd=str(work_dir), close_fds=True, creationflags=flags)
        return 0
    except Exception:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="Runner verified update helper")
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--package", required=True)
    parser.add_argument("--install-dir", required=True)
    parser.add_argument("--runtime-root", required=True)
    parser.add_argument("--no-restart-gui", action="store_true")
    args = parser.parse_args()
    if getattr(sys, "frozen", False) and not args.worker:
        worker_args = []
        if args.package:
            worker_args += ["--package", args.package]
        if args.install_dir:
            worker_args += ["--install-dir", args.install_dir]
        if args.runtime_root:
            worker_args += ["--runtime-root", args.runtime_root]
        if args.no_restart_gui:
            worker_args.append("--no-restart-gui")
        try:
            return launch_detached_worker(worker_args)
        except Exception as exc:
            print(f"Could not safely start the update worker: {exc}", file=sys.stderr)
            return 1
    try:
        return install(Path(args.package), Path(args.install_dir), Path(args.runtime_root), not args.no_restart_gui)
    except Exception as exc:
        print(f"Runner update failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
