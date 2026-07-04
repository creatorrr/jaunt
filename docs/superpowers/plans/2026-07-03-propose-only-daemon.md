# Propose-Only Daemon Landing + Discovery AST Prescreen — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Spec:** `docs/superpowers/specs/2026-07-03-propose-only-daemon-discovery-prescreen-design.md` — read it first.

**Goal:** jaunt 1.2.0 — the daemon parks green jobs as reviewable proposals by default (`[daemon] auto_commit = false`), landed explicitly via `jaunt jobs land`; discovery imports only modules that show jaunt markers.

**Architecture:** Propose-only is a branch inside the daemon's existing `_land_pending` that reuses the PARKED-job patch persistence (`.jaunt/jobs/<id>.patch` + `patch_paths`) with a new `PROPOSED` state; `jaunt jobs land` is a generalization of the existing `jobs retry` mechanics with a stricter refuse-and-supersede posture and the auto-commit path's journal line. The prescreen is a pure function applied in `discover_module_files`' scan branch; the `--target` fast-path bypasses it.

**Tech Stack:** Python 3.12+, `ast`, pytest, ruff (line-length 100, E/F/I/UP/B), `ty`, `uv`.

## Global Constraints

- After each task: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format .`, `uv run ty check` — all green before commit.
- Tests mock the generator/runner; no API keys, no `codex` binary.
- Exit codes are frozen: `0` ok, `2` config/discovery, `4` failed/refused, `5` wait timeout. `jobs land` failures use `4` (`EXIT_PYTEST_FAILURE`), bad ids/states use `2` (`EXIT_CONFIG_OR_DISCOVERY`) — mirroring `_cmd_jobs_retry` (`cli.py:1251-1318`).
- No `--force` on `jobs land` (spec decision). `jobs retry` keeps its existing `--force`.
- `JobRecord` is a frozen dataclass persisted as JSON; new fields MUST have defaults (old records must load).
- Conventional commits; commit after every task.

## File Structure

- Modify `src/jaunt/config.py` — `DaemonConfig.auto_commit: bool = False` + TOML parsing.
- Modify `src/jaunt/jobs.py` — `PROPOSED`/`DISCARDED` states; `JobRecord.cause`/`refrozen` fields; `proposed_for_module`.
- Modify `src/jaunt/daemon.py` — propose branch in `_land_pending`; journal action set; daemon status landing mode.
- Modify `src/jaunt/cli.py` — `jobs land [<id>|--all]`, `jobs discard <id>`; wait treats `PROPOSED` as green (verify + test).
- Modify `src/jaunt/discovery.py` — `_has_jaunt_markers` + `spec_prescreen` param.
- Modify docs: `docs-site/content/docs/guides/daemon.mdx`, `reference/cli.mdx`, `reference/config.mdx`, `reference/limitations.mdx`, `src/jaunt/instructions/primer.md`, `CLAUDE.md`.
- Tests: `tests/test_daemon.py`, `tests/test_jobs*.py` (follow existing file split), `tests/test_cli_jobs*.py` or where `jobs retry`/`jobs wait` tests live (grep `_cmd_jobs_retry` / `jobs_wait` under `tests/`), `tests/test_discovery.py`.

---

### Task 1: Config — `auto_commit` (default false) + daemon status line

**Files:**
- Modify: `src/jaunt/config.py:98-101` (`DaemonConfig`), `:507-515` (parsing)
- Modify: `src/jaunt/daemon.py` (the `daemon status` output — grep `def cmd_daemon` / status rendering; it may live in `cli.py`)
- Test: wherever `DaemonConfig`/`[daemon]` parsing is tested (grep `poll_interval` in `tests/`)

**Interfaces:**
- Produces: `DaemonConfig.auto_commit: bool = False`; `[daemon] auto_commit = true|false` parsed via the existing `_as_bool` helper (grep `_as_bool` in config.py; if absent, follow the pattern of other boolean fields like `[skills] auto`).

- [ ] **Step 1: Failing tests**

```python
def test_daemon_auto_commit_defaults_false(tmp_path):
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    cfg = load_config(root=tmp_path)
    assert cfg.daemon.auto_commit is False


def test_daemon_auto_commit_parses_true(tmp_path):
    (tmp_path / "jaunt.toml").write_text('version = 1\n[daemon]\nauto_commit = true\n')
    cfg = load_config(root=tmp_path)
    assert cfg.daemon.auto_commit is True
```

- [ ] **Step 2: Run — expect AttributeError/assert failure.**
- [ ] **Step 3: Implement** — add the field after `notify_command`:

```python
@dataclass(frozen=True)
class DaemonConfig:
    poll_interval: float = 2.0
    max_jobs: int = 0
    notify_command: str = ""
    auto_commit: bool = False
```

and in the `[daemon]` parsing block:

```python
    if "auto_commit" in daemon_tbl:
        daemon_auto_commit = _as_bool(daemon_tbl["auto_commit"], name="daemon.auto_commit")
    else:
        daemon_auto_commit = False
```

(threading it into the `DaemonConfig(...)` construction). In `daemon status` output add one line: `landing: auto-commit` / `landing: propose-only` from `cfg.daemon.auto_commit`.

- [ ] **Step 4: Existing daemon tests.** `uv run pytest tests/test_daemon.py -q` — landing-behavior tests now fail (they assume auto-commit). Set `auto_commit = true` in those tests' config fixtures (jaunt.toml text or `DaemonConfig(...)` constructions) — grep `DaemonConfig(` and `[daemon]` under `tests/`. Do NOT change test assertions; the auto-commit path must remain byte-identical when opted in.
- [ ] **Step 5: Full gates, commit** — `feat(config): [daemon] auto_commit, default false (propose-only)`

---

### Task 2: Job model — `PROPOSED`/`DISCARDED` states + provenance fields

**Files:**
- Modify: `src/jaunt/jobs.py`
- Test: where `jobs.py` is unit-tested (grep `PARKED` under `tests/`)

**Interfaces:**
- Produces: `jobs.PROPOSED = "proposed"`, `jobs.DISCARDED = "discarded"`; both in `PHASE_CLEAR_STATES`, **neither** in `ACTIVE_STATES` (a `PROPOSED` module must be re-queueable and `jobs wait` must terminate on it); `JobRecord.cause: str = ""`, `JobRecord.refrozen: str = ""` (empty/`"1"`); `proposed_for_module(root, module) -> JobRecord | None` (mirror of `parked_for_module`, `jobs.py:105-109`).

- [ ] **Step 1: Failing tests**

```python
def test_proposed_not_active_and_phase_clearing(tmp_path):
    job = JobRecord.new(module="m", spec_digest="d", base_commit="c", branch="main")
    save_job(tmp_path, job)
    updated = mark(tmp_path, job, PROPOSED, cause="spec change", refrozen="")
    assert updated.state == PROPOSED
    assert PROPOSED not in ACTIVE_STATES
    assert PROPOSED in PHASE_CLEAR_STATES
    assert proposed_for_module(tmp_path, "m").id == job.id


def test_old_job_records_load_without_new_fields(tmp_path):
    job = JobRecord.new(module="m", spec_digest="d", base_commit="c", branch="main")
    save_job(tmp_path, job)
    raw = json.loads((tmp_path / ".jaunt" / "jobs" / f"{job.id}.json").read_text())
    del raw["cause"], raw["refrozen"]
    (tmp_path / ".jaunt" / "jobs" / f"{job.id}.json").write_text(json.dumps(raw))
    assert load_job(tmp_path, job.id) is not None
```

- [ ] **Step 2: Run — fails on missing names.**
- [ ] **Step 3: Implement** — constants after `SUPERSEDED = "superseded"`:

```python
PROPOSED = "proposed"
DISCARDED = "discarded"
```

`PHASE_CLEAR_STATES` gains both. `JobRecord` gains (after `patch_paths`):

```python
    cause: str = ""      # human cause recorded at green time; reused for the landing commit
    refrozen: str = ""   # "1" when the green result was a re-freeze (journal action "refreeze")
```

and:

```python
def proposed_for_module(root: Path, module: str) -> JobRecord | None:
    for job in list_jobs(root, states={PROPOSED}):
        if job.module == module:
            return job
    return None
```

- [ ] **Step 4: Full gates, commit** — `feat(jobs): PROPOSED/DISCARDED states + cause/refrozen provenance fields`

---

### Task 3: Daemon — propose branch in `_land_pending`

**Files:**
- Modify: `src/jaunt/daemon.py:575-653` (`_land_pending`), `:544-551` (`_is_daemon_journal_addition` action set)
- Test: `tests/test_daemon.py` (follow its existing `_land_pending`/fake-runner fixtures)

**Interfaces:**
- Consumes: Task 1 `cfg.daemon.auto_commit`; Task 2 states/fields/`proposed_for_module`.
- Produces: with `auto_commit=false`, a green job ends `PROPOSED` with `.jaunt/jobs/<id>.patch` written, `patch_paths`/`cause`/`refrozen`/`battery` recorded, older `PROPOSED` for the module marked `SUPERSEDED`, journal `job-propose` line appended (uncommitted — recognized as daemon-authored), `_notify` fired. No commit is created.

- [ ] **Step 1: Failing test** (adapt to the file's existing daemon-fixture idiom — fake runner, temp git repo):

```python
def test_green_job_proposes_when_auto_commit_false(daemon_repo):
    # daemon_repo: existing fixture-style temp repo with a green-result pending job
    cfg = _cfg(daemon_repo, auto_commit=False)
    _run_land_pending(daemon_repo, cfg)          # helper mirroring existing tests
    job = jobs_mod.list_jobs(daemon_repo)[0]
    assert job.state == jobs_mod.PROPOSED
    assert (jobs_mod.jobs_dir(daemon_repo) / f"{job.id}.patch").read_text()
    assert json.loads(job.patch_paths)
    assert job.cause == "spec change"
    assert _head_unchanged(daemon_repo)          # no commit landed


def test_newer_proposal_supersedes_older(daemon_repo): ...
    # two green results for the same module, sequential _land_pending runs:
    # first job ends SUPERSEDED, second ends PROPOSED


def test_auto_commit_true_lands_exactly_as_before(daemon_repo): ...
    # cfg auto_commit=True: LANDED with provenance commit — assert against the
    # same expectations the pre-existing landing test uses (no drift).
```

- [ ] **Step 2: Run — fails (jobs land instead of propose).**
- [ ] **Step 3: Implement** — in `_land_pending`, after `message = landing.build_commit_message(...)` (`daemon.py:594`), insert the propose branch:

```python
        if not cfg.daemon.auto_commit:
            (jobs_mod.jobs_dir(root) / f"{job.id}.patch").write_text(
                result.patch, encoding="utf-8"
            )
            prev = jobs_mod.proposed_for_module(root, job.module)
            if prev is not None and prev.id != job.id:
                jobs_mod.mark(root, prev, jobs_mod.SUPERSEDED)
                journal_mod.append_events(
                    root,
                    [journal_mod.JournalEvent(
                        "job-supersede", prev.module, "newer proposal", prev.id
                    )],
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
                [journal_mod.JournalEvent(
                    "job-propose", job.module, f"{cause}; battery {battery}", job.id
                )],
            )
            _notify(cfg, updated)
            del state.pending[job_id]
            continue
```

Add `"job-propose"` and `"job-discard"` to the `_is_daemon_journal_addition` action set (`daemon.py:544-551`) so uncommitted propose/discard lines are classified as daemon-authored, and `"job-land"` is NOT added (landing appends the existing `build`/`refreeze` actions).

- [ ] **Step 4: Full gates, commit** — `feat(daemon): park green jobs as proposals when auto_commit is off`

---

### Task 4: CLI — `jobs land <id>` and `jobs discard <id>`

**Files:**
- Modify: `src/jaunt/cli.py` (parser wiring next to `jobs retry` at `:291-294`; handlers next to `_cmd_jobs_retry` at `:1251`; dispatch in `cmd_jobs` at `:1321`)
- Test: the file containing `jobs retry` CLI tests (grep `jobs_retry\|_cmd_jobs_retry` under `tests/`)

**Interfaces:**
- Consumes: Tasks 2–3.
- Produces: `_cmd_jobs_land(args) -> int`, `_cmd_jobs_discard(args) -> int`; parser: `jobs land (job_id | --all)`, `jobs discard job_id`, both with `--root` and `--json` consistent with sibling verbs.

Behavior of `land <id>` (spec §Feature 1, refined by code reality):

1. Job must exist and be `PROPOSED` (else exit 2, message like retry's).
2. **Freshness:** `_module_current_digest(root, args, job.module)` (`cli.py:1285`) vs `job.spec_digest`; mismatch → mark `SUPERSEDED`, stderr `superseded: <module> spec moved since generation; the daemon will propose a fresh build`, exit 4. No `--force`.
3. **Branch:** current branch must equal `job.branch` → else refuse (exit 4, message names both branches). Pre-check so the user gets a real message instead of `landing.land`'s silent `None`.
4. **Dirty precheck:** `git status --porcelain -- <patch_paths>` non-empty → refuse listing the dirty paths, exit 4, job stays `PROPOSED`.
5. **Journal:** if `JAUNT_LOG` exists: snapshot size, append `JournalEvent("refreeze" if job.refrozen else "build", job.module, f"{job.cause or 'spec change'}; battery {job.battery or '-'}", job.id)`, pass `extra_commit_paths=(journal_mod.JOURNAL_FILE,)`; truncate back on any non-landing outcome (mirror `_land_pending`'s snapshot/truncate at `daemon.py:595-637`).
6. **Land:** `landing.land(root, patch, patch_paths=..., message=landing.build_commit_message(job.module, job.cause or "spec change", job.id, job.spec_digest), expected_branch=job.branch, expected_head=<rev-parse HEAD>, extra_commit_paths=...)`.
   - sha → mark `LANDED` + `landed_commit`, print sha, exit 0.
   - `HEAD_MOVED` → truncate journal, stderr `head moved; re-run jaunt jobs land`, exit 4, stays `PROPOSED`.
   - `None` (3-way conflict — dirty was pre-checked) → truncate journal, mark `SUPERSEDED`, stderr `conflict applying proposal; superseded — the daemon will rebuild`, exit 4.

`discard <id>`: must be `PROPOSED` → mark `DISCARDED`, delete `.jaunt/jobs/<id>.patch`, append `JournalEvent("job-discard", job.module, "discarded", job.id)` (uncommitted; union-safe), print `discarded <id>`, exit 0.

- [ ] **Step 1: Failing tests** — one per behavior above, in the retry-test idiom (temp git repo, fabricated `PROPOSED` job record + patch file):

```python
def test_jobs_land_happy_path_creates_provenance_commit(proposed_repo): ...
    # exit 0; commit message == landing.build_commit_message(module, cause, id, digest);
    # job LANDED with landed_commit == HEAD; journal gained a "build" line committed with it

def test_jobs_land_stale_digest_supersedes(proposed_repo): ...
def test_jobs_land_dirty_paths_refuses_without_superseding(proposed_repo): ...
def test_jobs_land_conflict_supersedes_and_truncates_journal(proposed_repo): ...
def test_jobs_land_wrong_state_exits_2(proposed_repo): ...
def test_jobs_discard_marks_and_removes_patch(proposed_repo): ...
```

- [ ] **Step 2: Run — parser errors (`land` unknown).**
- [ ] **Step 3: Implement** per the behavior table. Parser additions beside `jobs retry`:

```python
    jobs_land_p = jobs_sub.add_parser("land", help="Land a parked proposal as a provenance commit.")
    jobs_land_p.add_argument("job_id", nargs="?")
    jobs_land_p.add_argument("--all", action="store_true")
    jobs_land_p.add_argument("--root", default=argparse.SUPPRESS)
    jobs_discard_p = jobs_sub.add_parser("discard", help="Discard a parked proposal.")
    jobs_discard_p.add_argument("job_id")
    jobs_discard_p.add_argument("--root", default=argparse.SUPPRESS)
```

(match sibling parsers' `--json`/root conventions exactly — read the retry parser block first).

- [ ] **Step 4: Full gates, commit** — `feat(cli): jaunt jobs land/discard for parked proposals`

---

### Task 5: `jobs land --all` + `jobs wait` proposal semantics

**Files:**
- Modify: `src/jaunt/cli.py` (`_cmd_jobs_land` gains the `--all` path; wait needs verification only)
- Test: same CLI test file + the `jobs wait` test file

**Interfaces:**
- Consumes: Task 4.
- Produces: `land --all` lands every `PROPOSED` job in **`created` order** (the daemon queues jobs in dependency order, so creation order approximates the spec's "dependency order" — a spec touch-up in Task 7 records this refinement); per-job outcome lines; exit 0 iff every attempted land succeeded (0 proposals → 0); first hard git error aborts remaining lands.
- `jobs wait`: `PROPOSED` must terminate the wait as green. Because `PROPOSED ∉ ACTIVE_STATES` (Task 2) and `_jobs_wait_result_code` (`cli.py:961-964`) only fails on `failed`/`parked`, this should already hold — this task **proves** it with tests rather than changing code.

- [ ] **Step 1: Failing/verifying tests**

```python
def test_jobs_land_all_lands_in_created_order_and_reports(proposed_repo_two_modules): ...
    # two proposals; both land; two provenance commits in created order; exit 0

def test_jobs_land_all_mixed_fresh_stale(proposed_repo_two_modules): ...
    # one fresh (lands), one stale (superseded); exit 4; both outcomes printed

def test_jobs_wait_treats_proposed_as_green(daemon_repo): ...
    # watched job reaches PROPOSED → wait returns EXIT_OK and terminates

def test_jobs_wait_json_includes_proposed_state(daemon_repo): ...
```

- [ ] **Step 2–3: Implement `--all`** (loop over `list_jobs(root, states={PROPOSED})`, reuse the single-land routine, aggregate exit code). If the wait tests fail, fix the specific state-set the wait loop uses (grep `ACTIVE_STATES` usage in `_cmd_jobs_wait`) — do not widen failure codes.
- [ ] **Step 4: Full gates, commit** — `feat(cli): jobs land --all + PROPOSED is terminal-green for jobs wait`

---

### Task 6: Discovery AST prescreen

**Files:**
- Modify: `src/jaunt/discovery.py` (`discover_module_files` scan branch, `discovery.py:165-184`)
- Test: `tests/test_discovery.py`

**Interfaces:**
- Produces: `_has_jaunt_markers(source: str) -> bool`; `discover_module_files(..., spec_prescreen: bool = True)` and the same kwarg on `discover_modules`, applied ONLY in the scan branch (`target_modules` fast-path untouched).
- Callers audit: grep `discover_modules(\|discover_module_files(` across `src/` — spec discovery call sites (builder/cli/tester/daemon probe) keep the default `True`; any non-spec usage (e.g. repo_context tree building, watcher file lists) must pass `spec_prescreen=False` or not use these functions at all — verify and adjust.

- [ ] **Step 1: Failing tests**

```python
def test_prescreen_skips_markerless_module(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "boobytrap.py").write_text("raise RuntimeError('imported!')\n")
    (root / "spec_mod.py").write_text("import jaunt\n@jaunt.magic()\ndef f() -> int:\n    ...\n")
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__")
    assert names == ["spec_mod"]


def test_prescreen_passes_bare_decorator_form(tmp_path):
    # `from jaunt import magic` + bare @magic
    ...
    assert "bare_mod" in names


def test_prescreen_skips_syntax_error_file_quietly(tmp_path): ...
def test_target_fast_path_bypasses_prescreen(tmp_path):
    # boobytrap.py explicitly targeted → still discovered (import error surfaces later, unchanged)
    names = discover_modules(roots=[root], exclude=[], generated_dir="__generated__",
                             target_modules={"boobytrap"})
    assert names == ["boobytrap"]
def test_textual_prefilter_short_circuits(tmp_path, monkeypatch):
    # file without 'jaunt' substring: ast.parse must not be called
    ...
```

- [ ] **Step 2: Run — boobytrap appears in names.**
- [ ] **Step 3: Implement**

```python
_JAUNT_DECORATOR_NAMES = frozenset({"magic", "test", "contract", "preserve"})


def _has_jaunt_markers(source: str) -> bool:
    """True when the source shows evidence of jaunt specs (import or decorator).

    Cheap textual prefilter first — files without the substring ``jaunt`` are
    never parsed. Files that fail to parse cannot define importable specs.
    """
    if "jaunt" not in source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "jaunt" or a.name.startswith("jaunt.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "jaunt" or mod.startswith("jaunt."):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                name = (
                    target.attr
                    if isinstance(target, ast.Attribute)
                    else getattr(target, "id", "")
                )
                if name in _JAUNT_DECORATOR_NAMES:
                    return True
    return False
```

In the scan loop, after the exclude check:

```python
            if spec_prescreen:
                try:
                    source = py_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not _has_jaunt_markers(source):
                    continue
```

(add `import ast` to the module imports).

- [ ] **Step 4: Full gates** — the whole suite must stay green: existing discovery-dependent tests all use spec-bearing modules, so the prescreen should be invisible. Any test that discovers a markerless module is a real caller-audit finding — fix the call site, not the test, unless the test itself fabricates a non-spec module for discovery (then pass `spec_prescreen=False` there deliberately and say why in the test).
- [ ] **Step 5: Commit** — `feat(discovery): AST prescreen — only import modules with jaunt markers`

---

### Task 7: Docs + spec touch-ups

**Files:**
- Modify: `docs-site/content/docs/guides/daemon.mdx` (landing modes, propose→land flow, `wait && land --all` loop), `reference/cli.mdx` (`jobs land/discard`), `reference/config.mdx` (`auto_commit`, default false, changelog note), `reference/limitations.mdx` (prescreen alias limitation), `src/jaunt/instructions/primer.md` (wait-then-land), `CLAUDE.md` ([daemon] table + jobs verbs).
- Modify: `docs/superpowers/specs/2026-07-03-propose-only-daemon-discovery-prescreen-design.md` — record two implementation refinements: (a) `--all` lands in job-creation order (daemon queues in dependency order; spec said "dependency order"), (b) journal detail: `job-propose`/`job-discard` lines are daemon/CLI-appended uncommitted (union-safe, recognized as daemon-authored); the landing commit carries the standard `build`/`refreeze` line rather than a new `land` action.

- [ ] **Step 1: Make the edits** (doc prose only; no source changes).
- [ ] **Step 2: Full gates (unchanged), commit** — `docs: propose-only daemon landing + discovery prescreen`

---

### Task 8: Version bump

**Files:**
- Modify: `pyproject.toml` (`version = "1.2.0"`), `uv.lock` (via `uv lock`)

- [ ] **Step 1:** `sed -i 's/^version = "1.1.0"/version = "1.2.0"/' pyproject.toml && uv lock`
- [ ] **Step 2: Full gates, commit** — `chore: bump version to 1.2.0`

(Merging the PR publishes 1.2.0 to PyPI via the release workflow — mem-mcp-b's adoption campaign consumes it directly.)

---

## Execution notes

- Order is 1→2→3→4→5 strictly (each consumes the previous interface); 6 is independent (can run parallel to 3–5 if executed by concurrent agents — different files); 7–8 last.
- Riskiest surface: Task 4's journal snapshot/truncate mirror of `_land_pending` — read `daemon.py:595-637` before writing it, and golden-compare the landing commit (message AND committed paths) against the auto-commit path in the Task 4 happy-path test.
- The `auto_commit` default flip is the one change existing users feel; Task 1 deliberately updates daemon-test fixtures to `true` so the auto path stays pinned byte-for-byte.
