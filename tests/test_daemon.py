from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

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


def test_run_once_full_cycle_lands_and_journals(repo: Path, jaunt_cfg: JauntConfig) -> None:
    (repo / journal.JOURNAL_FILE).touch()
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


def test_probe_failure_is_loud_and_non_destructive(repo: Path, jaunt_cfg: JauntConfig) -> None:
    (repo / journal.JOURNAL_FILE).touch()
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
    (repo / journal.JOURNAL_FILE).touch()

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
    assert "regen" not in _git(repo, "log", "-1", "--format=%s")


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
