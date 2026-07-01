# Jaunt Daemon + Change Journal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship `jaunt daemon` (commit-triggered, worktree-isolated background codegen jobs that auto-commit on green), a committed `JAUNT_LOG` change journal, and the `jaunt guard` warn-on-access hook.

**Architecture:** The daemon is a thin orchestrator over jaunt's existing CLI JSON contracts: it polls HEAD, probes staleness via `jaunt status --json` in a detached probe worktree, runs `jaunt build --target <mod> --json` plus a deterministic, model-free gate (`jaunt check` and committed batteries) in per-job worktrees, and lands green diffs onto the developer's branch as pathspec-limited provenance commits — revalidating HEAD immediately before every commit. All model-facing work stays inside the existing build pipeline (gates included); the daemon adds isolation, scheduling, landing, and journaling. Landing is serialized in the main loop; job execution parallelizes via a thread pool (subprocess-bound).

**Tech Stack:** Python 3.12+, argparse CLI (existing pattern), `subprocess` git plumbing, `concurrent.futures.ThreadPoolExecutor`, pytest with fake runners (no API keys, no network).

**Spec:** `docs/superpowers/specs/2026-07-01-jaunt-daemon-background-codegen-design.md`

**Companion plans (not in this document):** adoption parity (async/class contract mode + DB fixtures) and the mem-mcp-b rollout are separate plans per the spec's workstream split.

## Global Constraints

- Python 3.12+; ruff (E/F/I/UP/B, line-length 100); `uv run ruff check .` and `uv run ty check` must pass.
- Full suite green: `uv run pytest`. New tests must not require API keys or network; daemon/build interactions go through injectable runner callables.
- Exit codes follow existing conventions: 0 OK, 2 config/discovery error (`EXIT_CONFIG_OR_DISCOVERY` — that is the real constant name in cli.py, there is no `EXIT_CONFIG`), 3 generation error, 4 test failure.
- New CLI commands dispatch via explicit `if args.command == "...":` branches in `main()` — the repo does NOT use `set_defaults(func=...)`; parser defaults are ignored by the dispatcher.
- `load_config` is keyword-only: call it as `load_config(root=root)`, never positionally.
- The daemon writes only: `__generated__/**` (i.e. `cfg.paths.generated_dir` trees), `*.contract.json` sidecars inside those trees, `JAUNT_LOG`, and its own `.jaunt/` state. It only appends commits — never rebase, never force-push, never reset outside daemon-owned worktrees.
- Journal lines are single-line and pre-redacted: derived-battery detail is never more than opaque id + exception class (mirrors `heldout.py` redaction).
- Timestamps in journal lines are UTC, format `YYYY-MM-DD HH:MMZ`.
- Commit messages for landings end with trailers `Jaunt-Job: <id>` and `Jaunt-Spec: <digest8>`.

---

### Task 1: Journal core (`journal.py`)

**Files:**
- Create: `src/jaunt/journal.py`
- Test: `tests/test_journal.py`

**Interfaces:**
- Consumes: nothing (leaf module).
- Produces: `JOURNAL_FILE = "JAUNT_LOG"`; `JournalEvent(action: str, module: str, detail: str, job_id: str | None = None, when: datetime | None = None)`; `format_line(event: JournalEvent) -> str`; `append_events(root: Path, events: Sequence[JournalEvent], *, create: bool = False) -> bool`; `read_lines(root: Path, *, limit: int = 20, module: str | None = None) -> list[str]`; `ensure_union_merge_attribute(root: Path) -> bool`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_journal.py
from datetime import UTC, datetime
from pathlib import Path

import pytest

from jaunt import journal


def _event(**kw):
    defaults = dict(
        action="build",
        module="recall.compress",
        detail="prose change (gate: MEANINGFUL); battery 47/47",
        job_id="a1b2c3d4",
        when=datetime(2026, 7, 1, 14, 32, tzinfo=UTC),
    )
    defaults.update(kw)
    return journal.JournalEvent(**defaults)


def test_format_line_layout():
    line = journal.format_line(_event())
    assert line == (
        "2026-07-01 14:32Z build    recall.compress — "
        "prose change (gate: MEANINGFUL); battery 47/47; job a1b2c3d4"
    )


def test_format_line_without_job_id():
    line = journal.format_line(_event(action="refreeze", job_id=None, detail="cosmetic (gate: EQUIVALENT)"))
    assert line.endswith("recall.compress — cosmetic (gate: EQUIVALENT)")
    assert "job" not in line


def test_append_requires_existing_file_unless_create(tmp_path: Path):
    assert journal.append_events(tmp_path, [_event()]) is False
    assert not (tmp_path / journal.JOURNAL_FILE).exists()
    assert journal.append_events(tmp_path, [_event()], create=True) is True
    assert journal.append_events(tmp_path, [_event(action="adopt")]) is True
    text = (tmp_path / journal.JOURNAL_FILE).read_text(encoding="utf-8")
    assert text.count("\n") == 2


def test_append_rejects_newlines_in_detail(tmp_path: Path):
    with pytest.raises(ValueError):
        journal.append_events(tmp_path, [_event(detail="two\nlines")], create=True)


def test_read_lines_tail_and_module_filter(tmp_path: Path):
    events = [
        _event(module="recall.rank", detail="d1"),
        _event(module="record.plan", detail="d2"),
        _event(module="recall.rank", detail="d3"),
    ]
    journal.append_events(tmp_path, events, create=True)
    assert len(journal.read_lines(tmp_path, limit=2)) == 2
    ranked = journal.read_lines(tmp_path, module="recall.rank")
    assert len(ranked) == 2
    assert all("recall.rank" in ln for ln in ranked)


def test_ensure_union_merge_attribute(tmp_path: Path):
    assert journal.ensure_union_merge_attribute(tmp_path) is True
    attrs = (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    assert "JAUNT_LOG merge=union" in attrs
    assert journal.ensure_union_merge_attribute(tmp_path) is False  # idempotent
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_journal.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jaunt.journal'` (or ImportError).

- [ ] **Step 3: Implement `src/jaunt/journal.py`**

```python
"""Committed JAUNT_LOG change journal: terse, append-only, one line per event."""

from __future__ import annotations

import os
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

JOURNAL_FILE = "JAUNT_LOG"
_ATTR_LINE = "JAUNT_LOG merge=union"
_ACTION_WIDTH = 8


@dataclass(frozen=True)
class JournalEvent:
    action: str
    module: str
    detail: str
    job_id: str | None = None
    when: datetime | None = None


def format_line(event: JournalEvent) -> str:
    when = event.when or datetime.now(tz=UTC)
    stamp = when.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")
    line = f"{stamp} {event.action:<{_ACTION_WIDTH}} {event.module} — {event.detail}"
    if event.job_id:
        line += f"; job {event.job_id}"
    return line


def append_events(root: Path, events: Sequence[JournalEvent], *, create: bool = False) -> bool:
    """Append one line per event. Opt-in via file presence unless create=True."""
    path = root / JOURNAL_FILE
    if not path.exists() and not create:
        return False
    lines = []
    for event in events:
        for field in (event.action, event.module, event.detail):
            if "\n" in field or "\r" in field:
                raise ValueError(f"journal fields must be single-line: {field!r}")
        lines.append(format_line(event))
    if not lines:
        return path.exists()
    with open(path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return True


def read_lines(root: Path, *, limit: int = 20, module: str | None = None) -> list[str]:
    path = root / JOURNAL_FILE
    if not path.exists():
        return []
    lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    if module is not None:
        lines = [ln for ln in lines if f" {module} — " in ln]
    return lines[-limit:] if limit else lines


def ensure_union_merge_attribute(root: Path) -> bool:
    """Add `JAUNT_LOG merge=union` to .gitattributes if missing. Returns True if added."""
    path = root / ".gitattributes"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    if _ATTR_LINE in existing.splitlines():
        return False
    joiner = "" if (not existing or existing.endswith("\n")) else "\n"
    path.write_text(existing + joiner + _ATTR_LINE + "\n", encoding="utf-8")
    return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_journal.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check src/jaunt/journal.py tests/test_journal.py && uv run ty check
git add src/jaunt/journal.py tests/test_journal.py
git commit -m "feat(journal): JAUNT_LOG core — format, atomic append, tail, merge=union"
```

---

### Task 2: `jaunt log` CLI + journal wiring into build/adopt

**Files:**
- Modify: `src/jaunt/cli.py` (new subparser + `cmd_log`; wiring in `cmd_build` and `cmd_adopt`)
- Test: `tests/test_cli_log.py`

**Interfaces:**
- Consumes: `journal.append_events`, `journal.read_lines` (Task 1).
- Produces: `jaunt log [-n N] [--module X] [--json]` command; build/adopt append journal events when `JAUNT_LOG` exists at root.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_log.py
from pathlib import Path

from jaunt import journal
from jaunt.cli import main


def test_log_command_prints_tail(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    journal.append_events(
        tmp_path,
        [journal.JournalEvent(action="build", module=f"m{i}", detail="d") for i in range(30)],
        create=True,
    )
    rc = main(["log", "-n", "5"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.count("\n") == 5
    assert "m29" in out


def test_log_command_module_filter_and_empty(tmp_path: Path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["log"])
    assert rc == 0
    assert "no journal" in capsys.readouterr().out.lower()
```

Note: mirror the existing CLI test style in `tests/test_cli.py` (they call `main([...])` with
`monkeypatch.chdir`); if `main` requires a scaffolded project for `log`, keep `cmd_log`
config-free (it only needs the root path from `--root`/cwd).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_log.py -v`
Expected: FAIL (argparse: invalid choice 'log').

- [ ] **Step 3: Add the subparser and `cmd_log`**

In `build_parser()` (near the other `subparsers.add_parser` calls, cli.py ~line 326):

```python
log_p = subparsers.add_parser("log", help="Show the JAUNT_LOG change journal.")
log_p.add_argument("-n", "--lines", type=int, default=20, help="Number of lines (0 = all).")
log_p.add_argument("--module", default=None, help="Filter by module name.")
log_p.add_argument("--root", default=".", help="Project root.")
log_p.add_argument("--json", action="store_true", dest="json_mode")
```

Then add the dispatch branch in `main()` alongside the existing `args.command` branches
(the repo dispatches explicitly; `set_defaults(func=...)` is ignored):

```python
if args.command == "log":
    return cmd_log(args)
```

```python
def cmd_log(args: argparse.Namespace) -> int:
    from jaunt import journal

    root = Path(args.root).resolve()
    lines = journal.read_lines(root, limit=args.lines, module=args.module)
    if args.json_mode:
        _emit_json({"command": "log", "ok": True, "lines": lines})
        return EXIT_OK
    if not lines:
        print("No journal entries (no JAUNT_LOG file, or it is empty).")
        return EXIT_OK
    for line in lines:
        print(line)
    return EXIT_OK
```

- [ ] **Step 4: Wire journal events into `cmd_build`**

Locate where build results are final (grep `'"refrozen"'` in cli.py — the JSON payload
assembly in `cmd_build`). Immediately before the payload is emitted / summary printed, add:

```python
from jaunt import journal as _journal

events = []
for mod in sorted(generated):
    change = stale_changes.get(mod, "")
    cause = "prose change (gate: MEANINGFUL)" if change == "prose" else "structural change"
    events.append(_journal.JournalEvent(action="build", module=mod, detail=cause))
for mod in sorted(refrozen):
    events.append(
        _journal.JournalEvent(action="refreeze", module=mod, detail="cosmetic (gate: EQUIVALENT)")
    )
for mod, err in sorted(failed.items()):
    first = str(err).splitlines()[0][:120] if str(err) else "generation failed"
    events.append(_journal.JournalEvent(action="build-fail", module=mod, detail=first))
_journal.append_events(root, events)  # opt-in: appends only if JAUNT_LOG exists
```

Adapt the variable names to the ones cmd_build actually uses for the `generated` /
`refrozen` / `failed` payload keys (they exist — the JSON contract guarantees it). If
`stale_changes` is not in scope at that point, use `detail="rebuilt"` for generated
modules; do not thread new state through the builder for this.

In `cmd_adopt` (cli.py ~line 932), after a successful adopt, append:

```python
from jaunt import journal as _journal

_journal.append_events(
    root,
    [_journal.JournalEvent(action="adopt", module=spec_ref, detail=f"battery derived: {case_count} cases")],
)
```

(`spec_ref`/`case_count`: use the local names for the adopted ref and derived-case count
already present in `cmd_adopt`'s success path; if no count is at hand, `detail="battery derived"`.)

- [ ] **Step 5: Extend tests for the wiring**

Append to `tests/test_cli_log.py` a test that follows the existing mocked-build pattern from
`tests/test_cli.py` (fake backend, scaffolded project): create `JAUNT_LOG` first (opt-in),
run `main(["build", ...])`, then assert `journal.read_lines(root)` contains a
`build`/`refreeze` line for the built module. Reuse whatever project fixture
`tests/test_cli.py` uses for build tests verbatim.

- [ ] **Step 6: Run tests, lint, commit**

Run: `uv run pytest tests/test_cli_log.py tests/test_cli.py -v`
Expected: PASS.

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/cli.py tests/test_cli_log.py
git commit -m "feat(journal): jaunt log command + build/adopt journal wiring"
```

---

### Task 3: Job records (`jobs.py`)

**Files:**
- Create: `src/jaunt/jobs.py`
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: nothing (leaf module; stdlib only).
- Produces: state constants `QUEUED/RUNNING/GREEN/LANDED/PARKED/FAILED/SUPERSEDED`; `JobRecord` dataclass; `new_job_id(module, spec_digest, base_commit) -> str`; `jobs_dir(root) -> Path`; `save_job(root, job)`; `load_job(root, job_id) -> JobRecord | None`; `list_jobs(root, states=None) -> list[JobRecord]`; `active_for_module(root, module) -> JobRecord | None`; `mark(root, job, state, **updates) -> JobRecord`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_jobs.py
from pathlib import Path

from jaunt import jobs


def _mk(root: Path, module="recall.rank", digest="abc123", base="deadbeef") -> jobs.JobRecord:
    job = jobs.JobRecord.new(module=module, spec_digest=digest, base_commit=base, branch="main")
    jobs.save_job(root, job)
    return job


def test_new_job_id_deterministic_and_short():
    a = jobs.new_job_id("m", "d1", "c1")
    assert a == jobs.new_job_id("m", "d1", "c1")
    assert a != jobs.new_job_id("m", "d2", "c1")
    assert len(a) == 8


def test_save_load_roundtrip(tmp_path: Path):
    job = _mk(tmp_path)
    loaded = jobs.load_job(tmp_path, job.id)
    assert loaded == job
    assert loaded.state == jobs.QUEUED
    assert jobs.load_job(tmp_path, "nope") is None


def test_list_jobs_filters_and_sorts(tmp_path: Path):
    j1 = _mk(tmp_path, module="a")
    j2 = _mk(tmp_path, module="b")
    jobs.mark(tmp_path, j2, jobs.FAILED, error="boom")
    assert [j.module for j in jobs.list_jobs(tmp_path)] == ["a", "b"]
    assert [j.module for j in jobs.list_jobs(tmp_path, states={jobs.QUEUED})] == ["a"]
    assert j1.state == jobs.QUEUED


def test_active_for_module(tmp_path: Path):
    job = _mk(tmp_path)
    assert jobs.active_for_module(tmp_path, "recall.rank").id == job.id
    jobs.mark(tmp_path, job, jobs.LANDED, landed_commit="c0ffee")
    assert jobs.active_for_module(tmp_path, "recall.rank") is None


def test_mark_updates_fields_and_persists(tmp_path: Path):
    job = _mk(tmp_path)
    updated = jobs.mark(tmp_path, job, jobs.GREEN, gate="MEANINGFUL", battery="47/47")
    assert updated.state == jobs.GREEN
    assert jobs.load_job(tmp_path, job.id).battery == "47/47"
    assert updated.updated >= updated.created
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: FAIL (no module `jaunt.jobs`).

- [ ] **Step 3: Implement `src/jaunt/jobs.py`**

```python
"""Persisted job records for the jaunt daemon (.jaunt/jobs/*.json)."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

QUEUED = "queued"
RUNNING = "running"
GREEN = "green"
LANDED = "landed"
PARKED = "parked"
FAILED = "failed"
SUPERSEDED = "superseded"

ACTIVE_STATES = frozenset({QUEUED, RUNNING, GREEN})


def new_job_id(module: str, spec_digest: str, base_commit: str) -> str:
    return hashlib.sha256(f"{module}\x00{spec_digest}\x00{base_commit}".encode()).hexdigest()[:8]


@dataclass(frozen=True)
class JobRecord:
    id: str
    module: str
    spec_digest: str
    base_commit: str
    branch: str
    state: str
    created: float
    updated: float
    gate: str = ""
    battery: str = ""
    landed_commit: str = ""
    error: str = ""
    detail_log: str = ""
    patch_paths: str = ""  # JSON-encoded list; set when a job parks so retry can re-land

    @classmethod
    def new(cls, *, module: str, spec_digest: str, base_commit: str, branch: str) -> JobRecord:
        now = time.time()
        return cls(
            id=new_job_id(module, spec_digest, base_commit),
            module=module,
            spec_digest=spec_digest,
            base_commit=base_commit,
            branch=branch,
            state=QUEUED,
            created=now,
            updated=now,
        )


def jobs_dir(root: Path) -> Path:
    return root / ".jaunt" / "jobs"


def _path(root: Path, job_id: str) -> Path:
    return jobs_dir(root) / f"{job_id}.json"


def save_job(root: Path, job: JobRecord) -> None:
    path = _path(root, job.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(job), sort_keys=True, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def load_job(root: Path, job_id: str) -> JobRecord | None:
    path = _path(root, job_id)
    if not path.exists():
        return None
    try:
        return JobRecord(**json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, TypeError):
        return None


def list_jobs(root: Path, states: frozenset[str] | set[str] | None = None) -> list[JobRecord]:
    directory = jobs_dir(root)
    if not directory.exists():
        return []
    records = []
    for path in directory.glob("*.json"):
        job = load_job(root, path.stem)
        if job is not None and (states is None or job.state in states):
            records.append(job)
    return sorted(records, key=lambda j: (j.created, j.id))


def active_for_module(root: Path, module: str) -> JobRecord | None:
    for job in list_jobs(root, states=ACTIVE_STATES):
        if job.module == module:
            return job
    return None


def mark(root: Path, job: JobRecord, state: str, **updates: str) -> JobRecord:
    updated = replace(job, state=state, updated=time.time(), **updates)
    save_job(root, updated)
    return updated
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_jobs.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Lint, commit**

```bash
uv run ruff check src/jaunt/jobs.py tests/test_jobs.py && uv run ty check
git add src/jaunt/jobs.py tests/test_jobs.py
git commit -m "feat(daemon): persisted job records in .jaunt/jobs"
```

---

### Task 4: Landing — patch extraction with path allowlist (`landing.py`, part 1)

**Files:**
- Create: `src/jaunt/landing.py`
- Test: `tests/test_landing.py`

**Interfaces:**
- Consumes: nothing from other new modules; shells out to `git`.
- Produces: `LandingError`; `git_out(repo: Path, *args: str) -> str` (raises `LandingError` on nonzero exit); `changed_paths(worktree: Path, base_commit: str) -> list[str]`; `extract_patch(worktree: Path, base_commit: str, is_allowed: Callable[[str], bool]) -> str` (empty string when no changes; raises `LandingError` when any changed path fails the predicate — the predicate form exists precisely so the daemon can allow *nested* generated dirs like `src/pkg/__generated__/mod.py` without allowlisting whole source roots).

- [ ] **Step 1: Write the failing tests (real git in tmp repos)**

```python
# tests/test_landing.py
import subprocess
from pathlib import Path

import pytest

from jaunt import landing


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
    (r / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def _machine_owned(path: str) -> bool:
    return "/__generated__/" in f"/{path}" or path == "JAUNT_LOG"


def test_extract_patch_scoped_by_predicate(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    gen = repo / "src" / "__generated__"
    gen.mkdir()
    (gen / "app.py").write_text("y = 2\n", encoding="utf-8")
    patch = landing.extract_patch(repo, base, is_allowed=_machine_owned)
    assert "src/__generated__/app.py" in patch


def test_extract_patch_rejects_out_of_scope_paths(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 999\n", encoding="utf-8")
    with pytest.raises(landing.LandingError, match="src/app.py"):
        landing.extract_patch(repo, base, is_allowed=_machine_owned)


def test_extract_patch_empty_when_no_changes(repo: Path):
    base = _git(repo, "rev-parse", "HEAD")
    assert landing.extract_patch(repo, base, is_allowed=_machine_owned) == ""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_landing.py -v`
Expected: FAIL (no module `jaunt.landing`).

- [ ] **Step 3: Implement extraction**

```python
"""Landing: extract job diffs and commit them onto the developer's branch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


class LandingError(Exception):
    pass


def git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise LandingError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def changed_paths(worktree: Path, base_commit: str) -> list[str]:
    git_out(worktree, "add", "-A")  # stage untracked so diff sees new files
    out = git_out(worktree, "diff", "--cached", "--name-only", base_commit)
    return [p for p in out.splitlines() if p.strip()]


def extract_patch(
    worktree: Path, base_commit: str, is_allowed: Callable[[str], bool]
) -> str:
    paths = changed_paths(worktree, base_commit)
    if not paths:
        return ""
    violations = [p for p in paths if not is_allowed(p)]
    if violations:
        raise LandingError(f"job touched paths outside allowlist: {', '.join(sorted(violations))}")
    return git_out(worktree, "diff", "--cached", "--binary", base_commit, "--", *paths)
```

Notes: staging happens in the *job worktree*, which is daemon-owned — the developer's tree
is never staged. `git add -A` does not stage ignored files, so `.jaunt/cache` writes inside
the worktree stay invisible provided `.jaunt/` is gitignored (the daemon refuses to start
otherwise — Task 6).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_landing.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Lint, commit**

```bash
uv run ruff check src/jaunt/landing.py tests/test_landing.py && uv run ty check
git add src/jaunt/landing.py tests/test_landing.py
git commit -m "feat(daemon): patch extraction with hard path allowlist"
```

---

### Task 5: Landing — apply and commit with trailers (`landing.py`, part 2)

**Files:**
- Modify: `src/jaunt/landing.py`
- Test: `tests/test_landing.py` (extend)

**Interfaces:**
- Consumes: `JobRecord` (Task 3) for message construction only (pass fields, not the object).
- Produces: `build_commit_message(module: str, cause: str, job_id: str, spec_digest: str) -> str`; `HEAD_MOVED = "HEAD_MOVED"` (sentinel; a real SHA can never equal it); `land(repo: Path, patch: str, *, patch_paths: Sequence[str], message: str, expected_branch: str, expected_head: str) -> str | None` — returns the commit SHA on success, `HEAD_MOVED` when the repo's HEAD no longer equals `expected_head` (caller defers to the next daemon iteration, which re-probes and may supersede — this closes the stale-landing race where a user commits a newer spec while a job runs), and `None` when parking is required (branch mismatch, dirty machine-owned paths, or 3-way conflict). Never raises for park conditions; raises `LandingError` only for unexpected git failures.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_landing.py`:

```python
def _patch_for(repo: Path, relpath: str, content: str) -> tuple[str, str, list[str]]:
    """Produce (patch, base, paths) for a single-file change without committing it."""
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    patch = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--binary", base],
        check=True, capture_output=True, text=True,
    ).stdout
    _git(repo, "reset", "--hard", base)  # rewind; the patch is the artifact
    return patch, base, [relpath]


def _land(repo: Path, patch: str, paths: list[str], msg: str = "m", branch: str = "main"):
    head = _git(repo, "rev-parse", "HEAD")
    return landing.land(
        repo, patch, patch_paths=paths, message=msg, expected_branch=branch, expected_head=head
    )


def test_land_commits_with_trailers(repo: Path):
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 2\n")
    msg = landing.build_commit_message("app", "prose change", "a1b2c3d4", "abcd1234")
    sha = _land(repo, patch, paths, msg=msg)
    assert sha and sha != landing.HEAD_MOVED
    body = _git(repo, "log", "-1", "--format=%B")
    assert "Jaunt-Job: a1b2c3d4" in body and "Jaunt-Spec: abcd1234" in body
    assert (repo / "src/__generated__/app.py").read_text(encoding="utf-8") == "y = 2\n"


def test_land_is_pathspec_limited(repo: Path):
    (repo / "notes.txt").write_text("dev work in progress\n", encoding="utf-8")
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 3\n")
    sha = _land(repo, patch, paths, msg="regen(app): x")
    assert sha and sha != landing.HEAD_MOVED
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["src/__generated__/app.py"]
    assert (repo / "notes.txt").exists()  # untouched, uncommitted


def test_land_defers_when_head_moved(repo: Path):
    stale_head = _git(repo, "rev-parse", "HEAD")
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 9\n")
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "user commit moves HEAD")
    result = landing.land(
        repo, patch, patch_paths=paths, message="m", expected_branch="main", expected_head=stale_head
    )
    assert result == landing.HEAD_MOVED
    assert _git(repo, "status", "--porcelain") == ""  # nothing applied


def test_land_parks_on_wrong_branch(repo: Path):
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 4\n")
    _git(repo, "checkout", "-b", "other")
    assert _land(repo, patch, paths) is None


def test_land_parks_on_locally_modified_generated_path(repo: Path):
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 5\n")
    (repo / "src/__generated__").mkdir(exist_ok=True)
    (repo / "src/__generated__/app.py").write_text("hand edit\n", encoding="utf-8")
    assert _land(repo, patch, paths) is None


def test_land_parks_on_conflict(repo: Path):
    # Patch built against a file state that no longer exists after a conflicting commit.
    patch, base, paths = _patch_for(repo, "src/__generated__/app.py", "y = 6\n")
    (repo / "src/__generated__").mkdir(exist_ok=True)
    (repo / "src/__generated__/app.py").write_text("conflicting committed content\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "conflicting")
    result = _land(repo, patch, paths)
    assert result is None
    status = _git(repo, "status", "--porcelain")
    assert status == ""  # no half-applied state left behind
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_landing.py -v`
Expected: new tests FAIL with `AttributeError: ... no attribute 'land'`.

- [ ] **Step 3: Implement landing**

Append to `src/jaunt/landing.py`:

```python
import tempfile
from collections.abc import Sequence

HEAD_MOVED = "HEAD_MOVED"  # sentinel: caller defers landing to the next daemon iteration


def build_commit_message(module: str, cause: str, job_id: str, spec_digest: str) -> str:
    return (
        f"regen({module}): {cause}\n\nJaunt-Job: {job_id}\nJaunt-Spec: {spec_digest[:8]}\n"
    )


def _current_branch(repo: Path) -> str:
    return git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def land(
    repo: Path,
    patch: str,
    *,
    patch_paths: Sequence[str],
    message: str,
    expected_branch: str,
    expected_head: str,
) -> str | None:
    if not patch:
        return None
    if _current_branch(repo) != expected_branch:
        return None
    if git_out(repo, "rev-parse", "HEAD").strip() != expected_head:
        # A commit landed after the daemon probed this HEAD (possibly a newer spec for
        # this very module). Never land against an unprobed HEAD — defer; the next
        # iteration re-probes and either supersedes the job or lands it cleanly.
        return HEAD_MOVED
    dirty = git_out(repo, "status", "--porcelain", "--", *patch_paths).strip()
    if dirty:
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
        f.write(patch)
        patch_file = f.name
    apply_proc = subprocess.run(
        ["git", "-C", str(repo), "apply", "--3way", patch_file],
        capture_output=True, text=True, check=False,
    )
    if apply_proc.returncode != 0:
        # Roll back any partial application on the machine-owned paths only.
        subprocess.run(
            ["git", "-C", str(repo), "checkout", "--", *patch_paths],
            capture_output=True, text=True, check=False,
        )
        subprocess.run(
            ["git", "-C", str(repo), "clean", "-fd", "--", *patch_paths],
            capture_output=True, text=True, check=False,
        )
        return None
    git_out(repo, "add", "--", *patch_paths)
    git_out(repo, "commit", "-m", message, "--", *patch_paths)
    return git_out(repo, "rev-parse", "HEAD").strip()
```

Edge case encoded in the tests: rollback after a failed 3-way must clear both modified
tracked files (`checkout --`) and newly-created untracked files (`clean -fd --`), and both
are pathspec-limited to the machine-owned paths — the developer's other files are never
touched by rollback.

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_landing.py -v`
Expected: PASS (8 tests).

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/landing.py tests/test_landing.py
git commit -m "feat(daemon): pathspec-limited landing with provenance trailers + park conditions"
```

---

### Task 6: Daemon config + lockfile + `jaunt daemon` CLI skeleton

**Files:**
- Modify: `src/jaunt/config.py` (add `DaemonConfig`, parse `[daemon]`)
- Create: `src/jaunt/daemon.py` (lockfile + start/stop/status plumbing only in this task)
- Modify: `src/jaunt/cli.py` (`jaunt daemon start|stop|status` subcommands)
- Test: `tests/test_daemon.py`, `tests/test_config.py` (extend)

**Interfaces:**
- Consumes: existing `JauntConfig` loading; `jobs.list_jobs` (Task 3).
- Produces: `DaemonConfig(poll_interval: float = 2.0, max_jobs: int = 0, notify_command: str = "")` on `JauntConfig.daemon`; `daemon.acquire_lock(root) -> bool`; `daemon.release_lock(root)`; `daemon.lock_pid(root) -> int | None` (None if absent or stale); `daemon.DISABLE_ENV = "JAUNT_DAEMON_DISABLE"`; CLI `cmd_daemon`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_daemon.py
import os
from pathlib import Path

from jaunt import daemon


def test_lock_acquire_release(tmp_path: Path):
    assert daemon.acquire_lock(tmp_path) is True
    assert daemon.lock_pid(tmp_path) == os.getpid()
    assert daemon.acquire_lock(tmp_path) is False  # already held by a live pid
    daemon.release_lock(tmp_path)
    assert daemon.lock_pid(tmp_path) is None


def test_stale_lock_is_reclaimed(tmp_path: Path):
    lock = tmp_path / ".jaunt" / "daemon.pid"
    lock.parent.mkdir(parents=True)
    lock.write_text("999999999\n", encoding="utf-8")  # certainly-dead pid
    assert daemon.lock_pid(tmp_path) is None
    assert daemon.acquire_lock(tmp_path) is True
    daemon.release_lock(tmp_path)
```

Extend `tests/test_config.py` following its existing table-parsing test pattern: a config
with `[daemon]\npoll_interval = 5.0\nmax_jobs = 2` parses onto `cfg.daemon.poll_interval == 5.0`,
and the section is optional (defaults apply when absent).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: FAIL (no module `jaunt.daemon`).

- [ ] **Step 3: Implement config + lockfile**

`config.py` — add near the other section dataclasses, and wire into `JauntConfig` +
the TOML parsing function following the exact pattern of `CodexConfig`:

```python
@dataclass
class DaemonConfig:
    poll_interval: float = 2.0
    max_jobs: int = 0  # 0 -> fall back to build.jobs
    notify_command: str = ""
```

`src/jaunt/daemon.py`:

```python
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
    """Atomic acquire via O_CREAT|O_EXCL — check-then-write would let two daemons race."""
    path = _lock_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(2):  # one retry after clearing a stale pidfile
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if lock_pid(root) is not None:
                return False  # a live daemon holds the lock
            path.unlink(missing_ok=True)  # stale: owning pid is dead
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(f"{os.getpid()}\n")
        return True
    return False


def release_lock(root: Path) -> None:
    _lock_path(root).unlink(missing_ok=True)
```

- [ ] **Step 4: Add the CLI subcommands**

In `build_parser()`:

```python
daemon_p = subparsers.add_parser("daemon", help="Background codegen daemon.")
daemon_sub = daemon_p.add_subparsers(dest="daemon_command", required=True)
daemon_start_p = daemon_sub.add_parser("start", help="Run the daemon (foreground; Ctrl-C to stop).")
daemon_start_p.add_argument("--root", default=".")
daemon_start_p.add_argument("--json", action="store_true", dest="json_mode")
daemon_stop_p = daemon_sub.add_parser("stop", help="Stop a running daemon.")
daemon_stop_p.add_argument("--root", default=".")
daemon_status_p = daemon_sub.add_parser("status", help="Show daemon and job status.")
daemon_status_p.add_argument("--root", default=".")
daemon_status_p.add_argument("--json", action="store_true", dest="json_mode")
```

`cmd_daemon` (start delegates to `daemon.run_daemon` — implemented in Task 7; for this task,
`start` may raise `NotImplementedError` behind the lock acquisition so stop/status are testable):

```python
def cmd_daemon(args: argparse.Namespace) -> int:
    import signal

    from jaunt import daemon as daemon_mod
    from jaunt import jobs as jobs_mod

    root = Path(args.root).resolve()
    if args.daemon_command == "stop":
        pid = daemon_mod.lock_pid(root)
        if pid is None:
            print("Daemon not running.")
            return EXIT_OK
        os.kill(pid, signal.SIGTERM)
        print(f"Sent SIGTERM to daemon (pid {pid}).")
        return EXIT_OK
    if args.daemon_command == "status":
        pid = daemon_mod.lock_pid(root)
        records = jobs_mod.list_jobs(root)
        if getattr(args, "json_mode", False):
            _emit_json(
                {
                    "command": "daemon-status",
                    "ok": True,
                    "running": pid is not None,
                    "pid": pid,
                    "jobs": [{"id": j.id, "module": j.module, "state": j.state} for j in records],
                }
            )
        else:
            print(f"Daemon: {'running (pid ' + str(pid) + ')' if pid else 'stopped'}")
            for j in records[-10:]:
                print(f"- {j.id} {j.module}: {j.state}")
        return EXIT_OK
    # start
    if os.environ.get(daemon_mod.DISABLE_ENV):
        print(f"{daemon_mod.DISABLE_ENV} is set; refusing to start.", file=sys.stderr)
        return EXIT_CONFIG_OR_DISCOVERY
    ignored = (
        subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", ".jaunt"],
            capture_output=True, check=False,
        ).returncode
        == 0
    )
    if not ignored:
        print(
            "error: .jaunt/ must be gitignored before running the daemon "
            "(its cache and job state would otherwise trip the landing allowlist). "
            "Add '.jaunt/' to .gitignore.",
            file=sys.stderr,
        )
        return EXIT_CONFIG_OR_DISCOVERY
    if not daemon_mod.acquire_lock(root):
        print("Daemon already running.", file=sys.stderr)
        return EXIT_CONFIG_OR_DISCOVERY
    try:
        daemon_mod.run_daemon(root)  # Task 7
        return EXIT_OK
    finally:
        daemon_mod.release_lock(root)
```

(Use the module's existing `EXIT_OK`/`EXIT_CONFIG_OR_DISCOVERY` constants and `_emit_json`
helper — there is no `EXIT_CONFIG` in cli.py; `sys`, `os`, and `subprocess` are already
imported there. Dispatch: add `if args.command == "daemon": return cmd_daemon(args)` to
`main()`'s explicit branch chain.)

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/test_daemon.py tests/test_config.py -v`
Expected: PASS.

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/daemon.py src/jaunt/config.py src/jaunt/cli.py tests/
git commit -m "feat(daemon): config section, pidfile lock, daemon start/stop/status CLI"
```

---

### Task 7: Daemon core loop — poll, probe, enqueue, execute, land

**Files:**
- Modify: `src/jaunt/daemon.py`
- Modify: `src/jaunt/status_core.py`, `src/jaunt/cli.py` (Step 0 prerequisite: digests + `--magic-only`)
- Test: `tests/test_daemon.py` (extend), `tests/test_cli_status.py` (extend)

**Interfaces:**
- Consumes: `jobs.*` (Task 3, incl. `patch_paths` field), `landing.*` (Tasks 4–5, incl. `HEAD_MOVED` and `changed_paths`), `journal.append_events` (Task 1).
- Produces:
  - `MagicStatus.digests: dict[str, str]` (Step 0 — module → `module_digest`, the same digest build freshness uses; NOT the contract digest) exposed in the `jaunt status --json` payload as `"digests"`, plus a `--magic-only` status flag that skips contract-row evaluation and tree drift (contract rows can run batteries — far too heavy and env-dependent for a probe).
  - `Runner` protocol: `probe(worktree: Path) -> tuple[dict[str, str], dict[str, str]]` (stale module → change kind, module → digest); `build(worktree: Path, module: str) -> BuildOutcome`; `gate(worktree: Path, module: str) -> GateOutcome`.
  - `BuildOutcome(ok: bool, refrozen: bool, error: str = "")`.
  - `GateOutcome(ok: bool, battery: str = "-", detail: str = "")` — detail is pre-redacted, single line.
  - `JobResult(job_id, build, gate, patch, patch_paths)`.
  - `CliRunner` default impl shelling `[sys.executable, "-m", "jaunt", ...]`.
  - `DaemonState` (last probed HEAD, in-flight futures, pending-landing results).
  - `run_once(root: Path, cfg: JauntConfig, state: DaemonState, runner: Runner, pool: Executor) -> None` — single testable iteration, ordered probe → collect → land → spawn.
  - `run_daemon(root: Path, *, runner: Runner | None = None, iterations: int | None = None, sleep=time.sleep) -> None`.

- [ ] **Step 0 (prerequisite): expose module digests + `--magic-only` in `jaunt status`**

The daemon's supersede logic keys on the real per-module digest, and its probe must be
cheap and pure. Neither holds today: `MagicStatus` carries only `stale/fresh/stale_changes`,
and `cmd_status` evaluates contract rows (which can run batteries) and tree drift.

Failing test first — extend `tests/test_cli_status.py` following its existing fixture
pattern:

```python
def test_status_json_exposes_module_digests_and_magic_only(scaffolded_magic_project, capsys, monkeypatch):
    monkeypatch.chdir(scaffolded_magic_project)
    rc = main(["status", "--json", "--magic-only"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["stale"]  # the scaffolded spec module is unbuilt, hence stale
    for module in payload["stale"]:
        assert payload["digests"][module]  # non-empty digest per discovered module
    assert "contracts" not in payload  # --magic-only skips contract evaluation entirely
```

Run: `uv run pytest tests/test_cli_status.py -v -k digests` — expect FAIL.

Implementation: add `digests: dict[str, str]` to `MagicStatus` (`status_core.py:92`),
populated where per-module staleness is computed — use the same `module_digest` value the
freshness comparison already computes (grep `module_digest` in `status_core.py` /
`digest.py`; do NOT recompute differently). In `cmd_status`, add the `--magic-only` flag:
when set, skip `_contract_rows` and tree-drift entirely and omit those keys from the JSON
payload; always include `"digests": mstatus.digests`. Run the test, then commit:
`git commit -m "feat(status): expose per-module digests + --magic-only probe flag"`.

- [ ] **Step 1: Write the failing tests**

Extend `tests/test_daemon.py`. Use real git tmp repos (reuse the `repo` fixture shape from
`tests/test_landing.py` — extract it into a shared helper in this file rather than importing
across test modules) and a fake runner:

```python
import subprocess
from concurrent.futures import ThreadPoolExecutor

from jaunt import daemon, jobs, journal


class FakeRunner:
    """Stale until built; probe returns a controllable digest so tests can supersede."""

    def __init__(self, module="app", change="prose"):
        self.module, self.change = module, change
        self.digest = "digest-v1"
        self.built: list[str] = []

    def probe(self, worktree):
        if self.built:
            return {}, {}
        return {self.module: self.change}, {self.module: self.digest}

    def build(self, worktree, module):
        gen = worktree / "src" / "__generated__"
        gen.mkdir(parents=True, exist_ok=True)
        (gen / f"{module}.py").write_text("generated = True\n", encoding="utf-8")
        self.built.append(module)
        return daemon.BuildOutcome(ok=True, refrozen=False)

    def gate(self, worktree, module):
        return daemon.GateOutcome(ok=True, battery="3/3")


def _spec_commit(repo, body='"""spec v2"""\n'):
    (repo / "src" / "app.py").write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "spec commit")


def _cycle(repo, cfg, state, runner, pool, n=3):
    """Drive run_once to quiescence: probe/spawn, drain, collect/land."""
    for _ in range(n):
        daemon.run_once(repo, cfg, state, runner, pool)
        daemon.drain(state)


def test_run_once_full_cycle_lands_and_journals(repo, jaunt_cfg):
    journal.append_events(repo, [], create=True)  # opt in to journaling
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=2) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)
    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert len(landed) == 1 and landed[0].module == "app"
    assert landed[0].spec_digest == "digest-v1"
    assert "regen(app)" in _git(repo, "log", "-1", "--format=%s")
    assert any("build" in ln and "app" in ln and "3/3" in ln for ln in journal.read_lines(repo))
    assert (repo / "src" / "__generated__" / "app.py").exists()


def test_supersede_on_newer_spec_commit(repo, jaunt_cfg):
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg, state, runner, pool)  # enqueue against digest-v1
        first = jobs.list_jobs(repo)[0]
        runner.digest = "digest-v2"                            # spec genuinely changed
        _spec_commit(repo, body='"""spec v3"""\n')
        _cycle(repo, jaunt_cfg, state, runner, pool)
    assert jobs.load_job(repo, first.id).state == jobs.SUPERSEDED
    landed = jobs.list_jobs(repo, states={jobs.LANDED})
    assert landed and landed[0].spec_digest == "digest-v2"


def test_failed_build_journals_and_marks_failed(repo, jaunt_cfg):
    journal.append_events(repo, [], create=True)

    class FailingRunner(FakeRunner):
        def build(self, worktree, module):
            self.built.append(module)
            return daemon.BuildOutcome(ok=False, refrozen=False, error="codex exited 3")

    runner = FailingRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)
    failed = jobs.list_jobs(repo, states={jobs.FAILED})
    assert failed and "codex exited 3" in failed[0].error
    assert any("job-fail" in ln for ln in journal.read_lines(repo))


def test_failed_gate_blocks_landing(repo, jaunt_cfg):
    class GateFailRunner(FakeRunner):
        def gate(self, worktree, module):
            return daemon.GateOutcome(ok=False, detail="jaunt check failed")

    runner = GateFailRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        _cycle(repo, jaunt_cfg, state, runner, pool)
    assert not jobs.list_jobs(repo, states={jobs.LANDED})
    failed = jobs.list_jobs(repo, states={jobs.FAILED})
    assert failed and "check failed" in failed[0].error
    assert not (repo / "src" / "__generated__" / "app.py").exists()  # nothing landed
```

(The `jaunt_cfg` fixture loads a minimal scaffolded `jaunt.toml` in the repo via the same
helper `tests/test_config.py` uses; source root `src`, generated dir `__generated__`.
Reuse the `repo`/`_git` fixture shape from `tests/test_landing.py`, extracted into this
file.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: new tests FAIL (`DaemonState`/`run_once` missing).

- [ ] **Step 3: Implement the loop**

Append to `src/jaunt/daemon.py`:

```python
import json as _json
import subprocess
import sys
import time
from concurrent.futures import Executor, Future
from dataclasses import dataclass, field

from jaunt import jobs as jobs_mod
from jaunt import journal as journal_mod
from jaunt import landing
from jaunt.config import JauntConfig


@dataclass(frozen=True)
class BuildOutcome:
    ok: bool
    refrozen: bool
    error: str = ""


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    battery: str = "-"  # "47/47" when a deterministic battery ran; "-" when none exists
    detail: str = ""    # pre-redacted, single line


@dataclass
class JobResult:
    job_id: str
    build: BuildOutcome
    gate: GateOutcome | None = None
    patch: str = ""
    patch_paths: tuple[str, ...] = ()


@dataclass
class DaemonState:
    last_head: str = ""
    futures: dict[str, Future] = field(default_factory=dict)
    pending: dict[str, JobResult] = field(default_factory=dict)


def drain(state: DaemonState) -> None:
    for fut in list(state.futures.values()):
        fut.result()


class CliRunner:
    """Default runner: drives jaunt's own CLI JSON contracts in a worktree."""

    def _run(self, worktree, *argv) -> dict:
        proc = subprocess.run(
            [sys.executable, "-m", "jaunt", *argv],
            cwd=worktree, capture_output=True, text=True, check=False,
        )
        try:
            return _json.loads(proc.stdout or "{}")
        except _json.JSONDecodeError:
            return {"ok": False, "error": (proc.stderr or proc.stdout)[-500:]}

    def probe(self, worktree):
        payload = self._run(worktree, "status", "--json", "--magic-only")
        stale = payload.get("stale", [])
        changes = payload.get("stale_changes", {})
        digests = payload.get("digests", {})
        return {m: changes.get(m, "structural") for m in stale}, digests

    def build(self, worktree, module) -> BuildOutcome:
        # --no-repo-map / --no-auto-skills keep the build from writing treedocs.yaml
        # and skill files in the worktree — tracked side effects that would trip the
        # landing allowlist. Cache writes land in .jaunt/ (gitignored, invisible).
        payload = self._run(
            worktree, "build", "--target", module, "--json",
            "--no-repo-map", "--no-auto-skills",
        )
        if module in payload.get("refrozen", []):
            return BuildOutcome(ok=True, refrozen=True)
        if module in payload.get("generated", []):
            return BuildOutcome(ok=True, refrozen=False)
        error = str(payload.get("failed", {}).get(module, payload.get("error", "build failed")))
        return BuildOutcome(ok=False, refrozen=False, error=error.splitlines()[0][:200])

    def gate(self, worktree, module) -> GateOutcome:
        # Deterministic, model-free gate: `jaunt check` re-runs committed contract
        # batteries and drift checks with no API key (build-internal validation — AST,
        # import provenance, ty — already ran inside `build`). Repos with no contracts
        # pass trivially and the journal shows battery "-".
        payload = self._run(worktree, "check", "--json")
        if payload.get("ok", False):
            battery = str(payload.get("battery") or "-")
            return GateOutcome(ok=True, battery=battery)
        detail = str(payload.get("error", "jaunt check failed")).splitlines()[0][:200]
        return GateOutcome(ok=False, detail=detail)


def _head(repo) -> str:
    return landing.git_out(repo, "rev-parse", "HEAD").strip()


def _branch(repo) -> str:
    return landing.git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _worktrees_dir(root):
    return root / ".jaunt" / "worktrees"


def _remove_worktree(root, path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
        capture_output=True, text=True, check=False,
    )


def _execute_job(root, cfg, job: jobs_mod.JobRecord, runner) -> JobResult:
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
            # Nested generated dirs (src/pkg/__generated__/mod.py) and their
            # .contract.json sidecars pass; whole source roots do NOT.
            return f"/{gen}/" in f"/{path}" or path == journal_mod.JOURNAL_FILE

        patch = landing.extract_patch(wt, job.base_commit, is_allowed=_machine_owned)
        paths = tuple(landing.changed_paths(wt, job.base_commit))
        return JobResult(job_id=job.id, build=build, gate=gate, patch=patch, patch_paths=paths)
    finally:
        _remove_worktree(root, wt)


def run_once(root, cfg: JauntConfig, state: DaemonState, runner, pool: Executor) -> None:
    # 1) Probe on HEAD movement FIRST, so supersede always runs before any landing.
    head = _head(root)
    if head != state.last_head:
        state.last_head = head
        probe = _worktrees_dir(root) / "probe"
        _remove_worktree(root, probe)
        landing.git_out(root, "worktree", "add", "--detach", str(probe), head)
        try:
            stale, digests = runner.probe(probe)
        finally:
            _remove_worktree(root, probe)
        branch = _branch(root)
        for module, _change in sorted(stale.items()):
            digest = digests.get(module, "")
            existing = jobs_mod.active_for_module(root, module)
            if existing is not None:
                if existing.spec_digest == digest:
                    continue  # same contract — in-flight job still valid
                jobs_mod.mark(root, existing, jobs_mod.SUPERSEDED)
                state.futures.pop(existing.id, None)
                state.pending.pop(existing.id, None)
            job = jobs_mod.JobRecord.new(
                module=module, spec_digest=digest, base_commit=head, branch=branch
            )
            jobs_mod.save_job(root, job)

    # 2) Move finished futures into the pending-landing set.
    for job_id, fut in list(state.futures.items()):
        if fut.done():
            del state.futures[job_id]
            state.pending[job_id] = fut.result()

    # 3) Land pending results — only for jobs still RUNNING, only onto the probed HEAD.
    for job_id, result in list(state.pending.items()):
        job = jobs_mod.load_job(root, job_id)
        if job is None or job.state != jobs_mod.RUNNING:
            del state.pending[job_id]  # superseded while running
            continue
        if not result.build.ok or (result.gate is not None and not result.gate.ok):
            detail = result.build.error or (result.gate.detail if result.gate else "gate failed")
            jobs_mod.mark(root, job, jobs_mod.FAILED, error=detail)
            journal_mod.append_events(
                root, [journal_mod.JournalEvent("job-fail", job.module, detail, job.id)]
            )
            del state.pending[job_id]
            continue
        cause = "cosmetic (gate: EQUIVALENT)" if result.build.refrozen else "spec change"
        message = landing.build_commit_message(job.module, cause, job.id, job.spec_digest)
        sha = landing.land(
            root, result.patch, patch_paths=list(result.patch_paths),
            message=message, expected_branch=job.branch, expected_head=state.last_head,
        )
        if sha == landing.HEAD_MOVED:
            continue  # keep pending; next iteration re-probes and supersedes or lands
        del state.pending[job_id]
        if sha is None:
            (jobs_mod.jobs_dir(root) / f"{job.id}.patch").write_text(result.patch, encoding="utf-8")
            jobs_mod.mark(
                root, job, jobs_mod.PARKED,
                patch_paths=_json.dumps(list(result.patch_paths)),
            )
            journal_mod.append_events(
                root, [journal_mod.JournalEvent("job-park", job.module, "landing conflict", job.id)]
            )
        else:
            # Our own landing commit moved HEAD; adopt it so the next pending result
            # can land this iteration. Safe: regen commits touch only machine-owned
            # paths and can never change a spec digest.
            state.last_head = sha
            action = "refreeze" if result.build.refrozen else "build"
            battery = result.gate.battery if result.gate else "-"
            jobs_mod.mark(root, job, jobs_mod.LANDED, landed_commit=sha)
            journal_mod.append_events(
                root,
                [journal_mod.JournalEvent(action, job.module, f"{cause}; battery {battery}", job.id)],
            )

    # 4) Spawn queued jobs up to the concurrency cap.
    max_jobs = cfg.daemon.max_jobs or cfg.build.jobs
    for job in jobs_mod.list_jobs(root, states={jobs_mod.QUEUED}):
        if len(state.futures) >= max_jobs:
            break
        job = jobs_mod.mark(root, job, jobs_mod.RUNNING)
        state.futures[job.id] = pool.submit(_execute_job, root, cfg, job, runner)


def run_daemon(root, *, runner=None, iterations: int | None = None, sleep=time.sleep) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from jaunt.config import load_config

    cfg = load_config(root=root)  # keyword-only in config.py
    runner = runner or CliRunner()
    state = DaemonState()
    # recover(root) is added by Task 8 and called here, before the loop.
    max_jobs = cfg.daemon.max_jobs or cfg.build.jobs
    count = 0
    with ThreadPoolExecutor(max_workers=max_jobs) as pool:
        while iterations is None or count < iterations:
            if os.environ.get(DISABLE_ENV):
                break
            run_once(root, cfg, state, runner, pool)
            count += 1
            sleep(cfg.daemon.poll_interval)
        drain(state)
        run_once(root, cfg, state, runner, pool)  # final collection pass
```

Implementation notes:
- The `expected_head` discipline means landings serialize against probes: nothing lands
  onto a HEAD the daemon hasn't probed. A user commit between probe and land defers the
  landing by one iteration (correctness over latency). The daemon's own landing commits
  update `state.last_head` in place, which is safe because they can never alter spec
  digests.
- `jaunt check --json`: verify what count fields its payload actually exposes and surface
  them as the `battery` string; if it exposes none, keep `"-"`. Do not invent keys.
- If `jaunt build` grows new tracked side effects later, they surface as loud
  `LandingError: job touched paths outside allowlist` failures — by design. Extend the
  build flags, never the predicate.

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_daemon.py tests/test_landing.py -v`
Expected: PASS.

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/daemon.py src/jaunt/landing.py tests/
git commit -m "feat(daemon): poll/probe/enqueue/execute/land core loop with supersede"
```

---

### Task 8: Crash recovery + worktree hygiene

**Files:**
- Modify: `src/jaunt/daemon.py`
- Test: `tests/test_daemon.py` (extend)

**Interfaces:**
- Consumes: `jobs.*`, `landing.git_out`.
- Produces: `recover(root: Path) -> list[str]` — called at daemon start before the loop: RUNNING/GREEN jobs → `FAILED` with `error="orphaned by daemon restart"`; `git worktree prune` + remove leftover `.jaunt/worktrees/*` dirs; returns affected job ids.

- [ ] **Step 1: Write the failing test**

```python
def test_recover_orphans_and_prunes_worktrees(repo, jaunt_cfg):
    job = jobs.JobRecord.new(module="app", spec_digest="d", base_commit=_git(repo, "rev-parse", "HEAD"), branch="main")
    jobs.save_job(repo, job)
    jobs.mark(repo, job, jobs.RUNNING)
    stray = repo / ".jaunt" / "worktrees" / "zombie"
    _git(repo, "worktree", "add", "--detach", str(stray), "HEAD")
    affected = daemon.recover(repo)
    assert job.id in affected
    assert jobs.load_job(repo, job.id).state == jobs.FAILED
    assert not stray.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daemon.py::test_recover_orphans_and_prunes_worktrees -v`
Expected: FAIL (`recover` missing).

- [ ] **Step 3: Implement `recover` and call it from `run_daemon` before the loop**

```python
def recover(root) -> list[str]:
    affected = []
    for job in jobs_mod.list_jobs(root, states={jobs_mod.RUNNING, jobs_mod.GREEN}):
        jobs_mod.mark(root, job, jobs_mod.FAILED, error="orphaned by daemon restart")
        affected.append(job.id)
    wt_dir = _worktrees_dir(root)
    if wt_dir.exists():
        for path in wt_dir.iterdir():
            subprocess.run(
                ["git", "-C", str(root), "worktree", "remove", "--force", str(path)],
                capture_output=True, text=True, check=False,
            )
    subprocess.run(
        ["git", "-C", str(root), "worktree", "prune"], capture_output=True, text=True, check=False
    )
    return affected
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_daemon.py -v`
Expected: PASS.

```bash
git add src/jaunt/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): crash recovery — orphan jobs failed, worktrees pruned"
```

---

### Task 9: `jaunt jobs` CLI — list, would-rebuild preview, show, retry

**Files:**
- Modify: `src/jaunt/cli.py`
- Test: `tests/test_cli_jobs.py`

**Interfaces:**
- Consumes: `jobs.*`, `landing.land`, `compute_magic_status` (existing, `status_core.py`).
- Produces: `jaunt jobs [--json]` (job records + `would rebuild:` preview from working-tree staleness); `jaunt jobs show <id> [--full]`; `jaunt jobs retry <id>` (parked job → re-`land` from saved `.jaunt/jobs/<id>.patch`; on success mark LANDED, else stay PARKED, exit 4).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_cli_jobs.py
from pathlib import Path

from jaunt import jobs
from jaunt.cli import main


def test_jobs_list_empty(tmp_path: Path, capsys, monkeypatch, scaffolded_project):
    monkeypatch.chdir(scaffolded_project)
    rc = main(["jobs", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert '"jobs": []' in out.replace(" ", "").replace("\n", "") or '"jobs":[]' in out.replace(" ", "")


def test_jobs_show_full_reads_detail_log(tmp_path: Path, capsys, monkeypatch, scaffolded_project):
    monkeypatch.chdir(scaffolded_project)
    root = Path(scaffolded_project)
    job = jobs.JobRecord.new(module="app", spec_digest="d", base_commit="c", branch="main")
    detail = jobs.jobs_dir(root) / f"{job.id}.log"
    detail.parent.mkdir(parents=True, exist_ok=True)
    detail.write_text("full assertion diff here\n", encoding="utf-8")
    jobs.save_job(root, jobs.mark(root, job, jobs.FAILED, error="battery 45/47", detail_log=str(detail)))
    rc = main(["jobs", "show", job.id, "--full"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "battery 45/47" in out and "full assertion diff here" in out
```

(`scaffolded_project`: reuse/extract the minimal project fixture used by
`tests/test_cli_status.py` — a `jaunt init`-shaped tmp dir inside a git repo. Add a retry test
mirroring `test_land_parks_on_conflict` from Task 5: park a job with a saved patch, resolve the
conflict by committing the expected base content, run `main(["jobs", "retry", job.id])`, assert
exit 0 and state LANDED.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_jobs.py -v`
Expected: FAIL (invalid choice 'jobs').

- [ ] **Step 3: Implement subparser + `cmd_jobs`**

```python
jobs_p = subparsers.add_parser("jobs", help="Show daemon job records and pending staleness.")
jobs_p.add_argument("--root", default=".")
jobs_p.add_argument("--json", action="store_true", dest="json_mode")
jobs_sub = jobs_p.add_subparsers(dest="jobs_command")
jobs_show_p = jobs_sub.add_parser("show", help="Show one job record.")
jobs_show_p.add_argument("job_id")
jobs_show_p.add_argument("--full", action="store_true", help="Include full local detail log.")
jobs_show_p.add_argument("--root", default=".")
jobs_retry_p = jobs_sub.add_parser("retry", help="Retry landing a parked job.")
jobs_retry_p.add_argument("job_id")
jobs_retry_p.add_argument("--root", default=".")
```

`cmd_jobs` behavior (write it following `cmd_status`'s structure):
- default (no subcommand): print job records (id, module, state, battery, error first line);
  then compute the would-rebuild preview by calling `compute_magic_status` with the same
  argument construction `cmd_status` uses (copy that call) and print
  `would rebuild: <module> (<change kind>)` for each stale module. JSON mode:
  `{"command": "jobs", "ok": true, "jobs": [...], "would_rebuild": {...}}`.
- `show`: load record, print all fields; `--full` also prints the `detail_log` file contents
  when the path is non-empty and exists.
- `retry`: require state PARKED; read `.jaunt/jobs/<id>.patch` and the record's
  `patch_paths` (JSON-encoded list stored at park time — Task 3 field); call `landing.land`
  with `expected_branch=job.branch` and `expected_head` = current `git rev-parse HEAD`
  (retry explicitly accepts the current HEAD as the landing base — the human invoking it
  is the revalidation); on success `mark(..., LANDED, landed_commit=sha)` and exit 0; else
  print the park reason and exit 4.

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_cli_jobs.py -v`
Expected: PASS.

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/cli.py src/jaunt/jobs.py tests/test_cli_jobs.py
git commit -m "feat(daemon): jaunt jobs — list with would-rebuild preview, show --full, retry"
```

---

### Task 10: `jaunt guard` — warn-on-access hook

**Files:**
- Create: `src/jaunt/guard.py`
- Modify: `src/jaunt/cli.py` (subparser + `cmd_guard`)
- Test: `tests/test_guard.py`
- Create: `docs/hooks.md` (installation snippet)

**Interfaces:**
- Consumes: `cfg.paths.generated_dir` (best-effort; falls back to `__generated__` when no config found — the hook must never crash the harness).
- Produces: `guard.evaluate(payload: dict, *, generated_dir: str) -> dict | None` — `None` = allow silently; dict = Claude Code `PreToolUse` hook output asking for confirmation with a redirect message; CLI `jaunt guard` reading the hook JSON from stdin and printing the output JSON (always exit 0).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_guard.py
from jaunt import guard


def _payload(tool: str, path: str) -> dict:
    return {"tool_name": tool, "tool_input": {"file_path": path}}


def test_warns_on_generated_path_edit():
    out = guard.evaluate(_payload("Edit", "src/pkg/__generated__/mod.py"), generated_dir="__generated__")
    assert out is not None
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "src/pkg/mod.py" in decision["permissionDecisionReason"]  # owning-spec hint


def test_allows_normal_paths_and_non_file_tools():
    assert guard.evaluate(_payload("Edit", "src/pkg/mod.py"), generated_dir="__generated__") is None
    assert guard.evaluate({"tool_name": "Bash", "tool_input": {"command": "ls"}}, generated_dir="__generated__") is None


def test_never_raises_on_malformed_payload():
    assert guard.evaluate({}, generated_dir="__generated__") is None
    assert guard.evaluate({"tool_input": None}, generated_dir="__generated__") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_guard.py -v`
Expected: FAIL (no module `jaunt.guard`).

- [ ] **Step 3: Implement `src/jaunt/guard.py`**

```python
"""PreToolUse guard: warn when an agent reads/edits machine-owned generated code."""

from __future__ import annotations

_FILE_KEYS = ("file_path", "path", "notebook_path")


def _owning_spec_hint(path: str, generated_dir: str) -> str:
    parts = path.split("/")
    if generated_dir in parts:
        idx = parts.index(generated_dir)
        return "/".join(parts[:idx] + parts[idx + 1 :])
    return path


def evaluate(payload: dict, *, generated_dir: str) -> dict | None:
    try:
        tool_input = payload.get("tool_input") or {}
        path = next((str(tool_input[k]) for k in _FILE_KEYS if tool_input.get(k)), None)
    except (AttributeError, TypeError):
        return None
    if not path or f"/{generated_dir}/" not in f"/{path}":
        return None
    spec_hint = _owning_spec_hint(path, generated_dir)
    reason = (
        f"{path} is machine-owned generated code (jaunt). Edit the spec instead: "
        f"{spec_hint}. Changes here are overwritten on the next build."
    )
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "ask",
            "permissionDecisionReason": reason,
        }
    }
```

`cmd_guard` in cli.py (subparser `guard`, no arguments beyond `--generated-dir` defaulting to
`__generated__`, overridable; try loading `jaunt.toml` for the real value but swallow all
errors):

```python
def cmd_guard(args: argparse.Namespace) -> int:
    import json as _json

    from jaunt import guard as guard_mod

    try:
        payload = _json.load(sys.stdin)
    except Exception:
        return EXIT_OK
    out = guard_mod.evaluate(payload, generated_dir=args.generated_dir)
    if out is not None:
        print(_json.dumps(out))
    return EXIT_OK
```

- [ ] **Step 4: Write `docs/hooks.md`**

```markdown
# Warn-on-access hook

Add to `.claude/settings.json` in a jaunt project:

​```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|Read|NotebookEdit",
        "hooks": [{"type": "command", "command": "jaunt guard"}]
      }
    ]
  }
}
​```

Agents get a confirmation prompt with a pointer to the owning spec when they touch
`__generated__/**`. For harnesses without hook support (Codex), the barrier is advisory:
`jaunt instructions` states the rule.
```

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/test_guard.py -v`
Expected: PASS.

```bash
uv run ruff check . && uv run ty check
git add src/jaunt/guard.py src/jaunt/cli.py tests/test_guard.py docs/hooks.md
git commit -m "feat(guard): warn-on-access PreToolUse hook for generated code"
```

---

### Task 11: Scaffolding + docs — init integration, CLAUDE.md, journal opt-in

**Files:**
- Modify: `src/jaunt/cli.py` (`cmd_init`: scaffold `JAUNT_LOG`, `.gitattributes` union rule, `.jaunt/` gitignore entry)
- Modify: `CLAUDE.md` (CLI commands section: `daemon`, `jobs`, `log`, `guard`)
- Modify: `README.md` if it lists commands (mirror CLAUDE.md's additions)
- Test: `tests/test_cli_init.py` (extend)

**Interfaces:**
- Consumes: `journal.ensure_union_merge_attribute`, `journal.JOURNAL_FILE`.
- Produces: `jaunt init` creates an empty `JAUNT_LOG`, adds `JAUNT_LOG merge=union` to `.gitattributes`, and ensures `.jaunt/` is in `.gitignore`.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_cli_init.py` following its existing pattern:

```python
def test_init_scaffolds_journal_and_attributes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rc = main(["init"])
    assert rc == 0
    assert (tmp_path / "JAUNT_LOG").exists()
    assert "JAUNT_LOG merge=union" in (tmp_path / ".gitattributes").read_text(encoding="utf-8")
    assert ".jaunt/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_init.py -v`
Expected: new test FAILS.

- [ ] **Step 3: Implement in `cmd_init`** (after the existing scaffolding, same style):

```python
from jaunt import journal as _journal

(root / _journal.JOURNAL_FILE).touch(exist_ok=True)
_journal.ensure_union_merge_attribute(root)
gitignore = root / ".gitignore"
existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
if ".jaunt/" not in existing.splitlines():
    joiner = "" if (not existing or existing.endswith("\n")) else "\n"
    gitignore.write_text(existing + joiner + ".jaunt/\n", encoding="utf-8")
```

- [ ] **Step 4: Update CLAUDE.md CLI section**

Add under the CLI commands block:

```bash
jaunt daemon start            # Background codegen: commit-triggered isolated jobs, auto-commit on green
jaunt daemon stop|status      # Stop / inspect the daemon
jaunt jobs                    # Job records + would-rebuild preview; show <id> [--full]; retry <id>
jaunt log                     # Tail the JAUNT_LOG change journal (-n N, --module X)
jaunt guard                   # PreToolUse hook: warn when agents touch __generated__ (see docs/hooks.md)
```

And a `[daemon]` block in the jaunt.toml example: `poll_interval = 2.0`, `max_jobs = 0  # 0 -> build.jobs`, `notify_command = ""`.

- [ ] **Step 5: Run the full suite, lint, commit**

Run: `uv run pytest`
Expected: PASS (entire suite).

```bash
uv run ruff check . && uv run ty check
git add -A
git commit -m "feat(daemon): init scaffolding for JAUNT_LOG + docs for daemon/jobs/log/guard"
```

---

### Task 12: End-to-end smoke + notify command

**Files:**
- Modify: `src/jaunt/daemon.py` (notify_command execution on job-fail/job-park/landed)
- Test: `tests/test_daemon.py` (extend)

**Interfaces:**
- Consumes: `cfg.daemon.notify_command`.
- Produces: after each landing/parking/failure the daemon runs `notify_command` (if set) via `subprocess.run(shell=True)` with env vars `JAUNT_JOB_ID`, `JAUNT_JOB_MODULE`, `JAUNT_JOB_STATE`; failures of the notify command itself are swallowed (never crash the loop).

- [ ] **Step 1: Write the failing test**

```python
def test_notify_command_fires_with_env(repo, jaunt_cfg_with_notify, tmp_path):
    # jaunt_cfg_with_notify sets notify_command = f"echo $JAUNT_JOB_MODULE:$JAUNT_JOB_STATE >> {tmp_path}/notify.txt"
    runner = FakeRunner()
    state = daemon.DaemonState()
    with ThreadPoolExecutor(max_workers=1) as pool:
        _spec_commit(repo)
        daemon.run_once(repo, jaunt_cfg_with_notify, state, runner, pool)
        daemon.drain(state)
        daemon.run_once(repo, jaunt_cfg_with_notify, state, runner, pool)
    text = (tmp_path / "notify.txt").read_text(encoding="utf-8")
    assert "app:landed" in text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daemon.py -v -k notify`
Expected: FAIL.

- [ ] **Step 3: Implement `_notify` and call it at the three terminal transitions in `run_once`**

```python
def _notify(cfg: JauntConfig, job: jobs_mod.JobRecord) -> None:
    if not cfg.daemon.notify_command:
        return
    env = dict(os.environ, JAUNT_JOB_ID=job.id, JAUNT_JOB_MODULE=job.module, JAUNT_JOB_STATE=job.state)
    try:
        subprocess.run(cfg.daemon.notify_command, shell=True, env=env, timeout=10, check=False)
    except Exception:
        pass
```

- [ ] **Step 4: Full suite, lint, commit**

Run: `uv run pytest && uv run ruff check . && uv run ty check`
Expected: PASS.

```bash
git add src/jaunt/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): notify_command on landed/parked/failed transitions"
```

---

## Review status

**Codex review (2026-07-01, session 019f1f9d):** 11 P1 findings, all applied to this
plan — real `args.command` dispatch, `EXIT_CONFIG_OR_DISCOVERY`, keyword-only
`load_config(root=root)`, a deterministic gate step (`Runner.gate` running `jaunt check`)
between build and landing, `expected_head` revalidation with the `HEAD_MOVED` defer
sentinel (closes the stale-landing race), per-module digests + `--magic-only` exposed by
`jaunt status` as an explicit prerequisite (Task 7 Step 0), side-effect-quiet builds
(`--no-repo-map --no-auto-skills` + gitignored `.jaunt/` enforced at daemon start), a
predicate allowlist that never admits whole source roots, atomic `O_CREAT|O_EXCL`
pidfile locking, and `patch_paths` persisted on `JobRecord` at park time so retry works.

**Deferred (P2, from the same review — fix during implementation, do not drop):**
1. Match the repo's `dest="json_output"` / `_is_json_mode()` convention for the new
   commands' `--json` flags instead of the plan's `json_mode`.
2. `merge=union` does not guarantee newest-last ordering across branch merges — make
   `jaunt log` sort by the leading timestamp on read; file order is best-effort.
3. `GREEN` state is defined but no code path currently sets it — either mark jobs GREEN
   when their future completes (before landing) or drop GREEN from `recover()`.
4. `jaunt guard` should best-effort load `jaunt.toml` for the real `generated_dir` and
   filter on `tool_name` (file tools only), per its own interface description.

- **Spec coverage:** journal (Tasks 1–2), job records + daemon + supersede + recovery
  (Tasks 3, 6–8, 12), landing with hard predicate allowlist + trailers + park + HEAD
  revalidation (Tasks 4–5, 9), status digests + `--magic-only` (Task 7 Step 0),
  `jaunt jobs`/`log` CLI (Tasks 2, 9), guard hook (Task 10), init/docs (Task 11).
  Journal rotation is deferred (file stays small at pilot scale) — a follow-up, not
  silently dropped. Per-module pytest batteries beyond `jaunt check` are deferred to the
  adoption-parity plan, where batteries for the pilot modules actually exist.
- **Type consistency:** `JobRecord` fields referenced by Tasks 7/9 (`gate`, `battery`,
  `detail_log`, `landed_commit`, `error`, `patch_paths`) are all defined in Task 3.
