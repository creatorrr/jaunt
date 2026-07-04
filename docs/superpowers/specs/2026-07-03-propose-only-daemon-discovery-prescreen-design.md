# Propose-only daemon landing + discovery AST prescreen — Design

**Date:** 2026-07-03
**Status:** Approved design (pre-implementation)
**Target release:** jaunt 1.2.0
**Consumers:** mem-mcp-b (first real adopter — see its `docs/jaunt-adoption-guide.md`), all future daemon users
**Extends:** `2026-07-01-jaunt-daemon-background-codegen-design.md`

## Problem & motivation

The mem-mcp-b adoption campaign (settled 2026-07-03) surfaced two upstream gaps:

1. **The daemon can only auto-commit.** A green job flows straight through
   `landing.land()` (`daemon.py:594-652`) into a provenance commit. mem-mcp-b chose a
   propose-only trust ramp: the daemon generates and parks; a human or agent lands
   explicitly. There is no such mode, and auto-commit-by-default is a footgun for every
   *future* first-time adopter too.
2. **Discovery imports everything under `source_roots`.** `import_and_collect`
   (`discovery.py:217-227`) calls `importlib.import_module` on every scanned module,
   spec-bearing or not. In a monorepo like mem-mcp-b (~274 files in the main app,
   side-effectful boot modules, DB pools at import time in the worst cases), broad
   roots are unusable — and narrow roots are semantically wrong, because module
   identity derives from the root-relative path, so pointing a root *inside* a package
   registers specs under the wrong module names.

## Goals

- `[daemon] auto_commit` config; **default `false`** (propose-only becomes the default;
  `true` opts back into today's behavior).
- Parked proposals: first-class job state, patch artifact, explicit
  `jaunt jobs land / discard` verbs, digest re-validation at land time.
- `jaunt jobs wait` treats a parked proposal as terminal-green.
- Discovery imports **only** modules that plausibly define specs (textual prefilter +
  AST marker check via the existing parse cache); everything else is never imported.
- Zero change to digests, freshness, or generation output in either feature.

## Non-goals

- No remote/CI landing service; landing is a local CLI verb.
- No partial landing (a proposal lands atomically or not at all).
- No `[paths]` include/exclude glob system — the prescreen removes the need.
- No auto-supersede *rebuild* chaining (landing a stale proposal is refused; producing
  the fresh replacement job remains the daemon's normal trigger loop).

## Feature 1: propose-only landing

### Config

```toml
[daemon]
auto_commit = false   # NEW — default. true restores auto-commit-on-green.
```

`jaunt daemon status` reports the active landing mode. The default flip ships in
1.2.0's changelog as the headline behavior change; the daemon is ~1 week old with one
known deployment (the jaunt repo's own dogfooding), so the blast radius is us.

### Job lifecycle

Today (from `jobs.py` / `daemon.py:652`): pending → running → gates → `LANDED` (green)
or failed/parked. New states:

- **`PROPOSED`** — gates green, `auto_commit=false`: the job's diff is captured via the
  existing `landing.extract_patch` (`landing.py:32`) and stored with the job record
  (e.g. `.jaunt/jobs/<id>/proposal.patch`), together with the spec digest it was
  generated from and the intended provenance commit message
  (`landing.build_commit_message`, `landing.py:50`). The job worktree is cleaned up as
  usual — the patch is the artifact.
- **`SUPERSEDED`** — a newer job for the same module reaches `PROPOSED`/`LANDED`, or a
  land attempt finds the spec digest stale. Terminal; patch retained for archaeology
  until normal job-record GC.
- **`DISCARDED`** — explicit `jaunt jobs discard <id>`. Terminal.

Multiple proposals per module: when a job for module M reaches `PROPOSED`, any older
`PROPOSED` job for M is auto-marked `SUPERSEDED`.

### `jaunt jobs land <id>` / `land --all` / `discard <id>`

`land <id>`, run in the user's checkout:

1. **Freshness gate:** recompute the module's spec digest at HEAD; mismatch with the
   proposal's recorded digest → refuse, mark `SUPERSEDED`, hint ("spec moved since
   generation; the daemon will propose a fresh build").
2. **Cleanliness gate:** the paths the patch touches must be unmodified in the working
   tree and index → otherwise refuse with the dirty paths listed. No `--force`.
3. **Apply:** `git apply --3way` the patch; any conflict → refuse, mark `SUPERSEDED`
   (conflict means the base moved under the generated files — regeneration is the only
   honest resolution).
4. **Commit:** create exactly the provenance commit auto-commit mode would have made
   (same `build_commit_message` output), mark the job `LANDED` with the commit sha,
   append the journal line.

`land --all`: land every still-fresh `PROPOSED` job in module-DAG dependency order
(reusing the build scheduler's topo order); report per-job outcome; exit 0 only if
every attempted land succeeded, else 4. `discard <id>`: mark `DISCARDED`, journal it.

### `jaunt jobs wait`

`PROPOSED` counts as terminal-green: exit 0 when all watched jobs are
`LANDED`/`PROPOSED`, 4 on failed/parked, 5 on timeout — unchanged codes, widened
green set. The agent loop becomes `jaunt jobs wait && jaunt jobs land --all`.

### JAUNT_LOG

Two new line kinds, same terse format as existing entries:
`propose(<module>): <cause> <job-id>` and `land(<module>): <job-id> <sha>`
(`discard`/`supersede` likewise). The journal stays a complete audit trail whether or
not auto-commit is on.

## Feature 2: discovery AST prescreen

### Mechanism

In the discovery scan, before `importlib.import_module` (`discovery.py:217-227`), each
candidate module file passes two gates:

1. **Textual prefilter:** the source must contain the substring `jaunt`. This skips
   ~95%+ of files in a large monorepo without parsing.
2. **AST marker check** (parsed via the existing persistent parse cache,
   `parse_cache.py`): the module must contain `import jaunt` / `from jaunt import …`,
   or a decorator whose name/attribute is one of `magic` / `test` / `contract` /
   `preserve`. Files that fail to parse are skipped (a module with a syntax error
   cannot define an importable spec) — logged at debug level, never an error.

Only modules passing both gates are imported. The `--target` fast-path
(`discovery.py:202`) bypasses the prescreen — an explicitly named module is imported
unconditionally, preserving today's error messages for typos.

### Semantics and limits

- Modules that import jaunt but define no specs still get imported — harmless (they
  already chose to import jaunt) and keeps the check simple.
- Documented limitation: a module invoking `jaunt.magic(...)` through an alias with no
  textual `jaunt` anywhere in the file is not discovered. Contrived; acceptable; noted
  in the docs-site limitations page.
- No behavior change for registration, digests, or freshness — discovery-only. On
  spec-dense repos (the common case today) the prescreen is a no-op with one extra
  cached parse per file; on monorepos it is both the safety fix and a large perf win.

## Compatibility & rollout

- **1.2.0.** The `auto_commit` default flip is the only behavior change existing users
  can feel; called out in CHANGELOG + docs. The jaunt repo's own daemon usage sets
  `auto_commit = true` to keep its current flow.
- Existing daemon tests run with `auto_commit = true` to stay valid; new lifecycle
  tests run the default path.
- Docs-site updates: `guides/daemon.mdx` (modes, land/discard flow), `reference/cli.mdx`
  (`jobs land/discard`, `wait` semantics), `reference/config.mdx` (`auto_commit`),
  `reference/limitations.mdx` (prescreen alias limitation). `jaunt instructions`
  primer: the wait-then-land loop.

## Testing

- **Lifecycle:** green job with `auto_commit=false` → `PROPOSED` with patch + digest +
  message recorded; `land <id>` → identical commit message/paths as auto-commit mode
  (golden comparison), state `LANDED` + sha; `discard`; newer proposal supersedes
  older; stale-digest land refused + `SUPERSEDED`; dirty-tree land refused; 3way
  conflict refused + `SUPERSEDED`.
- **`land --all`:** dependency order respected; mixed fresh/stale reports per-job; exit
  codes 0/4.
- **`wait`:** `PROPOSED` → exit 0; failed → 4; timeout → 5.
- **Journal:** propose/land/discard/supersede lines appended, `merge=union` safe.
- **Prescreen:** a boobytrapped module (raises at import, no jaunt marker) under a
  source root is never imported; a marker module is; syntax-error file skipped
  quietly; `--target` still imports a boobytrapped named module (and surfaces its
  error); textual prefilter short-circuits (no parse-cache entry created for
  jaunt-free files).
- **Auto-commit compat:** existing daemon suite green with `auto_commit = true`.

## Decision log

- **Propose-only as the default** over keeping auto-commit default — safer
  first-contact for every adopter; the daemon is young enough to flip (one known
  deployment). mem-mcp-b wants it; the jaunt repo opts back in.
- **`jobs land` CLI verb** over daemon-side approve queue or bare patch files —
  landing needs digest re-validation and provenance-commit consistency; a CLI verb in
  the user's checkout is the simplest place that has both.
- **Refuse-and-supersede on any land-time doubt** (stale digest, dirty tree, apply
  conflict) over force/merge options — regeneration is always the honest resolution;
  no `--force` footgun.
- **AST prescreen** over `[paths]` exclude globs — fixes the class of problem (imports
  are opt-in by evidence of specs) instead of pushing per-repo configuration onto
  adopters; globs remain a possible future escape hatch if evidence demands.
