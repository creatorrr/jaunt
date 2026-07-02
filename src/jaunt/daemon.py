"""Background daemon: lockfile, poll loop, job scheduling, landing."""

from __future__ import annotations

import json as _json
import os
import shutil
import subprocess
import sys
import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from jaunt import jobs as jobs_mod
from jaunt import journal as journal_mod
from jaunt import landing
from jaunt.config import JauntConfig

DISABLE_ENV = "JAUNT_DAEMON_DISABLE"


@dataclass(frozen=True)
class BuildOutcome:
    ok: bool
    refrozen: bool
    error: str = ""


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    battery: str = "-"
    detail: str = ""


@dataclass
class JobResult:
    job_id: str
    build: BuildOutcome
    gate: GateOutcome | None = None
    patch: str = ""
    patch_paths: tuple[str, ...] = ()


class Runner(Protocol):
    def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]: ...

    def build(self, worktree: Path, module: str) -> BuildOutcome: ...

    def gate(self, worktree: Path, module: str) -> GateOutcome: ...


@dataclass
class DaemonState:
    last_head: str = ""
    futures: dict[str, Future[JobResult]] = field(default_factory=dict)
    pending: dict[str, JobResult] = field(default_factory=dict)


def drain(state: DaemonState) -> None:
    for fut in list(state.futures.values()):
        fut.result()


class CliRunner:
    """Default runner: drives jaunt's own CLI JSON contracts in a worktree."""

    def _run(self, worktree: Path, *argv: str) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "jaunt", *argv],
            cwd=worktree,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            return _json.loads(proc.stdout or "{}")
        except _json.JSONDecodeError:
            return {"ok": False, "error": (proc.stderr or proc.stdout)[-500:]}

    def probe(self, worktree: Path) -> tuple[dict[str, str], dict[str, str]]:
        payload = self._run(worktree, "status", "--json", "--magic-only")
        stale = payload.get("stale", [])
        changes = payload.get("stale_changes", {})
        digests = payload.get("digests", {})
        return {m: changes.get(m, "structural") for m in stale}, digests

    def build(self, worktree: Path, module: str) -> BuildOutcome:
        payload = self._run(
            worktree,
            "build",
            "--target",
            module,
            "--json",
            "--no-repo-map",
            "--no-auto-skills",
        )
        if module in payload.get("refrozen", []):
            return BuildOutcome(ok=True, refrozen=True)
        if module in payload.get("generated", []):
            return BuildOutcome(ok=True, refrozen=False)
        error = str(payload.get("failed", {}).get(module, payload.get("error", "build failed")))
        return BuildOutcome(ok=False, refrozen=False, error=error.splitlines()[0][:200])

    def gate(self, worktree: Path, module: str) -> GateOutcome:
        payload = self._run(worktree, "check", "--json")
        checked = payload.get("checked", [])
        blocked = payload.get("blocked", [])
        if payload.get("ok", False):
            battery = f"{len(checked) - len(blocked)}/{len(checked)}" if checked else "-"
            return GateOutcome(ok=True, battery=battery)
        detail = str(payload.get("error", "jaunt check failed")).splitlines()[0][:200]
        return GateOutcome(ok=False, detail=detail)


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


def jaunt_dir_ignored(root: Path) -> bool:
    """True if ``.jaunt/`` is gitignored.

    Creates the directory first: dir-only ignore rules (``.jaunt/``) do not match a
    path that does not exist on disk, so a freshly initialized project would fail the
    check before its first daemon run. The daemon needs the directory anyway.
    """
    (root / ".jaunt").mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["git", "-C", str(root), "check-ignore", "-q", ".jaunt"],
        capture_output=True,
        check=False,
    )
    return proc.returncode == 0


def _head(repo: Path) -> str:
    return landing.git_out(repo, "rev-parse", "HEAD").strip()


def _branch(repo: Path) -> str:
    return landing.git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _worktrees_dir(root: Path) -> Path:
    return root / ".jaunt" / "worktrees"


def _remove_worktree(root: Path, path: Path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )


def recover(root: Path) -> list[str]:
    affected = []
    for job in jobs_mod.list_jobs(root, states={jobs_mod.RUNNING, jobs_mod.GREEN}):
        jobs_mod.mark(root, job, jobs_mod.FAILED, error="orphaned by daemon restart")
        affected.append(job.id)

    wt_dir = _worktrees_dir(root)
    if wt_dir.exists():
        for path in wt_dir.iterdir():
            _remove_worktree(root, path)
            if path.exists():
                if path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
                else:
                    path.unlink(missing_ok=True)

    subprocess.run(
        ["git", "-C", str(root), "worktree", "prune"],
        capture_output=True,
        text=True,
        check=False,
    )
    return affected


def _execute_job(
    root: Path, cfg: JauntConfig, job: jobs_mod.JobRecord, runner: Runner
) -> JobResult:
    wt = _worktrees_dir(root) / job.id
    landing.git_out(root, "worktree", "add", "--detach", str(wt), job.base_commit)
    try:
        build = runner.build(wt, job.module)
        if not build.ok:
            return JobResult(job_id=job.id, build=build)
        gate = runner.gate(wt, job.module)
        if not gate.ok:
            return JobResult(job_id=job.id, build=build, gate=gate)
        gen = cfg.paths.generated_dir

        def _machine_owned(path: str) -> bool:
            return f"/{gen}/" in f"/{path}" or path == journal_mod.JOURNAL_FILE

        patch = landing.extract_patch(wt, job.base_commit, is_allowed=_machine_owned)
        paths = tuple(landing.changed_paths(wt, job.base_commit))
        return JobResult(job_id=job.id, build=build, gate=gate, patch=patch, patch_paths=paths)
    finally:
        _remove_worktree(root, wt)


def _probe_head(root: Path, head: str, runner: Runner) -> tuple[dict[str, str], dict[str, str]]:
    probe = _worktrees_dir(root) / "probe"
    _remove_worktree(root, probe)
    landing.git_out(root, "worktree", "add", "--detach", str(probe), head)
    try:
        return runner.probe(probe)
    finally:
        _remove_worktree(root, probe)


def _collect_finished(state: DaemonState, root: Path) -> None:
    for job_id, fut in list(state.futures.items()):
        if not fut.done():
            continue
        del state.futures[job_id]
        result = fut.result()
        job = jobs_mod.load_job(root, job_id)
        green = result.build.ok and (result.gate is None or result.gate.ok)
        if job is not None and job.state == jobs_mod.RUNNING and green:
            jobs_mod.mark(root, job, jobs_mod.GREEN)
        state.pending[job_id] = result


def _notify(cfg: JauntConfig, job: jobs_mod.JobRecord) -> None:
    if not cfg.daemon.notify_command:
        return
    env = dict(
        os.environ,
        JAUNT_JOB_ID=job.id,
        JAUNT_JOB_MODULE=job.module,
        JAUNT_JOB_STATE=job.state,
    )
    try:
        subprocess.run(
            cfg.daemon.notify_command,
            shell=True,
            env=env,
            timeout=10,
            check=False,
        )
    except Exception:
        pass


def _land_pending(root: Path, cfg: JauntConfig, state: DaemonState) -> None:
    for job_id, result in list(state.pending.items()):
        job = jobs_mod.load_job(root, job_id)
        if job is None or job.state not in (jobs_mod.RUNNING, jobs_mod.GREEN):
            del state.pending[job_id]
            continue
        if not result.build.ok or (result.gate is not None and not result.gate.ok):
            detail = result.build.error or (result.gate.detail if result.gate else "gate failed")
            updated = jobs_mod.mark(root, job, jobs_mod.FAILED, error=detail)
            _notify(cfg, updated)
            journal_mod.append_events(
                root, [journal_mod.JournalEvent("job-fail", job.module, detail, job.id)]
            )
            del state.pending[job_id]
            continue

        cause = "cosmetic (gate: EQUIVALENT)" if result.build.refrozen else "spec change"
        message = landing.build_commit_message(job.module, cause, job.id, job.spec_digest)
        sha = landing.land(
            root,
            result.patch,
            patch_paths=list(result.patch_paths),
            message=message,
            expected_branch=job.branch,
            expected_head=state.last_head,
        )
        if sha == landing.HEAD_MOVED:
            continue
        del state.pending[job_id]
        if sha is None:
            (jobs_mod.jobs_dir(root) / f"{job.id}.patch").write_text(result.patch, encoding="utf-8")
            updated = jobs_mod.mark(
                root,
                job,
                jobs_mod.PARKED,
                patch_paths=_json.dumps(list(result.patch_paths)),
            )
            _notify(cfg, updated)
            journal_mod.append_events(
                root, [journal_mod.JournalEvent("job-park", job.module, "landing conflict", job.id)]
            )
        else:
            state.last_head = sha
            action = "refreeze" if result.build.refrozen else "build"
            battery = result.gate.battery if result.gate else "-"
            updated = jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha)
            _notify(cfg, updated)
            journal_mod.append_events(
                root,
                [
                    journal_mod.JournalEvent(
                        action, job.module, f"{cause}; battery {battery}", job.id
                    )
                ],
            )


def run_once(
    root: Path,
    cfg: JauntConfig,
    state: DaemonState,
    runner: Runner,
    pool: Executor,
) -> None:
    head = _head(root)
    if head != state.last_head:
        state.last_head = head
        stale, digests = _probe_head(root, head, runner)
        branch = _branch(root)
        for module, _change in sorted(stale.items()):
            digest = digests.get(module, "")
            existing = jobs_mod.active_for_module(root, module)
            if existing is not None:
                if existing.spec_digest == digest:
                    continue
                jobs_mod.mark(root, existing, jobs_mod.SUPERSEDED)
                state.futures.pop(existing.id, None)
                state.pending.pop(existing.id, None)
            job = jobs_mod.JobRecord.new(
                module=module, spec_digest=digest, base_commit=head, branch=branch
            )
            jobs_mod.save_job(root, job)

        # A module absent from the latest probe is no longer stale at this HEAD
        # (spec deleted/ejected/reverted): its in-flight job must never land.
        for job in jobs_mod.list_jobs(root, states=jobs_mod.ACTIVE_STATES):
            if job.module not in stale:
                jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)
                state.futures.pop(job.id, None)
                state.pending.pop(job.id, None)

    _collect_finished(state, root)
    _land_pending(root, cfg, state)

    max_jobs = cfg.daemon.max_jobs or cfg.build.jobs
    for job in jobs_mod.list_jobs(root, states={jobs_mod.QUEUED}):
        if len(state.futures) >= max_jobs:
            break
        running = jobs_mod.mark(root, job, jobs_mod.RUNNING)
        state.futures[running.id] = pool.submit(_execute_job, root, cfg, running, runner)


def run_daemon(
    root: Path,
    *,
    runner: Runner | None = None,
    iterations: int | None = None,
    sleep=time.sleep,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from jaunt.config import load_config

    cfg = load_config(root=root)
    recover(root)
    daemon_runner = runner or CliRunner()
    state = DaemonState()
    max_jobs = cfg.daemon.max_jobs or cfg.build.jobs
    count = 0
    with ThreadPoolExecutor(max_workers=max_jobs) as pool:
        while iterations is None or count < iterations:
            if os.environ.get(DISABLE_ENV):
                break
            run_once(root, cfg, state, daemon_runner, pool)
            count += 1
            sleep(cfg.daemon.poll_interval)
        drain(state)
        run_once(root, cfg, state, daemon_runner, pool)
