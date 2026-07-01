# Jaunt Daemon: Isolated Background Codegen Jobs + Change Journal

**Date:** 2026-07-01
**Status:** Approved design, pre-implementation
**Consumers:** jaunt (upstream features), mem-mcp-b (first real adopter)

## Context and goal

mem-mcp-b (the Memory Store: a production FastMCP + Postgres memory system) wants to
reduce the implementation detail visible to coding agents and humans, and move the
writing of unit bodies to isolated, background agent jobs. Humans and agents work at
the altitude of specs (signatures + docstring contracts); generated implementations
live below the line, owned by their tests.

Jaunt already ships the trust machinery this requires — input-keyed digests, smart
change detection (Layer A AST digest + Layer B semantic gate + re-freeze), held-out
batteries with redacted repair feedback, import provenance screening, and a model-free
CI gate. What it lacks is the execution model: `jaunt build` is a synchronous
foreground CLI. This design adds the background layer, plus a committed change journal
and a visibility barrier — all upstream in jaunt, with mem-mcp-b adopting via config.

## Decisions (settled during brainstorm)

| Fork | Decision |
|---|---|
| Adoption scope in mem-mcp-b | Both, phased: new code spec-first; existing units converted on touch |
| Where generation runs | Hybrid: local daemon generates; CI re-runs deterministic gates only (never generates, no API key) |
| Landing policy | Auto-commit on green, provenance-tagged |
| Ownership | Upstream in jaunt; mem-mcp-b is config-only |
| Visibility barrier | Warn on access (hook + advisory), not blocking |
| Job architecture | Approach A + thin C: worktree-per-job daemon with a persisted job journal; no queue service, no remote executors yet |
| Change journal | Committed `JAUNT_LOG`, append-only, `merge=union` |

## Architecture

Five components; the first four are jaunt features, the fifth is adoption config.

1. **`jaunt daemon`** — watches spec roots; commit-triggered jobs in isolated git
   worktrees; deterministic gates dispose; green lands as a provenance commit.
2. **`JAUNT_LOG`** — committed, append-only, one terse line per event; doubles as the
   semantic-gate audit trail (principles roadmap item 4).
3. **Warn-on-access hook** — shipped hook config (Claude Code `PreToolUse` + Codex
   equivalent) that warns on reads/edits of `__generated__/**` and redirects to the
   owning spec.
4. **Adoption parity** — async-function and class support for contract mode /
   `jaunt adopt`, plus a DB-fixture battery story (ephemeral Postgres) for
   DB-coupled units.
5. **mem-mcp-b adoption** — `jaunt.toml`, CI `jaunt check` merge gate,
   convert-on-touch policy, hook + journal wiring. No bespoke infra in the repo.

Trust chain: human edits **spec** → daemon generates in **isolation** → deterministic
**gates** dispose → green lands as **provenance commit + journal line** → PR review =
**spec diff + journal** → CI **re-verifies deterministically** before merge.

## Component details

### Trigger model: commits, not saves

Generation jobs launch only when a spec change is **committed**. Invariant: every
`regen` commit has its spec in its parent chain — generating from uncommitted edits
would land code whose spec exists nowhere in history, and `jaunt check` would rightly
flag it for everyone else. On saves, the daemon runs Layer A cheaply and surfaces
pending staleness in `jaunt jobs` ("would rebuild: recall.compress") without spawning
anything. Git history reads: `spec: recall dedupe rule` → `regen(recall): dedupe rule`.

### Job lifecycle

- One JSON record per job in `.jaunt/jobs/`: module, spec digest, base commit, state,
  gate verdict, battery counts, landing SHA.
- States: `queued → running → green → landed | parked | failed | superseded`.
- One active job per module; a newer spec commit for the same module cancels and
  supersedes the running job. Global concurrency respects `[build] jobs`.
- Prose-only changes judged `EQUIVALENT` by the Layer B gate skip the worktree and
  land as a cheap `refreeze(<module>)` commit through the same landing path.

### Isolation and landing

- Each job: `git worktree add .jaunt/worktrees/<job-id> <base-commit>`. Jobs never see
  the developer's dirty working tree.
- On green, the job's diff — scoped to `__generated__/**`, contract sidecars, and the
  journal line — is applied to the branch. If the branch advanced: 3-way apply and
  land, unless this module's spec changed again (supersede + requeue). Conflicts park
  the job with a notification.
- Commits use the developer's git identity plus trailers `Jaunt-Job: <id>` and
  `Jaunt-Spec: <digest8>`.

### Change journal (`JAUNT_LOG`)

One line per event, newest last, human-first and grep-friendly:

```
2026-07-01 14:32Z build    recall.compress — prose change (gate: MEANINGFUL); battery 47/47; job a1b2c3
2026-07-01 14:40Z refreeze recall.rank — cosmetic (gate: EQUIVALENT)
2026-07-01 15:02Z adopt    record.idempotency — battery derived: 12 cases (5 examples, 7 errors)
2026-07-01 15:20Z job-fail recall.fanout — battery 45/47 (derived#3: AssertionError); parked
```

- Failure lines carry only the redacted held-out signal (opaque id + exception class);
  the journal must never leak what the implementer barrier hides.
- `.gitattributes`: `JAUNT_LOG merge=union`. Rotation by year into `docs/` keeps the
  committed file bounded. `jaunt log [-n N] [--module X]` tails and filters.
- Every `EQUIVALENT`/`MEANINGFUL` gate verdict is journaled — closing roadmap item 4's
  audit-trail requirement at the terse tier (full old/new prose stays in the local job
  record).

### CLI surface

`jaunt daemon start|stop|status` (lockfile in `.jaunt/` prevents doubles),
`jaunt jobs [--watch]`, `jaunt jobs show <id> [--full]`, `jaunt jobs retry <id>`,
`jaunt log`.

### Warn-on-access hook

Shipped by jaunt as an installable Claude Code `PreToolUse` hook that intercepts
reads/edits of `__generated__/**` with a warning naming the owning spec path. For
Codex and other harnesses without an equivalent hook mechanism, the barrier is
advisory: the `jaunt instructions` primer and per-directory agent docs state the rule.
Escape hatch stays open (proceed past the warning); the default pressure is toward
the contract. Blocking modes are out of scope for now.

### Adoption parity

Contract mode and `jaunt adopt` currently cover top-level sync functions. This design
requires: async function support, class support, and a battery fixture story for
DB-coupled units (for mem-mcp-b: ephemeral Postgres, reusing its existing compose
tooling). Parity is a prerequisite for convert-on-touch to be honest in memory-api.

## Error handling

- **Typed failures, all journaled.** Generation error (codex failure/timeout): retry
  once, then `failed`. Gate failure (validation, imports): `failed` immediately —
  fail-safe, nothing lands on ambiguity. Battery failure: `failed` with redacted
  journal line; full detail persists in the local job record for the *human*
  (`jaunt jobs show <id> --full`) — the barrier constrains agents, not people.
- **Parked jobs** keep their patch for `jaunt jobs retry` (rebases against new HEAD).
- **Crash recovery:** on restart, rescan `.jaunt/jobs/`, requeue orphans, prune stale
  worktrees (`git worktree prune` + directory cleanup).
- **Hard safety lines:** the daemon writes only `__generated__/**`, contract sidecars,
  and `JAUNT_LOG`; it only appends commits — never rebases, never force-pushes, never
  touches the developer's working tree. Branch switches park in-flight landings.
- **Kill switch:** `jaunt daemon stop`; `JAUNT_DAEMON_DISABLE=1`.

## Testing

Same discipline as jaunt's existing suite — mocked generator backend, no API keys.

- Daemon: tmp-git-repo fixtures, fake backend, synchronously driven watch events;
  assert job records, commits, journal lines.
- Landing mechanics: scripted git scenarios — branch advanced, supersede, conflict,
  park/retry, crash/restart.
- Journal: format, redaction property (no derived detail beyond opaque id + exception
  class), rotation.
- Hook: standalone script tests.
- Parity: async/class tests mirroring existing contract-mode tests.
- Dogfood: jaunt's own repo runs the daemon before mem-mcp-b does.

## mem-mcp-b rollout

- **Phase 1 — pilot slice:** one bounded, mostly-pure unit (e.g., RECALL's
  compression/scoring step or a `packages/python` module); a handful of adopted
  specs; daemon running locally; CI wired with model-free `jaunt check`.
- **Phase 2 — policy:** `JAUNT_LOG` at repo root; warn-on-access hook in the repo's
  agent configs; convert-on-touch as an advisory CI comment.
- **Phase 3 — expand:** promote convert-on-touch to a required check; extend into
  memory-api's DB-coupled paths once the ephemeral-Postgres battery fixture story is
  proven on the pilot.

Convert-on-touch rule: a PR that modifies the body of an eligible existing unit must
first adopt it (spec + battery). Advisory at launch, promotable to CI-required.
**Eligible** = a unit that adoption parity can express: top-level functions (sync or
async) and classes under the configured source roots — excluding migrations, infra
scripts, and generated code itself. Eligibility widens as parity does.

## Out of scope

- Remote/pluggable job executors (the thin-C job journal leaves the seam).
- Any test-driven repair loop (heldout redaction already gates it when it comes).
- Blocking modes for the visibility barrier (warn only).
- Spec-as-source purity: generated bodies stay committed (demoted via gitattributes) —
  a production service needs greppable source and honest stack traces at 3am.

## Success criteria

- A spec commit lands working generated code without anyone reading a body.
- Regen PR review time ≈ spec-diff review time; generated diffs hidden in PRs.
- CI merge gate stays model-free and deterministic.
- `JAUNT_LOG` becomes the catch-up artifact agents and humans actually read.
- Every semantic-gate verdict is auditable after the fact.
