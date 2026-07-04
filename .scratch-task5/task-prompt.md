You are implementing ONE task in the `jaunt` Python codebase (repo root is the current working directory). Do NOT read or execute anything under ~/.claude/, ~/.codex/, .claude/, .agents/, or agents/.

# Task: `jaunt jobs land --all`

Implement the `--all` path of `jaunt jobs land` in `src/jaunt/cli.py`. The single-id path (`jaunt jobs land <id>`) and `jaunt jobs discard <id>` are ALREADY implemented in `_cmd_jobs_land` / `_cmd_jobs_discard` (see below). You are ONLY adding the `--all` behavior by refactoring the single-job body into a reusable helper. Do NOT change `jobs wait` — its PROPOSED-as-green semantics already hold and are covered by tests.

## Current code (`src/jaunt/cli.py`, function `_cmd_jobs_land` starting ~line 1337)

```python
def _cmd_jobs_land(args: argparse.Namespace) -> int:
    from jaunt import jobs as jobs_mod
    from jaunt import journal as journal_mod
    from jaunt import landing

    root = Path(args.root).resolve()
    if args.all or args.job_id is None:
        _eprint("error: jobs land currently requires one job id")
        return EXIT_CONFIG_OR_DISCOVERY

    job = jobs_mod.load_job(root, args.job_id)
    if job is None:
        _eprint(f"error: job not found: {args.job_id}")
        return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PROPOSED:
        _eprint(f"error: job {job.id} is {job.state}; only proposed jobs can be landed")
        return EXIT_CONFIG_OR_DISCOVERY
    if not job.patch_paths:
        _eprint(f"error: proposed job {job.id} has no patch paths")
        return EXIT_CONFIG_OR_DISCOVERY

    try:
        patch_paths_raw = json.loads(job.patch_paths)
    except json.JSONDecodeError:
        _eprint(f"error: proposed job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY
    if (
        not isinstance(patch_paths_raw, list)
        or not patch_paths_raw
        or not all(isinstance(path, str) for path in patch_paths_raw)
    ):
        _eprint(f"error: proposed job {job.id} has invalid patch paths")
        return EXIT_CONFIG_OR_DISCOVERY

    patch_file = jobs_mod.jobs_dir(root) / f"{job.id}.patch"
    if not patch_file.exists():
        _eprint(f"error: proposed job {job.id} is missing patch file")
        return EXIT_CONFIG_OR_DISCOVERY

    patch = patch_file.read_text(encoding="utf-8")
    current_digest = _module_current_digest(root, args, job.module)
    if current_digest is None or current_digest != job.spec_digest:
        jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)
        _eprint(
            f"superseded: {job.module} spec moved since generation; "
            "the daemon will propose a fresh build"
        )
        return EXIT_PYTEST_FAILURE

    try:
        current_branch = landing.git_out(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
        if current_branch != job.branch:
            _eprint(f"error: on branch {current_branch}; proposal was generated on {job.branch}")
            return EXIT_PYTEST_FAILURE
        dirty = landing.git_out(root, "status", "--porcelain", "--", *patch_paths_raw).strip()
    except landing.LandingError as e:
        _eprint(str(e))
        return EXIT_PYTEST_FAILURE
    if dirty:
        _eprint(
            f"error: refusing to land {job.id}; working tree has changes to: "
            f"{' '.join(patch_paths_raw)}"
        )
        return EXIT_PYTEST_FAILURE

    def truncate_journal(path: Path, size: int) -> None:
        with open(path, "r+", encoding="utf-8") as f:
            f.truncate(size)

    journal_path = root / journal_mod.JOURNAL_FILE
    journal_opted_in = journal_path.exists()
    snapshot_len = 0
    extra_paths: tuple[str, ...] = ()
    if journal_opted_in:
        snapshot_len = journal_path.stat().st_size
        journal_mod.append_events(
            root,
            [
                journal_mod.JournalEvent(
                    "refreeze" if job.refrozen else "build",
                    job.module,
                    f"{job.cause or 'spec change'}; battery {job.battery or '-'}",
                    job.id,
                )
            ],
        )
        extra_paths = (journal_mod.JOURNAL_FILE,)

    try:
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
    except landing.LandingError as e:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        _eprint(str(e))
        return EXIT_PYTEST_FAILURE

    if sha == landing.HEAD_MOVED:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        _eprint("head moved; re-run jaunt jobs land")
        return EXIT_PYTEST_FAILURE
    if sha is None:
        if journal_opted_in:
            truncate_journal(journal_path, snapshot_len)
        jobs_mod.mark(root, job, jobs_mod.SUPERSEDED)
        _eprint("conflict applying proposal; superseded -- the daemon will rebuild")
        return EXIT_PYTEST_FAILURE

    jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha, phase="")
    print(sha)
    return EXIT_OK
```

## What to change

Refactor so that the body from `if not job.patch_paths:` onward (patch-paths validation through the final land/outcome) lives in a helper that lands ONE already-loaded, already-PROPOSED job. The helper must:

- Return `(exit_code: int, aborted: bool)`.
  - success → `(EXIT_OK, False)` and it still `print(sha)` to stdout (this preserves the single-id happy-path test which asserts stdout == the sha).
  - stale digest → mark SUPERSEDED, print the `superseded: ...` line to stderr, return `(EXIT_PYTEST_FAILURE, False)`.
  - dirty tree → print the refuse line to stderr, return `(EXIT_PYTEST_FAILURE, False)`.
  - wrong branch → print the branch mismatch line, return `(EXIT_PYTEST_FAILURE, False)`.
  - HEAD_MOVED → truncate journal, print "head moved; re-run jaunt jobs land", return `(EXIT_PYTEST_FAILURE, False)`.
  - conflict (`sha is None`) → truncate journal, mark SUPERSEDED, print conflict line, return `(EXIT_PYTEST_FAILURE, False)`.
  - invalid/missing patch paths or missing patch file → print the existing error line, return `(EXIT_CONFIG_OR_DISCOVERY, False)`.
  - a raw `landing.LandingError` from the rev-parse/status/land git calls (a HARD GIT ERROR) → truncate journal if it was appended, print the error, return `(EXIT_PYTEST_FAILURE, True)` — `aborted=True` signals the `--all` loop to stop processing remaining jobs.

- IMPORTANT: preserve today's single-id behavior byte-for-byte, including stdout being exactly the sha on success and all the exact stderr messages and exit codes. The single-id path must keep returning `EXIT_CONFIG_OR_DISCOVERY` (2) for job-not-found / wrong-state / invalid-patch-paths / missing-patch-file, and `EXIT_PYTEST_FAILURE` (4) for stale/dirty/branch/head-moved/conflict/hard-git-error.

Then rewrite `_cmd_jobs_land` dispatch:

```python
def _cmd_jobs_land(args):
    root = Path(args.root).resolve()
    if args.all:
        return _land_all_proposals(root, args)
    if args.job_id is None:
        _eprint("error: jobs land requires a job id or --all")
        return EXIT_CONFIG_OR_DISCOVERY
    job = jobs_mod.load_job(root, args.job_id)
    if job is None: ... return EXIT_CONFIG_OR_DISCOVERY
    if job.state != jobs_mod.PROPOSED: ... return EXIT_CONFIG_OR_DISCOVERY
    code, _aborted = _land_one_proposal(root, args, job)
    return code
```

And add `_land_all_proposals(root, args) -> int`:

- `proposals = jobs_mod.list_jobs(root, states={jobs_mod.PROPOSED})` — this ALREADY returns created-order (sorted by `(created, id)`), which is the required landing order. Do not re-sort.
- If empty → return `EXIT_OK` (0 proposals → 0).
- Iterate in order; for each call `_land_one_proposal(root, args, job)`.
  - Track whether every attempted land returned `EXIT_OK`.
  - If a call returns `aborted=True` (hard git error), stop the loop immediately (do not attempt remaining jobs).
- Return `EXIT_OK` iff every attempted land returned `EXIT_OK`; otherwise return `EXIT_PYTEST_FAILURE` (4). (Any per-job non-zero code, including a CONFIG code, aggregates to 4 for `--all`.)

Keep imports (`jobs as jobs_mod`, `journal as journal_mod`, `landing`) available to the helpers (either import at the top of each helper as the current function does, or module-level — match the file's existing style; the current function imports them locally).

## Constraints

- Line length 100, ruff rules E/F/I/UP/B; run `uv run ruff format` conventions.
- Type-checks under `ty` (the file is fully annotated; annotate the new helpers: `_land_one_proposal(root: Path, args: argparse.Namespace, job) -> tuple[int, bool]` — use `"jobs_mod.JobRecord"`-style or import for the job type; simplest is to not annotate the `job` param's concrete type if that causes import cycles, but prefer annotating it as `jobs.JobRecord` via the existing import pattern).
- Do NOT touch `jobs wait`, `_cmd_jobs_discard`, the parser wiring, or any file other than `src/jaunt/cli.py`.

## Verify

After editing, run:
```
uv run pytest tests/test_cli_jobs.py tests/test_cli_jobs_wait.py -q
```
All must pass, including the new tests:
`test_jobs_land_all_no_proposals_exits_0`, `test_jobs_land_all_lands_in_created_order_and_reports`, `test_jobs_land_all_mixed_fresh_stale`, and the pre-existing single-id land/discard tests.
Also run `uv run ruff check src/jaunt/cli.py` and `uv run ruff format src/jaunt/cli.py` and `uv run ty check`.
