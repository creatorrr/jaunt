from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import jaunt.cli
from jaunt import daemon, jobs


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []
        self._events: list[tuple[float, Callable[[], None]]] = []

    def __call__(self) -> float:
        return self.now

    def add_event(self, at: float, callback: Callable[[], None]) -> None:
        self._events.append((at, callback))
        self._events.sort(key=lambda event: event[0])

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        ready = [event for event in self._events if event[0] <= self.now]
        self._events = [event for event in self._events if event[0] > self.now]
        for _at, callback in ready:
            callback()


def _write_config(root: Path, *, poll_interval: float = 1.0) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "jaunt.toml").write_text(
        f"version = 1\n\n[daemon]\npoll_interval = {poll_interval}\n",
        encoding="utf-8",
    )


def _new_job(root: Path, *, module: str = "app") -> jobs.JobRecord:
    job = jobs.JobRecord.new(
        module=module, spec_digest=f"d-{module}", base_commit="c", branch="main"
    )
    jobs.save_job(root, job)
    return job


def _wait_args(root: Path, *args: str):
    return jaunt.cli.parse_args(["jobs", "--root", str(root), "wait", *args])


def _run_wait(root: Path, *args: str, clock: FakeClock | None = None) -> int:
    fake = clock or FakeClock()
    return jaunt.cli._cmd_jobs_wait(_wait_args(root, *args), clock=fake, sleep=fake.sleep)


def _daemon_running(monkeypatch: pytest.MonkeyPatch, running: bool) -> None:
    monkeypatch.setattr(daemon, "probe_lock", lambda _root: (running, 1234 if running else None))


def test_parse_jobs_wait_flags() -> None:
    ns = jaunt.cli.parse_args(
        [
            "jobs",
            "wait",
            "abc123",
            "--timeout",
            "30",
            "--settle",
            "0",
            "--progress",
            "plain",
            "--no-progress",
        ]
    )

    assert ns.command == "jobs"
    assert ns.jobs_command == "wait"
    assert ns.job_id == "abc123"
    assert ns.timeout == 30.0
    assert ns.settle == 0.0
    assert ns.progress == "plain"
    assert ns.no_progress is True
    assert jaunt.cli._resolve_progress_mode(ns, json_mode=False) is None


@pytest.mark.parametrize("value", ["0", "-1"])
def test_jobs_wait_rejects_nonpositive_timeout(value: str) -> None:
    assert (
        jaunt.cli.main(["jobs", "wait", "--timeout", value]) == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    )


def test_jobs_wait_target_resolves_when_job_leaves_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path)
    _daemon_running(monkeypatch, True)
    job = _new_job(tmp_path)
    clock = FakeClock()
    clock.add_event(1.0, lambda: jobs.mark(tmp_path, job, jobs.LANDED, landed_commit="abc"))

    rc = _run_wait(tmp_path, job.id, "--progress", "none", clock=clock)

    assert rc == jaunt.cli.EXIT_OK
    assert clock.sleeps == [1.0]
    assert capsys.readouterr().err == ""


def test_jobs_wait_idle_resolves_after_settle_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, poll_interval=1.0)
    jobs.jobs_dir(tmp_path).mkdir(parents=True)
    _daemon_running(monkeypatch, True)
    clock = FakeClock()

    rc = _run_wait(tmp_path, "--settle", "2", "--progress", "none", clock=clock)

    assert rc == jaunt.cli.EXIT_OK
    assert clock.sleeps == [1.0, 1.0]
    assert clock.now == 2.0


def test_jobs_wait_missing_jobs_dir_resolves_after_settle_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_config(tmp_path, poll_interval=1.0)
    _daemon_running(monkeypatch, True)
    clock = FakeClock()

    rc = _run_wait(tmp_path, "--settle", "2", "--progress", "none", clock=clock)

    assert rc == jaunt.cli.EXIT_OK
    assert clock.sleeps == [1.0, 1.0]
    assert clock.now == 2.0


def test_jobs_wait_settle_catches_new_job_enqueued_inside_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path, poll_interval=1.0)
    jobs.jobs_dir(tmp_path).mkdir(parents=True)
    _daemon_running(monkeypatch, True)
    clock = FakeClock()
    created: dict[str, jobs.JobRecord] = {}

    def enqueue() -> None:
        created["job"] = _new_job(tmp_path, module="late")

    def land() -> None:
        jobs.mark(tmp_path, created["job"], jobs.LANDED, landed_commit="abc")

    clock.add_event(1.0, enqueue)
    clock.add_event(2.0, land)

    rc = _run_wait(
        tmp_path,
        "--settle",
        "2",
        "--json",
        "--progress",
        "none",
        clock=clock,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload == {
        "command": "jobs",
        "action": "wait",
        "ok": True,
        "timed_out": False,
        "jobs": [
            {
                "id": created["job"].id,
                "module": "late",
                "state": "landed",
                "phase": "",
                "error": "",
            }
        ],
    }
    assert clock.now == 4.0


def test_jobs_wait_observes_terminal_job_created_between_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path, poll_interval=1.0)
    _daemon_running(monkeypatch, True)
    clock = FakeClock()
    monkeypatch.setattr(jaunt.cli.time, "time", lambda: clock.now)
    created: dict[str, jobs.JobRecord] = {}

    def create_and_fail() -> None:
        job = _new_job(tmp_path, module="fast")
        created["job"] = jobs.mark(tmp_path, job, jobs.FAILED, error="boom")

    clock.add_event(0.5, create_and_fail)

    rc = _run_wait(
        tmp_path,
        "--settle",
        "2",
        "--json",
        "--progress",
        "none",
        clock=clock,
    )

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_PYTEST_FAILURE
    assert payload == {
        "command": "jobs",
        "action": "wait",
        "ok": False,
        "timed_out": False,
        "jobs": [
            {
                "id": created["job"].id,
                "module": "fast",
                "state": "failed",
                "phase": "",
                "error": "boom",
            }
        ],
    }
    assert clock.sleeps == [1.0, 1.0]


@pytest.mark.parametrize("state", [jobs.FAILED, jobs.PARKED])
def test_jobs_wait_terminal_attention_states_exit_4(
    tmp_path: Path, state: str, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path)
    job = _new_job(tmp_path)
    terminal = jobs.mark(tmp_path, job, state, error="needs attention")

    rc = _run_wait(tmp_path, terminal.id, "--json", "--progress", "none")

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_PYTEST_FAILURE
    assert payload["ok"] is False
    assert payload["timed_out"] is False
    assert payload["jobs"][0]["state"] == state
    assert payload["jobs"][0]["error"] == "needs attention"


def test_jobs_wait_timeout_exits_5(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    _daemon_running(monkeypatch, True)
    job = _new_job(tmp_path)
    clock = FakeClock()

    rc = _run_wait(
        tmp_path,
        job.id,
        "--timeout",
        "2.5",
        "--json",
        "--progress",
        "none",
        clock=clock,
    )

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == jaunt.cli.EXIT_TIMEOUT
    assert "timed out waiting for jobs" in captured.err
    assert payload["ok"] is False
    assert payload["timed_out"] is True
    assert payload["jobs"][0]["state"] == jobs.QUEUED
    assert clock.sleeps == [1.0, 1.0, 0.5]


def test_jobs_wait_daemon_dead_with_active_records_exits_2(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    _daemon_running(monkeypatch, False)
    _new_job(tmp_path)

    rc = _run_wait(tmp_path, "--json", "--progress", "none")

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    assert "daemon died mid-job" in captured.err
    assert payload["ok"] is False
    assert payload["timed_out"] is False
    assert payload["jobs"][0]["state"] == jobs.QUEUED


def test_jobs_wait_daemon_dead_with_no_jobs_is_nothing_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path)
    _daemon_running(monkeypatch, False)

    rc = _run_wait(tmp_path, "--json")

    payload = json.loads(capsys.readouterr().out)
    assert rc == jaunt.cli.EXIT_OK
    assert payload == {
        "command": "jobs",
        "action": "wait",
        "ok": True,
        "timed_out": False,
        "jobs": [],
    }


def test_jobs_wait_unknown_target_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_config(tmp_path)

    rc = _run_wait(tmp_path, "missing")

    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    assert "error: job not found: missing" in capsys.readouterr().err


def test_jobs_wait_streams_plain_lines_on_state_and_phase_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_config(tmp_path)
    _daemon_running(monkeypatch, True)
    job = _new_job(tmp_path)
    clock = FakeClock()
    running: dict[str, jobs.JobRecord] = {}

    def mark_running() -> None:
        running["job"] = jobs.mark(tmp_path, job, jobs.RUNNING, phase="generating")

    def mark_landed() -> None:
        jobs.mark(tmp_path, running["job"], jobs.LANDED, landed_commit="abc")

    clock.add_event(1.0, mark_running)
    clock.add_event(2.0, mark_landed)

    rc = _run_wait(tmp_path, job.id, "--progress", "plain", clock=clock)

    err = capsys.readouterr().err
    assert rc == jaunt.cli.EXIT_OK
    assert f"[wait] {job.id} app: queued" in err
    assert f"[wait] {job.id} app: running — generating" in err
    assert f"[wait] {job.id} app: landed" in err
