# Task: Daemon propose branch in `_land_pending` (jaunt 1.2.0 propose-only daemon)

You are implementing EXACTLY ONE task in the jaunt repo. Do NOT read or execute anything under ~/.claude/, ~/.codex/, .claude/, .agents/, or agents/.

Work only in `/home/diwank/github.com/creatorrr/jaunt-impl-propose-only`.

## Context

Jaunt's daemon runs background codegen jobs. When a job goes green, `_land_pending` in `src/jaunt/daemon.py` currently always auto-commits via `landing.land`. We are adding a **propose-only** mode: when `cfg.daemon.auto_commit` is False (the new default), a green job is parked as a `PROPOSED` proposal with its patch written to `.jaunt/jobs/<id>.patch`, instead of being committed.

Tasks 1 and 2 are already done:
- `cfg.daemon.auto_commit: bool` exists (default False).
- `jobs.PROPOSED = "proposed"`, `jobs.DISCARDED = "discarded"` exist; both in `PHASE_CLEAR_STATES`, neither in `ACTIVE_STATES`.
- `JobRecord.cause: str = ""` and `JobRecord.refrozen: str = ""` fields exist.
- `jobs.proposed_for_module(root, module) -> JobRecord | None` exists (mirror of `parked_for_module`).

## What to implement (Task 3)

### 1. Propose branch in `_land_pending` (`src/jaunt/daemon.py`)

`_land_pending` currently lives around lines 575-653. After the line:

    message = landing.build_commit_message(job.module, cause, job.id, job.spec_digest)

insert the propose branch (BEFORE the existing journal snapshot / `landing.land` block). When `not cfg.daemon.auto_commit`:

        if not cfg.daemon.auto_commit:
            (jobs_mod.jobs_dir(root) / f"{job.id}.patch").write_text(
                result.patch, encoding="utf-8"
            )
            prev = jobs_mod.proposed_for_module(root, job.module)
            if prev is not None and prev.id != job.id:
                jobs_mod.mark(root, prev, jobs_mod.SUPERSEDED)
                journal_mod.append_events(
                    root,
                    [
                        journal_mod.JournalEvent(
                            "job-supersede", prev.module, "newer proposal", prev.id
                        )
                    ],
                )
            updated = jobs_mod.mark(
                root,
                job,
                jobs_mod.PROPOSED,
                patch_paths=_json.dumps(list(result.patch_paths)),
                cause=cause,
                refrozen="1" if result.build.refrozen else "",
                battery=battery,
                phase="",
            )
            journal_mod.append_events(
                root,
                [
                    journal_mod.JournalEvent(
                        "job-propose", job.module, f"{cause}; battery {battery}", job.id
                    )
                ],
            )
            _notify(cfg, updated)
            del state.pending[job_id]
            continue

Notes:
- `_json` is the module-level `import json as _json` already in daemon.py.
- `cause`, `action`, `battery`, `message` are already computed just above this point. Keep the existing `message` line (auto-commit path still uses it). The propose branch does NOT commit and does NOT touch the journal snapshot/truncate logic.
- The auto-commit path (`cfg.daemon.auto_commit` True) must remain BYTE-IDENTICAL to today. Do not change any of the existing journal-snapshot / `landing.land` / PARKED / LANDED code below the propose branch.

### 2. Journal action set (`_is_daemon_journal_addition`, around lines 544-551)

Add `"job-propose"` and `"job-discard"` to the action set so uncommitted propose/discard journal lines are classified as daemon-authored (union-safe). Do NOT add `"job-land"`. The resulting set should be:

    return action in {
        "build",
        "refreeze",
        "job-fail",
        "job-park",
        "job-supersede",
        "job-propose",
        "job-discard",
        "probe-fail",
    }

## Tests to add (`tests/test_daemon.py`)

The test file uses a `repo` fixture (temp git repo whose `jaunt.toml` sets `[daemon] auto_commit = true`), a `jaunt_cfg` fixture, a `FakeRunner`, `_spec_commit(repo)`, `_opt_into_journal(repo)`, and a `_cycle(repo, cfg, state, runner, pool)` helper that drives `run_once` + `drain` to quiescence. `_git(repo, *args)` runs git. Read the existing tests (e.g. `test_run_once_full_cycle_lands_and_journals`, `test_restart_supersedes_stale_parked_job_and_enqueues_fresh`, `test_supersede_on_newer_spec_commit`) to match idioms exactly. `from jaunt.config import load_config` is already imported.

Add these tests.

    def test_green_job_proposes_when_auto_commit_false(repo: Path) -> None:
        (repo / "jaunt.toml").write_text(
            'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[daemon]\nauto_commit = false\n',
            encoding="utf-8",
        )
        _git(repo, "add", "-A")
        _git(repo, "commit", "-m", "propose mode")
        cfg = load_config(root=repo)
        runner = FakeRunner()
        state = daemon.DaemonState()
        with ThreadPoolExecutor(max_workers=2) as pool:
            _spec_commit(repo)
            _cycle(repo, cfg, state, runner, pool)

        proposed = jobs.list_jobs(repo, states={jobs.PROPOSED})
        assert len(proposed) == 1
        job = proposed[0]
        assert job.module == "app"
        assert (jobs.jobs_dir(repo) / f"{job.id}.patch").read_text(encoding="utf-8")
        assert json.loads(job.patch_paths)
        assert job.cause == "spec change"
        assert job.battery == "3/3"
        assert not jobs.list_jobs(repo, states={jobs.LANDED})
        assert "regen" not in _git(repo, "log", "-1", "--format=%s")

Superseding test. A job id derives from (module, spec_digest, base_commit), so two DISTINCT proposals for the same module need different spec digests. The cleanest deterministic approach: run one propose cycle to produce a real PROPOSED job for "app", then change `runner.digest = "digest-v2"`, reset `runner.built = []`, do another `_spec_commit(repo)`, and cycle again to produce a second, differently-ided proposal. Assert exactly one PROPOSED remains (the newer, digest-v2) and exactly one SUPERSEDED (the older). If two-cycle setup is awkward through `_cycle`, instead pre-create an older PROPOSED JobRecord for "app" via `jobs.JobRecord.new(...)` + `jobs.mark(repo, job, jobs.PROPOSED, patch_paths=..., ...)`, then run one propose cycle whose FakeRunner reports a different digest so the produced job id differs, and assert the pre-existing one became SUPERSEDED and the new one is PROPOSED. Pick whichever is cleaner/deterministic and make it pass.

    def test_newer_proposal_supersedes_older(repo: Path) -> None:
        ...

Auto-commit-unchanged test (repo fixture already sets auto_commit = true):

    def test_auto_commit_true_lands_exactly_as_before(repo: Path, jaunt_cfg: JauntConfig) -> None:
        _opt_into_journal(repo)
        runner = FakeRunner()
        state = daemon.DaemonState()
        with ThreadPoolExecutor(max_workers=2) as pool:
            _spec_commit(repo)
            _cycle(repo, jaunt_cfg, state, runner, pool)
        landed = jobs.list_jobs(repo, states={jobs.LANDED})
        assert len(landed) == 1 and landed[0].module == "app"
        assert "regen(app)" in _git(repo, "log", "-1", "--format=%s")
        assert not jobs.list_jobs(repo, states={jobs.PROPOSED})

## Verification

Run and make green:
- `uv run pytest tests/test_daemon.py -q`
- full suite `uv run pytest -q`
- `uv run ruff check .`
- `uv run ruff format .`
- `uv run ty check`

Do NOT modify any files other than `src/jaunt/daemon.py` and `tests/test_daemon.py`. Do NOT change existing test assertions. Do NOT change the auto-commit landing code path.
