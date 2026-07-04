# Task: Implement `jaunt jobs land <id>` and `jaunt jobs discard <id>` (Task 4 of propose-only daemon plan)

Do NOT read or execute anything under ~/.claude/, ~/.codex/, .claude/, .agents/, or agents/.

You are working in the git worktree `/home/diwank/github.com/creatorrr/jaunt-impl-propose-only` (branch feat/propose-only-daemon). Work ONLY in that directory. Use absolute paths.

Tasks 1-3 are ALREADY implemented (config `[daemon] auto_commit` default false; `jobs.PROPOSED`/`jobs.DISCARDED` states + `JobRecord.cause`/`refrozen` fields + `proposed_for_module`; daemon propose branch in `_land_pending`). You are implementing ONLY the CLI verbs `jobs land` and `jobs discard`, plus their tests.

## Files to modify
- `src/jaunt/cli.py` — parser wiring next to the `jobs retry` parser (around line 291-298); new handlers `_cmd_jobs_land` and `_cmd_jobs_discard` next to `_cmd_jobs_retry` (around line 1260-1327); dispatch in `cmd_jobs` (around line 1330-1337).
- `tests/test_cli_jobs.py` — add the new tests (this is the file that contains the `jobs retry` CLI tests).

## Global constraints
- Python 3.12+, ruff line-length 100 (rules E/F/I/UP/B), `ty` clean.
- Exit codes are frozen: `0` ok, `2` config/discovery (bad id/state), `4` failed/refused (`EXIT_PYTEST_FAILURE`), `5` wait timeout. `jobs land` refusals use `4`; bad ids/states use `2` — mirroring `_cmd_jobs_retry`.
- NO `--force` on `jobs land`.
- Tests mock no external services; use a temp git repo (see the existing test idiom below).

## Parser wiring (add right after the `jobs_retry_p` block, before `jobs_wait_p`)
```python
    jobs_land_p = jobs_sub.add_parser("land", help="Land a parked proposal as a provenance commit.")
    jobs_land_p.add_argument("job_id", nargs="?")
    jobs_land_p.add_argument("--all", action="store_true")
    jobs_land_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_discard_p = jobs_sub.add_parser("discard", help="Discard a parked proposal.")
    jobs_discard_p.add_argument("job_id")
    jobs_discard_p.add_argument("--root", default=argparse.SUPPRESS)
```
Note: the `jobs retry` parser has NO `--json`; match that — do NOT add `--json` to land/discard.

## Dispatch in `cmd_jobs`
Add, before the final `return _cmd_jobs_list(args)`:
```python
    if args.jobs_command == "land":
        return _cmd_jobs_land(args)
    if args.jobs_command == "discard":
        return _cmd_jobs_discard(args)
```

## Behavior of `_cmd_jobs_land(args) -> int` (single-id path only; `--all` is a LATER task — for now, if `args.all` is set OR `args.job_id` is None, print an error to stderr and return `EXIT_CONFIG_OR_DISCOVERY`; do NOT implement the --all loop)

Follow this table EXACTLY:

1. Resolve `root = Path(args.root).resolve()`. Load `job = jobs_mod.load_job(root, args.job_id)`. If None → stderr `error: job not found: <id>`, exit 2 (mirror retry).
2. Job must be `PROPOSED` (`jobs_mod.PROPOSED`). Else → stderr `error: job <id> is <state>; only proposed jobs can be landed`, exit 2.
3. Validate `job.patch_paths` (must be non-empty JSON list of strings) and that the patch file `jobs_mod.jobs_dir(root) / f"{job.id}.patch"` exists — mirror the retry validation exactly (each failure → stderr error, exit 2).
4. **Freshness gate:** `current_digest = _module_current_digest(root, args, job.module)`. If `current_digest is None or current_digest != job.spec_digest` → mark the job `SUPERSEDED` (`jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)`), stderr `superseded: <module> spec moved since generation; the daemon will propose a fresh build`, return `EXIT_PYTEST_FAILURE` (4). NO `--force`.
5. **Branch precheck:** `current_branch = landing.git_out(root, "rev-parse", "--abbrev-ref", "HEAD").strip()`. If `current_branch != job.branch` → stderr `error: on branch <current_branch>; proposal was generated on <job.branch>` (name both), return `EXIT_PYTEST_FAILURE` (4). Do NOT change job state. (This is a real message instead of `landing.land`'s silent `None`.)
6. **Dirty precheck:** `dirty = landing.git_out(root, "status", "--porcelain", "--", *patch_paths).strip()`. If non-empty → stderr `error: refusing to land <id>; working tree has changes to: <space-joined patch_paths>` (or list the dirty paths), return `EXIT_PYTEST_FAILURE` (4). Job STAYS `PROPOSED` (do NOT supersede).
7. **Journal snapshot/append/truncate** — mirror `daemon.py` `_land_pending` lines 631-673:
   - `journal_path = root / journal_mod.JOURNAL_FILE`; `journal_opted_in = journal_path.exists()`.
   - `snapshot_len = 0`; `extra_paths: tuple[str, ...] = ()`.
   - If `journal_opted_in`: `snapshot_len = journal_path.stat().st_size`; append one event:
     ```python
     journal_mod.append_events(
         root,
         [journal_mod.JournalEvent(
             "refreeze" if job.refrozen else "build",
             job.module,
             f"{job.cause or 'spec change'}; battery {job.battery or '-'}",
             job.id,
         )],
     )
     ```
     then `extra_paths = (journal_mod.JOURNAL_FILE,)`.
   - Truncate back via a local helper that mirrors `daemon._truncate_journal` (open r+, `f.truncate(snapshot_len)`) on ANY non-landing outcome (HEAD_MOVED, conflict/None, or a raised `landing.LandingError`).
8. **Land:**
   ```python
   expected_head = landing.git_out(root, "rev-parse", "HEAD").strip()
   sha = landing.land(
       root,
       patch,
       patch_paths=patch_paths_raw,
       message=landing.build_commit_message(
           job.module,
           job.cause or "spec change",
           job.id,
           job.spec_digest,
       ),
       expected_branch=job.branch,
       expected_head=expected_head,
       extra_commit_paths=extra_paths,
   )
   ```
   Wrap in try/except `landing.LandingError`: on exception, truncate the journal (if opted in) then stderr the error and return `EXIT_PYTEST_FAILURE`.
   Outcomes:
   - `sha == landing.HEAD_MOVED` → truncate journal, stderr `head moved; re-run jaunt jobs land`, return `EXIT_PYTEST_FAILURE`. Job stays `PROPOSED`.
   - `sha is None` (3-way conflict; dirty was pre-checked) → truncate journal, `jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)`, stderr `conflict applying proposal; superseded -- the daemon will rebuild`, return `EXIT_PYTEST_FAILURE`.
   - real sha → `jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha, phase="")`, `print(sha)`, return `EXIT_OK`.

## Behavior of `_cmd_jobs_discard(args) -> int`
1. `root = Path(args.root).resolve()`. Load job. If None → stderr `error: job not found: <id>`, exit 2.
2. Must be `PROPOSED`. Else → stderr `error: job <id> is <state>; only proposed jobs can be discarded`, exit 2.
3. `jobs_mod.mark(root, job, jobs_mod.DISCARDED)`.
4. Delete the patch file `jobs_mod.jobs_dir(root) / f"{job.id}.patch"` (use `.unlink(missing_ok=True)`).
5. Append `journal_mod.JournalEvent("job-discard", job.module, "discarded", job.id)` via `journal_mod.append_events` (uncommitted; union-safe; only appends if JAUNT_LOG exists).
6. `print(f"discarded {job.id}")`, return `EXIT_OK`.

## Imports available in cli.py
Handlers should do local imports like `_cmd_jobs_retry` does:
```python
    from jaunt import jobs as jobs_mod
    from jaunt import journal as journal_mod
    from jaunt import landing
```
`EXIT_OK`, `EXIT_CONFIG_OR_DISCOVERY`, `EXIT_PYTEST_FAILURE`, `_eprint`, `_module_current_digest`, `json` are module-level in cli.py already.

## Reference: the existing `_cmd_jobs_retry` (cli.py:1260-1327) — mirror its patch_paths validation and structure
```python
def _cmd_jobs_retry(args: argparse.Namespace) -> int:
    from jaunt import jobs as jobs_mod
    from jaunt import landing

    root = Path(args.root).resolve()
    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        _eprint(f"error: job not found: {args.job_id}")
        return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PARKED:
        _eprint(f"error: job {job.id} is {job.state}; only parked jobs can be retried")
        return EXIT_CONFIG_OR_DISCOVERY
    if not job.patch_paths:
        _eprint(f"error: parked job {job.id} has no patch paths")
        return EXIT_CONFIG_OR_DISCOVERY
    try:
        patch_paths_raw = json.loads(job.patch_paths)
    except json.JSONDecodeError:
        _eprint(f"error: parked job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY
    if not isinstance(patch_paths_raw, list) or not all(
        isinstance(path, str) for path in patch_paths_raw
    ):
        _eprint(f"error: parked job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY
    patch_file = jobs_mod.jobs_dir(root) / f"{job.id}.patch"
    if not patch_file.exists():
        _eprint(f"error: parked job {job.id} is missing patch file")
        return EXIT_CONFIG_OR_DISCOVERY
    patch = patch_file.read_text(encoding="utf-8")
    ...
```
Use the same messages but with "proposed job"/"proposal" wording instead of "parked".

## `landing.land` signature (already implemented)
```python
def land(repo, patch, *, patch_paths, message, expected_branch, expected_head,
         extra_commit_paths=()) -> str | None:
    # returns HEAD_MOVED sentinel, None (conflict/dirty), or the commit sha
```
`landing.HEAD_MOVED` is a string sentinel. `landing.build_commit_message(module, cause, job_id, spec_digest)` and `landing.git_out(repo, *args)` (raises `landing.LandingError`).

## Tests to add to tests/test_cli_jobs.py
The file already has helpers: `_git(repo, *args)`, `scaffolded_project` fixture, `_patch_for(repo, relpath, content) -> (patch, base, [relpath])`, `_make_magic_project(tmp_path) -> (project, module)`, `_status_digest(project, module, capsys) -> digest`, and `_park_job(...)`.

Add a helper `_propose_job(root, patch, paths, *, module="app", spec_digest="d", cause="spec change", battery="-", refrozen="")` mirroring `_park_job` but marking `jobs.PROPOSED` and setting `cause`/`battery`/`refrozen`:
```python
def _propose_job(root, patch, paths, *, module="app", spec_digest="d",
                 cause="spec change", battery="-", refrozen=""):
    job = jobs.JobRecord.new(
        module=module, spec_digest=spec_digest,
        base_commit=_git(root, "rev-parse", "HEAD"), branch="main",
    )
    patch_file = jobs.jobs_dir(root) / f"{job.id}.patch"
    patch_file.parent.mkdir(parents=True, exist_ok=True)
    patch_file.write_text(patch, encoding="utf-8")
    return jobs.mark(root, job, jobs.PROPOSED, patch_paths=json.dumps(paths),
                     cause=cause, battery=battery, refrozen=refrozen)
```

Write these tests (adapt exact wording to your implemented messages):

1. `test_jobs_land_happy_path_creates_provenance_commit` — use `_make_magic_project(tmp_path)` + `_status_digest` so the freshness digest matches (like `test_jobs_retry_lands_when_spec_digest_matches`). Propose a job with `spec_digest=digest, cause="spec change"`. Run `main(["jobs", "land", job.id, "--root", str(project)])`. Assert exit 0; job reloads `LANDED` with `landed_commit == HEAD`; the HEAD commit message equals `landing.build_commit_message(module, "spec change", job.id, digest)`; the generated file has the patched content. GOLDEN CHECK: the commit message and committed paths must match what the auto-commit daemon path would produce (build_commit_message with cause).

2. `test_jobs_land_commits_journal_line` — like #1 but create a `JAUNT_LOG` file first (`(project / "JAUNT_LOG").write_text("", ...)` then `_git add`+commit it). After landing, assert `JAUNT_LOG` contains a `build` line naming the module and the job id, and that the landing commit included `JAUNT_LOG` (check `_git(project, "show", "--name-only", "HEAD")` lists `JAUNT_LOG`).

3. `test_jobs_land_stale_digest_supersedes` — propose with a bogus `spec_digest="stale"` on a magic project (or scaffolded_project where `_module_current_digest` returns None/differs). Assert exit 4; stderr contains `superseded` and `spec moved since generation`; job reloads `SUPERSEDED`; no new commit landed (HEAD unchanged).

4. `test_jobs_land_dirty_paths_refuses_without_superseding` — use `scaffolded_project`; propose a job for `src/__generated__/app.py`; write a dirty change to that path in the working tree first; but freshness must pass — use a spec_digest matching `_module_current_digest`. Simplest: since scaffolded_project's `app` module has no magic specs, `_module_current_digest` likely returns None → freshness fails first. Instead, to isolate the dirty gate, MONKEYPATCH `jaunt.cli._module_current_digest` to return the job's `spec_digest`. Then create a dirty working-tree file at a patch path, run land, assert exit 4, stderr mentions the dirty path, and job stays `PROPOSED` (NOT superseded).

5. `test_jobs_land_conflict_supersedes_and_truncates_journal` — mirror `test_jobs_retry_keeps_parked_on_conflict`: propose a job whose patch conflicts with committed content; monkeypatch `_module_current_digest` to match so freshness passes; create a `JAUNT_LOG` file (committed) and snapshot its content; run land; assert exit 4, stderr mentions `conflict`, job reloads `SUPERSEDED`, and `JAUNT_LOG` content is unchanged (journal truncated back — no leftover `build` line).

6. `test_jobs_land_wrong_state_exits_2` — create a job in `PARKED` (via `_park_job`) or `LANDED` state, run `jobs land`, assert exit 2 and stderr says only proposed jobs can be landed.

7. `test_jobs_land_job_not_found_exits_2` — run `jobs land nonexistent`, assert exit 2.

8. `test_jobs_discard_marks_and_removes_patch` — propose a job (scaffolded_project, monkeypatch not needed since discard doesn't check digest). Run `main(["jobs", "discard", job.id])`. Assert exit 0; stdout `discarded <id>`; job reloads `DISCARDED`; patch file no longer exists.

9. `test_jobs_discard_wrong_state_exits_2` — job in `PARKED`; discard → exit 2.

For monkeypatching `_module_current_digest`, use `monkeypatch.setattr(jaunt.cli, "_module_current_digest", lambda root, args, module: <digest>)`. `jaunt.cli` is imported at top of the test file.

For branch-mismatch you may optionally add a test, but it is not required for Task 4 unless trivial.

## After implementing
Run:
```
cd /home/diwank/github.com/creatorrr/jaunt-impl-propose-only && uv run pytest tests/test_cli_jobs.py -q
```
Fix until green. Then run `uv run ruff check src/jaunt/cli.py tests/test_cli_jobs.py` and `uv run ruff format src/jaunt/cli.py tests/test_cli_jobs.py`.

Do NOT modify any other files. Do NOT touch config.py, jobs.py, daemon.py, discovery.py, or docs. Do NOT bump versions.
