from __future__ import annotations

import json
import os
from pathlib import Path

from jaunt import cli, daemon


def test_lock_acquire_release(tmp_path: Path) -> None:
    assert daemon.acquire_lock(tmp_path) is True
    assert daemon.lock_pid(tmp_path) == os.getpid()
    assert daemon.acquire_lock(tmp_path) is False
    daemon.release_lock(tmp_path)
    assert daemon.lock_pid(tmp_path) is None


def test_stale_lock_is_reclaimed(tmp_path: Path) -> None:
    lock = tmp_path / ".jaunt" / "daemon.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999999\n", encoding="utf-8")

    assert daemon.lock_pid(tmp_path) is None
    assert daemon.acquire_lock(tmp_path) is True
    daemon.release_lock(tmp_path)


def test_daemon_status_json_when_stopped(tmp_path: Path, capsys) -> None:
    assert cli.main(["daemon", "status", "--root", str(tmp_path), "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "daemon-status",
        "ok": True,
        "running": False,
        "pid": None,
        "jobs": [],
    }
