# Adoption Parity — async functions, classes, and the fixture seam for contract mode

**Date:** 2026-07-02
**Status:** Designed
**Prerequisite of:** mem-mcp-b rollout phases 2–3 (convert-on-touch honesty), per
`2026-07-01-jaunt-daemon-background-codegen-design.md` §Adoption parity.

## Problem

Contract mode (`@jaunt.contract`, `jaunt adopt/reconcile/check/eject`) covers only
top-level **sync functions taking one positional argument**. The restriction is
enforced in four places:

1. The `@jaunt.contract` runtime gate (`runtime.py:490-500`) rejects classes, async
   functions, methods, and class/staticmethods outright.
2. The contract digest path (`load_function_node`/`contract_digests`,
   `digest.py:438-481`) is a *separate, function-only* digest engine parallel to the
   richer class-aware machinery whole-class `@magic` uses (`normalized_contract`,
   `_normalized_members`).
3. The AST marker editor (`contract/edits.py:8-13`) only finds `ast.FunctionDef`.
4. The battery pipeline (`derive.py`, `battery.py`, `runner.py`) hardcodes a
   single-sync-call convention — `assert f(arg) == want` — in the docstring case
   grammar, the emitted pytest tests, the in-process `evaluate_blocks` used by
   reconcile, and mutation-strength scoring. There is no fixture scaffolding at all.

The daemon design requires parity — async support, class support, and a battery
fixture story for DB-coupled units — before convert-on-touch can be an honest rule
in mem-mcp-b ("eligible = a unit adoption parity can express").

## Decisions (settled with the user, 2026-07-02)

| Question | Decision |
|---|---|
| Class adoption unit | **Whole class** (`jaunt adopt module:Cls`); method-level adoption out of scope |
| Async battery style | **Native async tests** — `async def test_*` + `await`, runner passes `-p pytest_asyncio -o asyncio_mode=auto` (pytest-asyncio, ships in base) |
| Fixture story | **Generic seam only** — fixture-parameterized derived tests + documented `tests/contract/conftest.py`; the ephemeral-Postgres fixture is mem-mcp-b's conftest, never jaunt code |
| Case grammar | **Full upgrade** — multi-arg + kwargs calls, constructor recipes, fixture-name inputs; one grammar feeding all three consumers |
| Digest path | **Unify onto the class-aware machinery, keep plain-sync-function rendering byte-identical** — no spurious drift, no migration for existing adopted contracts |

## Design

### 1. Case grammar and the call-plan IR

Derived-case blocks in contract docstrings become **call expressions**:

```
Examples:
    slugify("Hello World") == "hello-world"
    join(["a", "b"], sep="-") == "a-b"
    Counter(start=10).increment(5) == 15

Errors:
    parse("") raises ValueError
    Counter(start=-1) raises ValueError

Fixtures: db
    lookup(db, "alice") == User("alice")
```

- The current bare form (`arg -> want`) remains valid **sugar** for a single-arg
  call on the adopted function — existing contracts parse identically and produce
  identical batteries (no drift).
- Each case parses (via `ast.parse` of the expression, never `eval` of untrusted
  strings at parse time) into a small **call-plan IR**: receiver recipe
  (none | constructor call), call chain, args/kwargs, expectation
  (equality | raises), and referenced fixture names.
- The IR has exactly three consumers, so they cannot diverge:
  1. the pytest battery renderer (`derive_regions`),
  2. in-process validation (`evaluate_blocks`, used by `reconcile`),
  3. mutation-strength scoring (`compute_strength`).
- A `Fixtures:` line names pytest fixtures; a fixture name referenced as an
  argument in a case becomes a parameter of the emitted test function.
- **Allowed names.** A case expression may reference: the adopted target,
  declared fixture names, Python builtins, and top-level names importable from
  the adopted module (e.g. `User` in `lookup(db, "alice") == User("alice")`).
  Any other name is a parse-time error. All three consumers resolve names the
  same way: the battery renderer emits an explicit
  `from <module> import <name>` per referenced module-level name, and
  in-process evaluation builds its namespace from exactly those imports plus
  builtins — never the whole module namespace.

### 2. Async functions

- Remove the `iscoroutinefunction` gate (`runtime.py:495-496`) and the
  `AsyncFunctionDef` rejection (`digest.py:446-447`); `_find_func` in
  `contract/edits.py` matches both function node types.
- Signature digest renders the `async` prefix via a **contract-specific
  signature renderer** (the shared `_function_signature` renders only
  args + return and must not change): sync rendering stays byte-for-byte
  identical to today's, async targets get the `async def` prefix, so a
  sync↔async flip is `SIGNATURE_DRIFT` (blocking), not silent.
- Battery emission: `async def test_*` with `await`ed calls. `run_battery_file`
  appends `-p pytest_asyncio -o asyncio_mode=auto` so pytest-asyncio (a base
  dependency, lock has 1.3.0) collects them even in environments where pytest
  plugin autoload is disabled. The flags are scoped to jaunt's own battery
  invocation; hand-written tests appended to a battery file inherit them, which
  is documented behavior.
- In-process paths (`evaluate_blocks`, strength) wrap coroutine calls in
  `asyncio.run()`. Projects whose fixtures require anyio semantics still work at
  battery (pytest) level; the in-process path only ever runs pure cases (§4).

### 3. Classes

- `jaunt adopt module:Cls` inserts `@jaunt.contract` above the `ClassDef`
  (marker editor learns class nodes and dotted-name rejection with a clear error
  for `module:Cls.method` — "adopt the whole class").
- The runtime gate admits classes. Class identity follows the whole-class
  `@magic` convention: undotted `qualname` + `isinstance(entry.obj, type)`
  (`class_name` stays reserved for method specs and remains unset).
- **Digest unification:** `contract_digests` reuses the class-aware analysis
  (`_normalized_members` and friends) but through a **contract-specific class
  normalizer** that emits three separate strings — `normalized_contract` as-is
  cannot be used, since it gives classes `signature=""`, prose = class docstring
  only, and folds method docstrings/signatures/bodies into one members payload
  (which would misclassify every method edit as `SIGNATURE_DRIFT`):
  - *prose* = class docstring + public (non-underscore) method docstrings,
    cleaned, in stable member order;
  - *signature* = class shape only — method names, signatures (including
    async-ness), bases, class-level attribute names — no docstrings, no bodies;
  - *body* = concatenated method bodies minus docstrings, with **no stub-body
    elision**: the magic-path rule that hashes `pass`/`...`/
    `raise NotImplementedError` bodies as `""` does not apply to contract
    digests, for classes or functions (adopted code is real code; eliding would
    also change existing function digests).
  For plain sync functions the rendered digest inputs are **byte-identical to
  today's** — verified by a dedicated regression test including stub-shaped
  bodies — so existing adopted batteries show no spurious drift and need no
  migration.
- Drift semantics fall out: add/remove/re-sign a method → `SIGNATURE_DRIFT`;
  edit a class or method docstring → `STALE_PROSE`; body-only edits →
  `REFACTORED` (non-blocking); battery failure → `BEHAVIOR_DRIFT`. All existing
  states, no new ones. The state machine's existing precedence is kept: when one
  edit changes both prose and shape (e.g. adding a documented method),
  `STALE_PROSE` wins — both states block `check` equally, and "reconcile this"
  is the right message for that edit anyway.
- **Battery layout:** one file per class, `test_<qualname_sanitized>.py`
  (dots → underscores), derived regions keyed per documented public method.
  Construction comes from the case grammar's constructor recipes; a method whose
  docstring has no derivable cases simply gets no derived region (same rule as
  functions today).
- `eject` removes the class marker and leaves the battery as plain pytest,
  unchanged semantics.

### 4. Fixture seam

- Declaration: the `Fixtures:` line in a derived-case block (§1). No decorator
  kwargs — the docstring is the contract, and fixture needs are part of it.
- Emission: fixture names become parameters of the emitted test functions;
  pytest resolves them from `tests/contract/conftest.py` (or any conftest on the
  normal discovery path). Jaunt never writes a conftest; it documents the
  location and ships an example in the docs.
- **Validation split:** reconcile validates *pure* cases in-process (fast path,
  unchanged) and validates *fixture-dependent* cases by rendering the **merged
  final battery content (preserved hand-written regions included) to a temp
  sibling file under the same `tests/contract` subtree** (so conftest discovery
  applies), running it through `run_battery_file`, and atomically replacing the
  real battery only on success. On failure nothing is written — preserving the
  existing invariant (and its tests) that a failed reconcile leaves no battery
  behind. Validating the merged content means a failing preserved hand test also
  blocks reconcile, mirroring what `check` would report anyway.
- **Strength scoring covers pure cases only.** Mutating source and re-running
  pytest per mutant is unbounded for DB fixtures. The battery header keeps
  `strength` machine-readable as today's `N/M` (`parse_strength`, and `eject`'s
  weak-contract warning, keep working) and adds a separate
  `strength-excluded: K` field; the CLI formats the human-facing
  `strength 4/5 (2 fixture cases not scored)` from the two fields — no silent
  caps.
- `jaunt check` is unchanged in character: it runs committed batteries through
  pytest deterministically, fixtures and all, with no model and no API key. A
  missing fixture at check time is an ordinary pytest error surfacing as
  `BEHAVIOR_DRIFT`.

### 5. Error handling

- Unparseable case expression → reconcile aborts with the offending docstring
  line and a hint; nothing is written.
- Case referencing an undeclared name (not the target, not a declared fixture)
  → same parse-time abort; prevents accidental dependence on module globals.
- Async target whose cases are validated in-process: event-loop errors surface
  as ordinary case failures with the case text.
- Class with zero documented public methods → adopt succeeds, battery contains
  header only, state `IN_SYNC` (same as a function docstring with no derivable
  blocks today); `reconcile` prints a "no derivable cases" note.
- All journal redaction rules unchanged: derived detail never leaks into
  `JAUNT_LOG` beyond opaque ids and exception class names.

### 6. Out of scope

- Method-level adoption (`module:Cls.method`).
- Any Postgres/docker/compose code in jaunt (`jaunt.testing` does not exist).
- anyio-marked battery emission (asyncio_mode=auto covers the emitted tests;
  revisit only if a real project's conftest is anyio-incompatible).
- Nested classes, metaclass-generated members, `__init_subclass__` dynamics —
  the AST shape must be statically visible, same as whole-class `@magic`.
- Model-called extraction changes: `adopt` stays deterministic-only (no
  `model_extract`), as today.

## Testing

Same discipline as the existing suite — mocked backend, no API keys:

- **Grammar:** parse → IR → each of the three emitters, including sugar-form
  compatibility (existing single-arg contracts produce byte-identical batteries),
  kwargs, constructor recipes, fixture refs, raises-cases, malformed input.
- **Digest compatibility regression:** plain sync function digests byte-identical
  before/after unification (golden values), including stub-shaped bodies
  (`pass`/`...`/`raise NotImplementedError`) that the magic path elides but
  contract digests must not.
- **Async pipeline:** adopt → reconcile → check on a tmp project with an async
  function; sync↔async flip drifts; battery runs under `-o asyncio_mode=auto`.
- **Class pipeline:** adopt a class; per-method derived regions; shape-drift
  matrix (add/remove/re-sign method, edit class vs method docstring, body-only
  edit); qualname sanitization; eject round-trip.
- **Fixture seam:** tmp project with `tests/contract/conftest.py`; reconcile
  routes fixture cases through pytest; strength exclusion reporting; missing
  fixture at check time → `BEHAVIOR_DRIFT`.
- **Dogfood:** adopt one async function inside jaunt's own repo as part of the
  plan's final task.

## Success criteria

- `jaunt adopt` accepts a real async function and a real class from a project
  shaped like mem-mcp-b's pilot slice, derives runnable batteries, and
  `jaunt check` gates them deterministically.
- Existing adopted sync-function contracts show **zero** drift after upgrading.
- A DB-coupled unit can declare `Fixtures: db` and be validated end-to-end with
  a user-authored conftest, with strength exclusions reported explicitly.
- Convert-on-touch eligibility in the daemon spec ("top-level functions, sync or
  async, and classes") becomes literally true.
