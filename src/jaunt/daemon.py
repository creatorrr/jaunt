"""Background daemon: lockfile, poll loop, job scheduling, landing."""

from __future__ import annotations

import os
from pathlib import Path

DISABLE_ENV = "JAUNT_DAEMON_DISABLE"


def _lock_path(root: Path) -> Path:
    return root / ".jaunt" / "daemon.pid"


def lock_pid(root: Path) -> int | None:
    path = _lock_path(root)
    if not path.exists():
        return None
    try:
        pid = int(path.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
    except (ValueError, ProcessLookupError, PermissionError):
        return None
    return pid


def acquire_lock(root: Path) -> bool:
    """Acquire via O_CREAT|O_EXCL so two daemons cannot pass a check-then-write race."""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if lock_pid(root) is not None:
                return False
            path.unlink(missing_ok=True)
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return True
    return False


def release_lock(root: Path) -> None:
    _lock_path(root).unlink(missing_ok=True)


def run_daemon(root: Path) -> None:
    raise NotImplementedError("run_daemon lands in the daemon core-loop task")
