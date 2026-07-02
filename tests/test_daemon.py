from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from jaunt import cli, daemon, jobs, journal
from jaunt.config import JauntConfig, load_config


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text('"""spec v1"""\n', encoding="utf-8")
    (r / ".gitignore").write_text(".jaunt/\n", encoding="utf-8")
    (r / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
        encoding="utf-8",
    )
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


@pytest.fixture()
def jaunt_cfg(repo: Path) -> JauntConfig:
    return load_config(root=repo)


@pytest.fixture()
def jaunt_cfg_with_notify(repo: Path, tmp_path: Path) -> JauntConfig:
    notify_path = tmp_path / "notify.txt"
    notify_command = f"echo $JAUNT_JOB_MODULE:$JAUNT_JOB_STATE >> {notify_path.as_posix()}"
    (repo / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                "",
                "[daemon]",
                f"notify_command = '{notify_command}'",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return load_config(root=repo)


class FakeRunner:
    """Stale until built; probe returns a controllable digest so tests can supersede."""

    def __init__(self, module: str = "app", change: str = "prose") -> None:
        self.module = module
        self.change = change
        self.digest = "digest-v1"
        self.built: list[str] = []

    def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
        if self.built:
            return {}, {}
        return {self.module: self.change}, {self.module: self.digest}

    def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
        gen = worktree / "src" / "__generated__"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / f"{module}.py").write_text("generated = True\n", encoding="utf-8")
        self.built.append(module)
        return daemon.BuildOutcome(ok=True, refrozen=False)

    def gate(self, worktree: Path, module: str) -> daemon.GateOutcome:
        return daemon.GateOutcome(ok=True, battery="3/3")


def _spec_commit(repo: Path, body: str = '"""spec v2"""\n') -> None:
    (repo / "src" / "app.py").write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "spec commit")


def _opt_into_journal(repo: Path) -> None:
    (repo / journal.JOURNAL_FILE).touch()
    _git(repo, "add", "--", journal.JOURNAL_FILE)
    _git(repo, "commit", "-m", "opt into journal")


def _wait_for_marker(path: Path, proc: subprocess.Popen[str], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            pytest.fail(
                f"child exited before writing {path}: rc={proc.returncode} "
                f"stdout={stdout!r} stderr={stderr!r}"
            )
        time.sleep(0.05)
    pytest.fail(f"timed out waiting for {path}")


def _install_failing_pre_commit(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def _cycle(
    repo: Path,
    cfg: JauntConfig,
    state: daemon.DaemonState,
    runner: FakeRunner,
    pool: ThreadPoolExecutor,
    n: int = 3,
) -> None:
    """Drive run_once to quiescence: probe/spawn, drain, collect/land."""
    for _ in range(n):
        daemon.run_once(repo, cfg, state, runner, pool)
        daemon.drain(state)


def test_lock_acquire_release(tmp_path: Path) -> None:
    lockfile = tmp_path / ".jaunt" / "daemon.pid"
    handle = daemon.acquire_lock(tmp_path)
    assert handle is not None
    try:
        st = os.fstat(handle.fd)
        pst = os.stat(lockfile)
        assert (pst.st_dev, pst.st_ino) == (st.st_dev, st.st_ino)
        assert lockfile.read_text(encoding="utf-8") == f"{os.getpid()}\n"
    finally:
        daemon.release_lock(handle)
    assert not lockfile.exists()


def test_lock_cross_process_contention_fails(tmp_path: Path) -> None:
    handle = daemon.acquire_lock(tmp_path)
    assert handle is not None
    try:
        code = """
import sys
from pathlib import Path
from jaunt import daemon

handle = daemon.acquire_lock(Path(sys.argv[1]))
if handle is None:
    print("blocked")
    raise SystemExit(0)
daemon.release_lock(handle)
print("acquired")
raise SystemExit(1)
"""
        proc = subprocess.run(
            [sys.executable, "-c", code, str(tmp_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        daemon.release_lock(handle)

    assert proc.returncode == 0
    assert proc.stdout.strip() == "blocked"


def test_release_lock_allows_subsequent_acquire(tmp_path: Path) -> None:
    handle = daemon.acquire_lock(tmp_path)
    assert handle is not None
    daemon.release_lock(handle)

    next_handle = daemon.acquire_lock(tmp_path)
    assert next_handle is not None
    daemon.release_lock(next_handle)


def test_probe_ignores_unlocked_live_pidfile(tmp_path: Path, capsys, monkeypatch) -> None:
    lock = tmp_path / ".jaunt" / "daemon.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    killed: list[tuple[int, int]] = []

    def kill_spy(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        pytest.fail(f"attempted to signal pid {pid}")

    monkeypatch.setattr(os, "kill", kill_spy)

    assert daemon.probe_lock(tmp_path) == (False, None)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(f"{os.getpid()}\n", encoding="utf-8")
    rc = cli.main(["daemon", "stop", "--root", str(tmp_path)])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Daemon not running."
    assert killed == []


def test_daemon_stop_empty_unlocked_lockfile_reports_not_running(
    tmp_path: Path, capsys, monkeypatch
) -> None:
    lock = tmp_path / ".jaunt" / "daemon.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text("", encoding="utf-8")
    killed: list[tuple[int, int]] = []

    def kill_spy(pid: int, sig: int) -> None:
        killed.append((pid, sig))
        pytest.fail(f"attempted to signal pid {pid}")

    monkeypatch.setattr(os, "kill", kill_spy)

    rc = cli.main(["daemon", "stop", "--root", str(tmp_path)])

    assert rc == 0
    assert capsys.readouterr().out.strip() == "Daemon not running."
    assert killed == []


def test_daemon_stop_signals_pid_from_locked_file(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "ready.txt"
    code = """
import os
import sys
import time
from pathlib import Path
from jaunt import daemon

root = Path(sys.argv[1])
marker = Path(sys.argv[2])
handle = daemon.acquire_lock(root)
if handle is None:
    raise SystemExit(2)
marker.write_text(str(os.getpid()), encoding="utf-8")
try:
    time.sleep(60)
finally:
    daemon.release_lock(handle)
"""
    child = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path), str(marker)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    real_kill = os.kill
    killed: list[tuple[int, int]] = []

    def kill_spy(pid: int, sig: int) -> None:
        killed.append((pid, sig))

    try:
        _wait_for_marker(marker, child)
        child_pid = int(marker.read_text(encoding="utf-8"))
        monkeypatch.setattr(os, "kill", kill_spy)

        rc = cli.main(["daemon", "stop", "--root", str(tmp_path)])

        assert rc == 0
        assert capsys.readouterr().out.strip() == f"Sent SIGTERM to daemon (pid {child_pid})."
        assert killed == [(child_pid, signal.SIGTERM)]
    finally:
        monkeypatch.setattr(os, "kill", real_kill)
        if child.poll() is None:
            real_kill(child.pid, signal.SIGTERM)
        child.wait(timeout=5)


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


def test_run_once_full_cycle_lands_and_journals(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert len(landed) == 1 and landed[0].module == "app"
    assert landed[0].spec_digest == "digest-v1"
    assert "regen(app)" in _git(repo, "log", "-1", "--format=%s")
    assert any(
        "build" in line and "app" in line and "3/3" in line for line in journal.read_lines(repo)
    )
    assert (repo / "src" / "__generated__" / "app.py").exists()


def test_run_once_spawn_false_leaves_queued_job_unsubmitted(
    repo: Path, jaunt_cfg: JauntConfig
) -> None:
    job = jobs.JobRecord.new(
        module="app",
        spec_digest="digest-v1",
        base_commit=_git(repo, "rev-parse", "HEAD"),
        branch="main",
    )
    jobs.save_job(repo, job)
    state = daemon.DaemonState()

    with ThreadPoolExecutor(max_workers=1) as pool:
        daemon.run_once(repo, jaunt_cfg, state, FakeRunner(), pool, spawn=False)

    loaded = jobs.load_job(repo, job.id)
    assert loaded is not None
    assert loaded.state == jobs.QUEUED
    assert state.futures == {}


def test_run_once_default_spawn_starts_queued_job(repo: Path, jaunt_cfg: JauntConfig) -> None:
    job = jobs.JobRecord.new(
        module="app",
        spec_digest="digest-v1",
        base_commit=_git(repo, "rev-parse", "HEAD"),
        branch="main",
    )
    jobs.save_job(repo, job)
    state = daemon.DaemonState()

    with ThreadPoolExecutor(max_workers=1) as pool:
        daemon.run_once(repo, jaunt_cfg, state, FakeRunner(), pool)

    loaded = jobs.load_job(repo, job.id)
    assert loaded is not None
    assert loaded.state == jobs.RUNNING
    assert set(state.futures) == {job.id}


def test_run_daemon_shutdown_collects_and_lands_spawned_job(repo: Path) -> None:
    _spec_commit(repo)

    daemon.run_daemon(repo, runner=FakeRunner(), iterations=1, sleep=lambda _: None)

    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert len(landed) == 1 and landed[0].module == "app"
    assert not jobs.list_jobs(repo, states={jobs.RUNNING})
    assert (repo / "src" / "__generated__" / "app.py").exists()


def test_landed_regen_commit_includes_jaunt_log(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert "src/__generated__/app.py" in committed
    assert journal.JOURNAL_FILE in committed
    blob = _git(repo, "show", f"HEAD:{journal.JOURNAL_FILE}")
    assert "build" in blob and "app" in blob
    assert _git(repo, "status", "--porcelain") == ""


def test_worker_journal_change_is_ignored_and_job_lands(
    repo: Path, jaunt_cfg: JauntConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    _opt_into_journal(repo)

    class WorkerJournalRunner(FakeRunner):
        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            outcome = super().build(worktree, module)
            with open(worktree / journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
                f.write("worker-build journal line\n")
            return outcome

    captured_patch_paths: list[list[str]] = []
    original_land = daemon.landing.land

    def land_spy(*args: Any, **kwargs: Any) -> str | None:
        captured_patch_paths.append(list(kwargs["patch_paths"]))
        return original_land(*args, **kwargs)

    monkeypatch.setattr(daemon.landing, "land", land_spy)

    runner = WorkerJournalRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    assert captured_patch_paths
    assert journal.JOURNAL_FILE not in captured_patch_paths[0]
    assert not jobs.list_jobs(repo, states={jobs.PARKED})
    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert len(landed) == 1 and landed[0].module == "app"

    committed = set(_git(repo, "show", "--name-only", "--format=", "HEAD").splitlines())
    assert "src/__generated__/app.py" in committed
    assert journal.JOURNAL_FILE in committed

    blob = _git(repo, "show", f"HEAD:{journal.JOURNAL_FILE}")
    lines = blob.splitlines()
    assert len(lines) == 1
    assert "build" in lines[0] and "app" in lines[0] and "3/3" in lines[0]
    assert "worker-build" not in blob


def test_dirty_jaunt_log_defers_landing_without_appending(
    repo: Path, jaunt_cfg: JauntConfig
) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    user_line = "manual user journal line\n"
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        head_before = _git(repo, "rev-parse", "HEAD")
        log_count_before = _git(repo, "rev-list", "--count", "HEAD")
        with open(repo / journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write(user_line)

        _cycle(repo, jaunt_cfg, state, runner, pool)

    assert _git(repo, "rev-parse", "HEAD") == head_before
    assert _git(repo, "rev-list", "--count", "HEAD") == log_count_before
    assert (repo / journal.JOURNAL_FILE).read_text(encoding="utf-8") == user_line
    assert not jobs.list_jobs(repo, states={jobs.LANDED, jobs.PARKED, jobs.FAILED})
    active = jobs.list_jobs(repo, states={jobs.RUNNING, jobs.GREEN})
    assert len(active) == 1 and active[0].module == "app"
    assert set(state.pending) == {active[0].id}


def test_landing_retries_after_user_commits_dirty_jaunt_log(
    repo: Path, jaunt_cfg: JauntConfig
) -> None:
    class StillStaleRunner(FakeRunner):
        def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
            return {self.module: self.change}, {self.module: self.digest}

    _opt_into_journal(repo)
    runner = StillStaleRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        with open(repo / journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write("manual user journal line\n")
        _cycle(repo, jaunt_cfg, state, runner, pool)

        _git(repo, "add", "--", journal.JOURNAL_FILE)
        _git(repo, "commit", "-m", "user journal note")
        previous_lines = _git(repo, "show", f"HEAD:{journal.JOURNAL_FILE}").splitlines()

        daemon.run_once(repo, jaunt_cfg, state, runner, pool)

    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert len(landed) == 1 and landed[0].module == "app"
    current_lines = _git(repo, "show", f"HEAD:{journal.JOURNAL_FILE}").splitlines()
    appended = current_lines[len(previous_lines) :]
    assert len(appended) == 1
    assert "build" in appended[0] and "app" in appended[0] and "3/3" in appended[0]
    assert "manual user journal line" in _git(repo, "show", f"HEAD~1:{journal.JOURNAL_FILE}")


def test_dirty_jaunt_log_deferred_notify_is_rate_limited(
    repo: Path, jaunt_cfg_with_notify: JauntConfig, tmp_path: Path
) -> None:
    notify_path = tmp_path / "notify.txt"
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        with open(repo / journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write("first manual line\n")
        _cycle(repo, jaunt_cfg_with_notify, state, runner, pool, n=5)

        lines = notify_path.read_text(encoding="utf-8").splitlines()
        assert lines.count("app:deferred") == 1

        _git(repo, "checkout", "--", journal.JOURNAL_FILE)
        daemon.run_once(repo, jaunt_cfg_with_notify, state, runner, pool)

        runner.digest = "digest-v2"
        runner.built = []
        _spec_commit(repo, body='"""spec v3"""\n')
        with open(repo / journal.JOURNAL_FILE, "a", encoding="utf-8") as f:
            f.write("second manual line\n")
        _cycle(repo, jaunt_cfg_with_notify, state, runner, pool, n=5)

    lines = notify_path.read_text(encoding="utf-8").splitlines()
    assert lines.count("app:deferred") == 2


def test_commit_failure_parks_without_killing_daemon(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _install_failing_pre_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    parked = jobs.list_jobs(repo, states={jobs.PARKED})
    assert parked and parked[0].module == "app"
    assert _git(repo, "status", "--porcelain", "--", "src/__generated__/app.py") == ""
    assert _git(repo, "diff", "--cached", "--name-only") == ""
    lines = journal.read_lines(repo, limit=0)
    assert any("job-park" in line and parked[0].id in line for line in lines)
    assert not any("build" in line and parked[0].id in line for line in lines)


def test_restart_preserves_parked_job(repo: Path, jaunt_cfg: JauntConfig) -> None:
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _install_failing_pre_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

        parked = jobs.list_jobs(repo, states={jobs.PARKED})[0]
        patch_path = jobs.jobs_dir(repo) / f"{parked.id}.patch"
        patch_content = patch_path.read_text(encoding="utf-8")

        runner.built = []
        restarted = daemon.DaemonState()
        daemon.run_once(repo, jaunt_cfg, restarted, runner, pool)
        daemon.drain(restarted)

    loaded = jobs.load_job(repo, parked.id)
    assert loaded is not None
    assert loaded.state == jobs.PARKED
    assert loaded.patch_paths == parked.patch_paths
    assert loaded.updated == parked.updated
    assert loaded.error == parked.error
    assert patch_path.read_text(encoding="utf-8") == patch_content
    assert [
        job for job in jobs.list_jobs(repo, states=jobs.ACTIVE_STATES) if job.module == "app"
    ] == []


def test_restart_supersedes_stale_parked_job_and_enqueues_fresh(
    repo: Path, jaunt_cfg: JauntConfig
) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _install_failing_pre_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

        parked = jobs.list_jobs(repo, states={jobs.PARKED})[0]
        runner.digest = "digest-v2"
        runner.built = []
        (repo / ".git" / "hooks" / "pre-commit").unlink()
        _spec_commit(repo, '"""spec v3"""\n')

        restarted = daemon.DaemonState()
        _cycle(repo, jaunt_cfg, restarted, runner, pool)

    loaded = jobs.load_job(repo, parked.id)
    assert loaded is not None
    assert loaded.state == jobs.SUPERSEDED
    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert any(job.module == "app" and job.spec_digest == "digest-v2" for job in landed)
    assert any(
        "job-supersede" in line and parked.id in line for line in journal.read_lines(repo, limit=0)
    )


def test_park_truncates_preappended_success_line(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        daemon.drain(state)
        before = (repo / journal.JOURNAL_FILE).read_text(encoding="utf-8")
        dirty = repo / "src" / "__generated__" / "app.py"
        dirty.parent.mkdir(parents=True, exist_ok=True)
        dirty.write_text("local edit\n", encoding="utf-8")
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)

    parked = jobs.list_jobs(repo, states={jobs.PARKED})
    assert parked and parked[0].module == "app"
    after = (repo / journal.JOURNAL_FILE).read_text(encoding="utf-8")
    appended = after.removeprefix(before).splitlines()
    assert len(appended) == 1
    assert "job-park" in appended[0] and parked[0].id in appended[0]
    assert "build" not in after


def test_notify_command_fires_with_env(
    repo: Path, jaunt_cfg_with_notify: JauntConfig, tmp_path: Path
) -> None:
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg_with_notify, state, runner, pool)
        daemon.drain(state)
        daemon.run_once(repo, jaunt_cfg_with_notify, state, runner, pool)

    text = (tmp_path / "notify.txt").read_text(encoding="utf-8")
    assert "app:landed" in text


def test_supersede_on_newer_spec_commit(repo: Path, jaunt_cfg: JauntConfig) -> None:
    event = threading.Event()

    class BlockingRunner(FakeRunner):
        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            event.wait(timeout=10)
            return super().build(worktree, module)

    runner = BlockingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        first = jobs.list_jobs(repo, states={jobs.RUNNING})[0]

        runner.digest = "digest-v2"
        _spec_commit(repo, '"""spec v3"""\n')
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        event.set()
        _cycle(repo, jaunt_cfg, state, runner, pool)

    loaded = jobs.load_job(repo, first.id)
    assert loaded is not None
    assert loaded.state == jobs.SUPERSEDED
    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert landed and landed[0].spec_digest == "digest-v2"
    assert not (repo / journal.JOURNAL_FILE).exists()


def test_active_supersede_on_newer_spec_commit_journals(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    event = threading.Event()

    class BlockingRunner(FakeRunner):
        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            event.wait(timeout=10)
            return super().build(worktree, module)

    runner = BlockingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        first = jobs.list_jobs(repo, states={jobs.RUNNING})[0]

        runner.digest = "digest-v2"
        _spec_commit(repo, '"""spec v3"""\n')
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        event.set()
        _cycle(repo, jaunt_cfg, state, runner, pool)

    supersede_lines = [
        line for line in journal.read_lines(repo, limit=0) if "job-supersede" in line
    ]
    assert len(supersede_lines) == 1
    assert "app" in supersede_lines[0]
    assert first.id in supersede_lines[0]


def test_probe_failure_is_loud_and_non_destructive(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)
    release = threading.Event()
    started = threading.Event()

    class ProbeFailingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.fail_probe = False

        def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
            if self.fail_probe:
                raise daemon.ProbeError("status exploded")
            return {self.module: self.change}, {self.module: self.digest}

        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            started.set()
            release.wait(timeout=10)
            return super().build(worktree, module)

    runner = ProbeFailingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        assert started.wait(timeout=5)
        first = jobs.list_jobs(repo, states={jobs.RUNNING})[0]
        last_successful_head = state.last_head

        runner.fail_probe = True
        _spec_commit(repo, '"""spec v3"""\n')
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)

        loaded = jobs.load_job(repo, first.id)
        assert loaded is not None
        assert loaded.state != jobs.SUPERSEDED
        assert state.last_head == last_successful_head
        probe_fail_lines = [line for line in journal.read_lines(repo) if "probe-fail" in line]
        assert len(probe_fail_lines) == 1

        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        probe_fail_lines = [line for line in journal.read_lines(repo) if "probe-fail" in line]
        assert len(probe_fail_lines) == 1

        runner.fail_probe = False
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        release.set()
        daemon.drain(state)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)

    loaded = jobs.load_job(repo, first.id)
    assert loaded is not None
    assert loaded.state == jobs.LANDED
    assert not jobs.list_jobs(repo, states={jobs.SUPERSEDED})


def test_cli_runner_probe_raises_on_failed_status(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = daemon.CliRunner()

    monkeypatch.setattr(runner, "_run", lambda *_args: (2, {"ok": True, "stale": []}))
    with pytest.raises(daemon.ProbeError):
        runner.probe(Path("/tmp/worktree"))

    monkeypatch.setattr(runner, "_run", lambda *_args: (0, {"ok": False, "error": "boom"}))
    with pytest.raises(daemon.ProbeError, match="boom"):
        runner.probe(Path("/tmp/worktree"))

    monkeypatch.setattr(
        runner,
        "_run",
        lambda *_args: (
            0,
            {
                "ok": True,
                "stale": ["m"],
                "stale_changes": {"m": "prose"},
                "digests": {"m": "d"},
            },
        ),
    )
    assert runner.probe(Path("/tmp/worktree")) == ({"m": "prose"}, {"m": "d"})


def test_failed_build_journals_and_marks_failed(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)

    class FailingRunner(FakeRunner):
        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            self.built.append(module)
            return daemon.BuildOutcome(ok=False, refrozen=False, error="codex exited 3")

    runner = FailingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    failed = jobs.list_jobs(repo, states={jobs.FAILED})
    assert failed and "codex exited 3" in failed[0].error
    assert any("job-fail" in line for line in journal.read_lines(repo))


def test_worker_exception_marks_failed_and_survives(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)

    class ExplodingRunner(FakeRunner):
        def build(self, worktree: Path, module: str) -> daemon.BuildOutcome:
            raise RuntimeError("boom")

    state = daemon.DaemonState()
    exploding = ExplodingRunner()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, exploding, pool)

        failed = jobs.list_jobs(repo, states={jobs.FAILED})
        assert failed and "RuntimeError: boom" in failed[0].error
        assert any("job-fail" in line for line in journal.read_lines(repo))

        healthy = FakeRunner()
        healthy.digest = "digest-v2"
        _spec_commit(repo, '"""spec v3"""\n')
        _cycle(repo, jaunt_cfg, state, healthy, pool)

    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert landed and landed[0].spec_digest == "digest-v2"
    assert (repo / "src" / "__generated__" / "app.py").exists()


def test_failed_gate_blocks_landing(repo: Path, jaunt_cfg: JauntConfig) -> None:
    class GateFailRunner(FakeRunner):
        def gate(self, worktree: Path, module: str) -> daemon.GateOutcome:
            return daemon.GateOutcome(ok=False, detail="jaunt check failed")

    runner = GateFailRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)

    assert not jobs.list_jobs(repo, states={jobs.LANDED})
    failed = jobs.list_jobs(repo, states={jobs.FAILED})
    assert failed and "check failed" in failed[0].error
    assert not (repo / "src" / "__generated__" / "app.py").exists()


def test_recover_orphans_and_prunes_worktrees(repo: Path, jaunt_cfg: JauntConfig) -> None:
    job = jobs.JobRecord.new(
        module="app",
        spec_digest="d",
        base_commit=_git(repo, "rev-parse", "HEAD"),
        branch="main",
    )
    jobs.save_job(repo, job)
    jobs.mark(repo, job, jobs.RUNNING)
    stray = repo / ".jaunt" / "worktrees" / "zombie"
    _git(repo, "worktree", "add", "--detach", str(stray), "HEAD")

    affected = daemon.recover(repo)

    assert job.id in affected
    recovered = jobs.load_job(repo, job.id)
    assert recovered is not None
    assert recovered.state == jobs.FAILED
    assert not stray.exists()


def test_supersede_when_module_vanishes_from_probe(repo: Path, jaunt_cfg: JauntConfig) -> None:
    """A spec deleted mid-job must supersede the job, never land its stale patch."""

    class VanishingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.vanished = False

        def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
            if self.vanished:
                return {}, {}
            return super().probe(worktree)

    runner = VanishingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)  # enqueue + spawn
        daemon.drain(state)  # job future completes, not yet collected
        runner.vanished = True  # spec removed: module no longer stale
        _spec_commit(repo, body="# spec deleted\n")
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)  # probe supersedes before landing
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
    superseded = jobs.list_jobs(repo, states={jobs.SUPERSEDED})
    assert superseded and superseded[0].module == "app"
    assert not jobs.list_jobs(repo, states={jobs.LANDED})
    assert not (repo / "src" / "__generated__" / "app.py").exists()
    assert not (repo / journal.JOURNAL_FILE).exists()
    assert "regen" not in _git(repo, "log", "-1", "--format=%s")


def test_vanished_module_supersede_journals(repo: Path, jaunt_cfg: JauntConfig) -> None:
    _opt_into_journal(repo)

    class VanishingRunner(FakeRunner):
        def __init__(self) -> None:
            super().__init__()
            self.vanished = False

        def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
            if self.vanished:
                return {}, {}
            return super().probe(worktree)

    runner = VanishingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        daemon.drain(state)
        first = jobs.list_jobs(repo, states={jobs.RUNNING})[0]
        runner.vanished = True
        _spec_commit(repo, body="# spec deleted\n")
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)

    supersede_lines = [
        line for line in journal.read_lines(repo, limit=0) if "job-supersede" in line
    ]
    assert len(supersede_lines) == 1
    assert "app" in supersede_lines[0]
    assert first.id in supersede_lines[0]


def test_jaunt_dir_ignored_creates_missing_dir(repo: Path) -> None:
    jaunt_dir = repo / ".jaunt"
    if jaunt_dir.exists():
        shutil.rmtree(jaunt_dir)
    assert daemon.jaunt_dir_ignored(repo) is True  # rule matches even from a fresh clone
    assert jaunt_dir.is_dir()  # created as a side effect


def test_jaunt_dir_ignored_false_without_rule(tmp_path: Path) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    _git(bare, "init", "-b", "main")
    _git(bare, "config", "user.email", "t@example.com")
    _git(bare, "config", "user.name", "T")
    assert daemon.jaunt_dir_ignored(bare) is False
