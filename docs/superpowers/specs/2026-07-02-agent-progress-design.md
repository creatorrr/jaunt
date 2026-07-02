# Agent-Friendly Progress + `jaunt jobs wait`

**Date:** 2026-07-02
**Status:** Designed

## Problem

Two ergonomic gaps bite coding agents (Claude Code, Codex, CI scripts) driving jaunt:

1. **Progress is invisible off-TTY.** `ProgressBar` (`src/jaunt/progress.py`) renders
   carriage-return bars gated on `sys.stderr.isatty()` (`cli.py:1869`, `cli.py:2263`).
   A non-TTY caller — every coding agent — gets *nothing* for the entire multi-minute
   codex build, so agents kill "hung" builds or burn tokens re-checking.
2. **No blocking completion primitive for the daemon.** The daemon runs jobs in the
   background; the only push hook is `[daemon] notify_command` (`daemon.py:387-408`),
   which doesn't help a synchronous agent. Today an agent must poll `jaunt jobs` /
   `jaunt log` in a sleep loop — exactly the token-wasting pattern we want to kill.

## Decisions (settled with the user, 2026-07-02)

| Question | Decision |
|---|---|
| Progress activation | **`--progress {auto,rich,plain,none}` flag; `auto` default** — rich on TTY, plain lines off-TTY. Agents get progress with zero flags. |
| Agent format | **Plain text lines** (one per event), not NDJSON — readable, cheap in tokens. |
| Completion primitive | **`jaunt jobs wait [<id>]`** — one blocking call, exit code tells the outcome. |
| Daemon in-flight visibility | **Heartbeat in the job record + `wait` streams it** — the blocking call doubles as the live progress view. |
| Push automation | **Unchanged** — `[daemon] notify_command` stays; docs show both patterns side by side. |

## Design

### 1. Progress modes

- New common flag `--progress {auto,rich,plain,none}` on `build`, `test`, `watch`,
  and `jobs wait`. Resolution:
  - `auto` (default): `rich` when `sys.stderr.isatty()`, else `plain`.
  - `rich`: today's carriage-return/rich rendering, forced on.
  - `plain`: line-per-event rendering (below).
  - `none`: no progress output.
- `--no-progress` is retained as an alias for `--progress none` (mutually exclusive
  with an explicit `--progress`; last-wins is fine, keep argparse simple: `--no-progress`
  simply overrides the mode to `none` after parsing).
- `--json` continues to default progress to `none` (`cli.py:1865-1872` logic keeps its
  json_mode guard), but an **explicit** `--progress plain` now works alongside `--json`:
  progress lines go to stderr, the JSON result stays clean on stdout.
- Implementation: `ProgressBar` gains `mode: str` (`"rich" | "plain"`). Plain mode:
  - never writes `\r` or ANSI, never uses the rich console;
  - `phase(item, stage, detail)` emits `[<label>] <item>: <stage> (<detail>)\n`;
  - `advance(item, ok)` emits `[<label>] <done>/<total> ok=<ok> fail=<fail> <item>\n`;
  - `finish()` emits `[<label>] done <done>/<total> ok=<ok> fail=<fail>\n`;
  - the `min_interval_s` throttle does not apply to plain mode (events are already
    sparse — one per phase change / module completion — and dropping events is worse
    than the extra lines).
- The three `ProgressBar` construction sites (`cli.py:1865-1875` build, `cli.py:2261-2264`
  test, `cli.py:2873-2879` skills) route through one shared helper
  `_make_progress(args, *, label, total, json_mode) -> ProgressBar | None` so mode
  resolution lives in exactly one place.

### 2. Daemon job heartbeat

- `JobRecord` (`src/jaunt/jobs.py:27`) gains `phase: str = ""`. Default keeps old
  records loading (`load_job` passes `**json`; missing key → dataclass default).
- The daemon's builds are **subprocesses** (`CliRunner._run`, `daemon.py:89-130`), so
  the heartbeat seam is the child's stderr: `CliRunner.build` adds `--progress plain`
  to the child argv, reads the child's stderr line-by-line while it runs, and after
  each line calls a `heartbeat(line)` callback. The daemon wires that callback to
  `jobs.mark(root, job, RUNNING, phase=<last line, stripped, truncated to 160 chars>)`,
  throttled to at most one record write per second (drop intermediate lines; the
  *latest* line is the heartbeat).
- Terminal `mark(...)` calls (GREEN/FAILED/LANDED/PARKED, e.g. `daemon.py:361-385`)
  set `phase=""` so stale phases never outlive a finished job.
- Display: `jaunt jobs` list and `jaunt jobs show <id>` print the phase after the
  state for RUNNING jobs (`running — [build] mymod: generating (calling codex)`);
  `daemon status` job lines (`cmd_daemon`, `cli.py:648`) include it the same way.
  JSON outputs gain a `"phase"` key.

### 3. `jaunt jobs wait` — the blocking primitive

```
jaunt jobs wait [<id>] [--timeout N] [--settle N] [--json] [--progress MODE]
```

- **Scope.** With `<id>`: block until that job leaves the active states
  (`ACTIVE_STATES = {queued, running, green}`, `jobs.py:19`). Without: block until
  the daemon is **idle** — no job in any active state.
- **Settle window (race guard).** An agent typically commits and immediately runs
  `wait`, but the daemon polls HEAD every `[daemon] poll_interval` (default 2.0 s) —
  a naive check would see "idle" before the job is even enqueued. `wait` therefore
  only exits when idleness has held **continuously for the settle window**: default
  `2 × poll_interval` (from loaded config), override with `--settle N` (seconds,
  `0` disables).
- **Streaming.** Poll job records every 1 s (plain file reads via `list_jobs`; no
  daemon IPC). Whenever any watched job's `(state, phase)` changes, print one line in
  the resolved progress mode (plain: `[wait] <id> <module>: <state> — <phase>`);
  `--progress none` waits silently. On each completion print a summary line
  (`[wait] <id> <module>: landed` / `failed: <error>`).
- **Exit codes.**
  - `0` — reached idle / target job done, and every job that completed during the
    wait ended `landed` (or `green`→`landed`); also `0` when there was nothing to
    wait for at all ("daemon idle, no active jobs").
  - `4` — at least one watched job ended `failed` or `parked` (attention needed;
    parked means built-but-unlandable, e.g. dirty journal — the agent must act).
  - `5` — `--timeout N` seconds elapsed first (**new exit code**; added to the
    exit-code tables in CLAUDE.md / DOCS.md / instructions). Default: no timeout.
  - `2` — daemon not running (`probe_lock`, `daemon.py:227`) **and** no active jobs
    to drain: waiting would hang forever, so fail fast with a hint to run
    `jaunt daemon start`. If the daemon is dead but active-state records exist,
    also exit `2` but say the daemon died mid-job (records are stale, `jaunt jobs
    retry` may apply). A missing `.jaunt/jobs/` dir is just "no jobs" → `0`.
- **`--json`** emits a final report on stdout:
  `{"command": "jobs", "action": "wait", "ok": true, "timed_out": false,
    "jobs": [{"id", "module", "state", "phase", "error"}, ...]}` where `jobs` lists
  every job that was active at any point during the wait (or the single target job).
  `ok` mirrors exit 0. Streaming lines respect `--progress` on stderr as usual.
- Unknown `<id>` → error to stderr, exit `2` (matches `jobs show`'s handling of
  unknown ids).

### 4. Documentation and agent discovery

- `jaunt instructions` (`src/jaunt/instructions/__init__.py`): document `--progress`
  under common flags, add `jobs wait` to COMMANDS with the exit-code table, and add
  one "agent loop" example: `git commit … && jaunt jobs wait --timeout 1800`.
- CLAUDE.md + DOCS.md/docs-site: `--progress` flag, `jobs wait`, exit code `5`, and
  a short "Automation" box showing blocking (`jobs wait`) vs push (`notify_command`)
  side by side.

## Error handling

- Progress rendering stays best-effort: plain mode inherits `_write`'s "disable on
  first failure" behavior (`progress.py:80-88`); a broken pipe never fails a build.
- Heartbeat writes are best-effort: a failed `save_job` during heartbeat is swallowed
  (the terminal `mark` is unchanged and still authoritative).
- `wait` treats unreadable/corrupt job records as absent (existing `load_job`
  semantics, `jobs.py:74-81`) — a corrupt record cannot wedge the wait loop.
- `wait` with `--timeout 0` or negative → argparse error, exit `2`.
- Ctrl-C during `wait` → default KeyboardInterrupt (130); no cleanup needed since
  `wait` only reads.

## Out of scope

- NDJSON/structured event streams (plain lines won; revisit if a consumer needs it).
- Progress for `reconcile`/`check` (fast, no codex calls today).
- Any change to `notify_command` semantics or new push channels (webhooks, MCP).
- `jaunt log --follow` (the journal remains a history view; `wait` is the live view).
- Windows TTY quirks beyond what `isatty` already handles.

## Testing

Same discipline as the suite — mocked backends, no daemon required, no API keys:

- **Plain renderer:** drive `ProgressBar(mode="plain")` against a `StringIO` (non-TTY):
  phase/advance/finish line formats, no `\r`/ANSI ever, no throttling drops.
- **Mode resolution:** `_make_progress` truth table — TTY×{auto,rich,plain,none} ×
  json_mode × `--no-progress` alias.
- **Heartbeat:** fake child process (script printing plain lines to stderr, then
  exiting) through `CliRunner.build` → job record's `phase` tracks the last line,
  ≤1 write/s, cleared on terminal `mark`; old-format record JSON (no `phase` key)
  still loads.
- **`jobs wait`:** synthetic `.jaunt/jobs/*.json` records flipped by a background
  thread — target-id wait, idle wait, settle window (enqueue *after* wait starts,
  inside the settle window, still caught), failed→4, parked→4, timeout→5, daemon
  dead + active records→2, daemon dead + no jobs→0 path via a monkeypatched
  `probe_lock`, `--json` report shape.
- **CLI e2e:** `jaunt build --progress plain` on a tmp project with the mocked
  backend emits phase/advance lines to stderr while `--json` stdout stays valid JSON.

## Success criteria

- An agent running `jaunt build` in a non-TTY shell sees per-module progress lines
  by default and a clean JSON result with `--json --progress plain`.
- `git commit && jaunt jobs wait` is a complete agent automation loop: one blocking
  call, live heartbeats while it runs, exit code telling green/attention/timeout —
  zero polling loops.
- A human's terminal experience is unchanged unless they ask for a mode.
