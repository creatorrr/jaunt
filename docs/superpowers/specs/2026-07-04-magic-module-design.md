# `jaunt.magic_module` — Module-Level Magic (Design)

Date: 2026-07-04
Status: Approved; codex@high design review folded in (2 P1, 4 P2, 2 P3 — all
addressed below; see §2 two-phase registration, §1 decorator exclusion rule,
§3 capture limitation, §8 lifecycle)
Target: jaunt 1.4 headline feature (alongside skills context budget)
Origin: user proposal + FEEDBACK finding 22 (module-level shared-constraint
channel); seams verified against 1.3.1 source by a dedicated read pass.

## Decisions (user-confirmed)

- **Positioning: new primary style.** Docs and tutorials lead with
  `magic_module` for module-at-a-time work; per-symbol decorators remain fully
  supported as the override/precision mechanism. Mixed modules are first-class.
- **Name:** `jaunt.magic_module`. Exported from `jaunt/__init__.py`.
- **Kwargs: full `@magic` parity** — `deps=`, `prompt=`, `infer_deps=`,
  `test=` as module-level defaults; per-symbol decorators override.
- **Tests story:** `magic_module(__name__, test=True)` applies the existing
  implicit-test opt-in to every governed spec. `@jaunt.test` files unchanged;
  no `test_module` in v1.
- **Runtime forwarding: Approach A** — intercept-once-then-vanish
  (`__class__` swap; rebind on first access; swap back).
- **Spec classification: strict stub forms** via the existing
  `class_analysis.is_stub_body` predicate.

## 1. User-facing semantics

```python
import jaunt

jaunt.magic_module(__name__, prompt="All parsers are RFC 5322 strict.")

EMAIL_RE = re.compile(...)          # handwritten constant — untouched

class Email:
    """Email object: from_, to, subject, body. Validates on construction."""
    # docstring-only body → whole-class spec (jaunt designs the API)

def parse_email(raw: str) -> Email:
    """Parse a raw RFC 5322 payload into Email. Raise ValueError with a
    descriptive message on malformed input."""
    ...

def _render_debug(email: Email) -> str:   # real body → handwritten helper
    return f"<{email.from_} -> {email.to}>"
```

Classification of each **top-level** `def` / `async def` / `class`:

| Shape | Classification |
|---|---|
| Function body passes `is_stub_body` (docstring-only, `...`, `pass`, `raise NotImplementedError`) | **spec** (same semantics as `@jaunt.magic` today) |
| Class with docstring-only body, or with ≥1 stub method | **whole-class spec**; members keep today's three-tier semantics (`@jaunt.preserve` / `@jaunt.sig` / guidepost) |
| Real body (function) / all-real-method class | **handwritten** — reusable context, never regenerated |
| Carries any jaunt decorator (`@magic`, `@preserve`, `@test`, `@contract`) | **skipped by the module scan** — the decorator governs it (decorator wins) |
| Carries any NON-jaunt decorator (`@typing.overload`, `@property`, `@functools.cache`, …) | **never governed** — handwritten. Explicit `@jaunt.magic` is the opt-in for a decorated spec. |

The non-jaunt-decorator exclusion closes the `@typing.overload` trap (ellipsis
overload stubs would otherwise classify as specs) and sidesteps descriptor
semantics wholesale. Jaunt-decorator skip detection is **alias-aware**: the
scan resolves `import jaunt as j` / `from jaunt import magic as m` from the
module's own import statements before matching decorator expressions —
today's name-only recognizers (`digest._is_jaunt_decorator`,
`class_analysis`) miss aliases, and a missed skip would double-register.

Non-def/class statements (constants, `if TYPE_CHECKING:` blocks, imports) are
never scanned. Defs/classes under top-level `if`/`try` branches are neither
governed nor guaranteed to appear as handwritten context (matching today's
context builders, which read direct `tree.body` only) — documented. `@jaunt.preserve`
on a stub-bodied def is the opt-out for an intentionally-empty handwritten
function. Nested defs/classes are out of scope (module scan is top-level
only, mirroring discovery today).

Note the docs convention change this blesses: module-mode stubs use `...` or
`raise NotImplementedError`; the older `raise RuntimeError("spec stub")`
convention does NOT classify as a stub and stays a decorator-mode idiom. Docs
standardize on `...` everywhere.

## 2. Registration (import-time, AST-based)

Constraint (verified): the registry is populated purely as an import side
effect — `import_and_collect` imports and reads; there is no post-import scan.
`magic_module(__name__)` therefore registers synchronously, at module-body
execution time, before the stubs below it exist as runtime objects:

1. Resolve the caller's module + source file (same frame discipline as
   `_resolve_magic_identity_from_callsite`, but expecting a bare call at
   module scope, not a decorator).
2. Parse the on-disk source (via the existing parse cache), scan top-level
   defs/classes, classify per §1.
3. For each spec, construct a `SpecEntry(kind="magic", ...)` and
   `register_magic(entry)` — initially with `entry.obj = None` (the object
   does not exist yet). `extract_source_segment` already locates segments by
   qualname, so digests need no object.
4. Record the module in a new module-magic registry
   (`registry.py`): `{module_name: ModuleMagicDefaults(deps, prompt,
   infer_deps, test)}` — consumed by the runtime hook (§3), `jaunt specs`,
   and diagnostics.

**Phase two — post-import obj backfill (codex P1).** `entry.obj = None` is
NOT tolerable downstream: the builder gates whole-class handling on
`isinstance(entry.obj, type)` and calls `resolve_base_contract(entry.obj)`
(builder.py:685/730/818); `module_contract` skips auto class tests for
non-`type` obj (:187); `tester.py` (:261) and CLI test-API enrichment
(cli.py:3010/3048) read it too. Therefore `import_and_collect` gains a
finalize step: immediately after importing a governed module (and before any
registry consumer runs), backfill each module-magic entry's `obj` from the
now-complete module — reading the ORIGINAL stub objects (the runtime hook
snapshots pre-rebind stubs in case first access already fired), then run the
same post-registration analysis pipeline decorator entries get
(`analyze_magic_decorators` with an empty decorator set → identical
`effective_signature` rendering, empty `decorator_api_records`). After
finalize, module-magic entries are shape-identical to decorator entries with
zero non-jaunt decorators — no `None` ever reaches builder/tester/CLI, and
digest/context inputs (`effective_signature`, `module_context_digest`
contributions) match decorator-style byte-for-byte.

**Digest neutrality, stated precisely (codex P1).** Converting a decorated
spec to module style is digest-neutral **iff the stub body form is
unchanged** — the jaunt decorator line never enters the digest, and the
finalize step makes signature rendering identical. Changing the body form
DOES restale: notably the older `raise RuntimeError("spec stub")` convention
is not a recognized stub form, so its body text is part of the current
digest; rewriting it to `...` during conversion restales that module once.
Called out in upgrading.mdx.

**Duplicate-registration guard.** Because the module scan skips
jaunt-decorated defs (alias-aware, per §1), a decorated symbol registers
exactly once — via its decorator, later in module execution. Defense in
depth (codex P2): `register_magic` additionally warns when an existing entry
for the same spec_ref is overwritten by one with a different origin
(module-scan vs decorator) — last-write-wins stays the behavior, but the
overlap is loud instead of silent. Tests cover the aliased-decorator case
(`from jaunt import magic as m`).

**Kwarg merge.** Rule: module defaults apply to EVERY spec in a governed
module — module-scan-registered and decorator-registered alike — merged
key-by-key into `SpecEntry.decorator_kwargs` at construction, with per-symbol
values winning per key (a decorated `@magic(deps=[...])` symbol still inherits
the module `prompt=`). Merging into `decorator_kwargs` means dependency resolution
(`deps.py`), prompt threading (`builder.py` decorator_prompts), and the
structural digest (`_stable_decorator_kwargs`) all react to module-default
changes with zero downstream modification — editing the module `prompt=`
restales every governed spec, which is correct (it is part of their contract).

For decorator-registered entries the merge happens inside `_decorate` by
consulting the module-magic registry (the `magic_module` call precedes the
defs, so defaults are always registered first; a decorator finding no
module entry behaves exactly as today).

## 3. Runtime forwarding (Approach A: intercept-once-then-vanish)

At call time, `magic_module`:

1. Builds the spec-name set (from §2's scan).
2. Swaps `sys.modules[__name__].__class__` to `_MagicModule`
   (a `types.ModuleType` subclass; direct precedent:
   `contract/__init__.py:27-40`).

`_MagicModule.__getattribute__` fast-paths everything except first access to
a governed spec name. On that first access it **resolves the module**:

- Import the generated counterpart (`spec_module_to_generated_module` +
  `importlib.import_module` — the existing lazy path).
- **Built:** rebind every spec name in the module `__dict__` to its generated
  object; stamp classes with `__jaunt_spec_ref__` (parity with the decorator's
  built path).
- **Not built:** rebind every spec function to a raiser with the decorator
  path's `_not_built_error` (actionable "run jaunt build" message, not a bare
  `NotImplementedError`), and every spec class to the existing
  `__new__`-raiser placeholder.
- Swap `__class__` back to `types.ModuleType`. Steady-state overhead: zero.

Properties:
- **External access** (`mod.parse_email`, `from mod import parse_email`
  after import) → generated code. `from mod import parse_email` at the top of
  ANOTHER module is an attribute access on `mod` → triggers resolution.
- **Sibling calls** (handwritten helper calls `parse_email(...)`) → late
  global binding; by the time any external caller reaches the module, globals
  are rebound. Works.
- **Documented limitation — import-time capture (codex P2, the sharpest
  edge):** module-level code below the defs that CALLS, INSTANTIATES, or
  SUBCLASSES a governed spec sees the pre-rebind stub — and unlike decorator
  mode's not-built placeholder, a stub class is silently constructible (its
  `__init__` is a docstring). Rebinding cannot fix already-captured
  references (dataclass field defaults, base classes, registries, closures);
  instances created from a pre-rebind stub class also break pickle
  round-trips (the module attribute resolves to the generated class).
  Three-part mitigation: (1) the §2 scan emits a deterministic **build-time
  warning** when it sees a top-level class whose base names a governed spec,
  or a top-level expression/assignment calling one; (2) the docs state the
  rule — module-level consumption of specs belongs in functions; (3) the
  escape hatch is explicit `@jaunt.magic` on that symbol (decorator mode
  substitutes the generated class at decoration time). Circular imports that
  reach into a half-executed governed module bypass resolution the same way
  they bypass everything else in Python — same rule applies.
- **Build-freshness parity with decorators:** both modes resolve the
  generated module once per process (`sys.modules` caching); a rebuild during
  a live process requires re-import in both. No regression.
- Pickling/`inspect` parity: after resolution, module attributes ARE the
  generated objects (better than decorator mode's wrappers for functions).

## 4. Discovery, config, and tooling integration

- **AST prescreen** (`discovery._has_jaunt_markers`): already passes on the
  `import jaunt` branch; additionally recognize a top-level
  `ast.Expr(Call(...))` whose func name/attr is `magic_module` for
  belt-and-braces (and add it to the marker name set).
- **`module_contract`:** governed spec names flow into
  `expected_names`/`generated` via the registry exactly as decorator specs do
  — handwritten classification stays name-driven and correct.
- **`jaunt specs`:** module-magic entries display with an origin marker
  (`module` vs `decorator`) and the effective merged kwargs; `--json` gains
  `"origin": "magic_module"`.
- **Validation, .pyi emission, semantic gate, `check`/`status`, daemon,
  guard:** unchanged — all operate on registry entries, digests, and
  generated artifacts, which are shape-identical. (The .pyi emitter already
  handles undecorated stubs — module mode makes its output cleaner, no
  wrapper types at all.)
- **`jaunt init`:** scaffold switches to magic_module style (INIT_TEMPLATE +
  starter spec). FULL_SCHEMA_TEMPLATE unchanged (no new config keys — the
  feature is code-level, not config-level).
- **Docs (same release):** writing-magic-specs guide leads with module style;
  quickstart converts; primer.md + CLAUDE.md vocabulary; upgrading.mdx notes
  (converting decorator→module style does NOT restale — the decorator line
  never entered the digest; adding module-level kwargs DOES restale, as any
  contract change should).

## 5. Error handling

- `magic_module(__name__)` called anywhere but module top-level scope →
  `JauntError` with the fix.
- Called twice in one module → `JauntError` (one governing call per module).
- Called with a name not in `sys.modules` (e.g. passed a wrong string) →
  `JauntError` naming what was received.
- A governed module with ZERO classified specs → warning (probably a
  mis-placed call or all-real bodies), not an error — legal during gradual
  conversion.
- `@jaunt.sig` on a top-level function in a governed module → same error as
  today outside whole-class specs (function signatures are already part of
  the contract; sealing is a method-tier concept).

## 6. Testing strategy

- Unit: classification table (§1) over a fixture module — every row asserted,
  including the `raise RuntimeError` non-stub, `@preserve` opt-out, mixed
  class, decorated-symbol skip (aliased and plain), `@typing.overload` /
  `@property` exclusion, conditional-def non-governance.
- Registration: registry state after importing a governed module — entries,
  merged kwargs, obj backfill (real stub objects post-finalize; whole-class
  entries pass the builder's `isinstance(obj, type)` gates), duplicate-origin
  warning, capture warning (top-level subclass/call of a governed spec).
- Runtime: built path (external access, `from`-import, sibling call,
  class instantiation + isinstance), not-built path (actionable error), swap-
  back verified (`type(mod) is types.ModuleType` after first access).
- Digest: decorator→module-style conversion with unchanged `...` bodies is
  digest-neutral (no restale) INCLUDING the `effective_signature` path;
  `raise RuntimeError`-body conversion restales (asserted, since it's the
  documented behavior); module `prompt=` edit restales all governed specs;
  per-symbol override wins.
- Build e2e (mocked backend): governed module builds, validates,
  emits .pyi, `check` green; mixed module builds.
- Prescreen: call-form detection without an importable jaunt alias.

## 7. Lifecycle & tooling interplay (codex P3)

- **`eject`:** leaves real committed code (not a stub), so an ejected
  function in a governed module classifies handwritten — no accidental
  re-governance. **`adopt`/`@contract`:** contract-decorated defs carry a
  jaunt decorator → skipped by the scan. Removing a `@contract` marker
  by hand from a still-stub-shaped function in a governed module WOULD flip
  it to module-magic — the §2 duplicate/origin machinery can't catch a
  removal, so `jaunt eject` docs note it (eject, don't hand-strip).
- **watch/daemon:** unchanged through their existing rediscovery paths
  (watcher re-runs discovery per change batch; daemon per HEAD) — governed
  modules re-scan on every cycle like decorator modules re-import.
- **`.pyi` interplay:** the stub emitter already writes the generated API's
  signatures next to the spec module; in module mode the runtime rebinds the
  same names to the same generated objects, so type-checker view and runtime
  view agree post-rebind. No emitter change needed.

## 8. Out of scope (recorded, not designed)

- `test_module()` counterpart; nested-def governance; `magic_module` in
  `__init__.py` governing a package; per-symbol kwarg *subtraction*
  (`prompt=None` to opt out of a module default); sealed top-level functions;
  governance of defs under conditional branches.
