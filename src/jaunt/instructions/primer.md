# Jaunt — agent primer

Jaunt is a spec-driven code generation framework for Python, with an alpha
TypeScript target. You write **intent** as typed stubs and contract prose. Jaunt
generates the **implementation** with the OpenAI Codex CLI (`codex exec`) and
writes it under `__generated__/`.

## Your role (read this first)

You author and refine *specs*. You do **not** write the implementations.

1. **Never hand-write the body of a spec.** Its body stays a stub: `...`, a bare
   docstring, `pass`, or `raise NotImplementedError`. The docstring is the
   contract; Jaunt fills in the code.
2. **Never edit files under `__generated__/`.** They are overwritten on every
   build. Fix the spec and rebuild instead.
3. **Pair every implementation spec with test intent.** A magic spec without
   `@jaunt.test` coverage is unfinished work.

## Mental model

- A **spec** is a stub describing *what* to build — a top-level stub in a
  `jaunt.magic_module` file, or a `@jaunt.magic`-decorated symbol. The full,
  cleaned docstring is the behavioral contract; later lines matter as much as the
  first.
- `jaunt build` generates implementations into `__generated__/`; importing the
  symbol transparently resolves to the generated code.
- Builds are **incremental**: Jaunt hashes each spec's normalized contract and its
  transitive dependencies. Cosmetic edits (formatting, comments) do not trigger a
  rebuild; signature, docstring-contract, and dependency-API changes do.

## The two modes

Both coexist and are selected per symbol by decorator.

- **Magic mode** — `jaunt.magic_module` / `@jaunt.magic` / `@jaunt.test`. The
  docstring is canonical and Jaunt generates the implementation (and tests) under
  `__generated__/`. Use this when you want Jaunt to write the code.
- **Contract mode** — `@jaunt.contract`. The *committed code* is canonical; the
  docstring is a contract; Jaunt derives a committed pytest battery under
  `tests/contract/` (it does not generate the implementation). Use this to pin
  behavior of code you want to keep hand-written. It covers top-level functions
  (sync or async) and whole classes, and derived cases may declare pytest
  fixtures resolved from `tests/contract/conftest.py`.

## The build/test loop

1. Write `@jaunt.magic` specs (and `@jaunt.test` specs).
2. `jaunt build` → generate implementations.
3. `jaunt test` → generate tests and run pytest.
4. Review the generated code in `__generated__/`. If it is wrong, **refine the
   spec docstring** (or add a `prompt=` hint) and rebuild — do not patch the
   output.
5. `jaunt status` shows what is stale and needs rebuilding.

## TypeScript target

A version-2 `[target.ts]` project uses private `*.jaunt.ts[x]` inputs. Discovery
is static: the project-local worker parses source and `tsconfig.json` without
executing the spec or application code.

```ts
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Convert a title to a stable URL slug. */
export function slugify(title: string): string {
  return jaunt.magic();
}
```

Run `jaunt sync` before the first build. It creates a deterministic API mirror,
an ordinary public facade when one is absent, and a typed throwing placeholder.
This makes editor types available without a model call, but the module remains
unbuilt until `jaunt build --language ts` validates and commits a real
implementation.

TypeScript rules:

- Production code imports the public facade, never `*.jaunt.ts[x]` or a generated
  private path.
- Optional executable context lives in the paired `*.context.ts[x]` file and is a
  one-way leaf: it cannot value-import its own facade, implementation, or spec.
- Never edit generated implementations, `.api.ts` mirrors, `.jaunt.json`
  sidecars, or generated Vitest batteries.
- TSDoc is the behavioral contract. `@jauntPreserve` marks the rare real class
  member copied into generated output. TypeScript has no `@jaunt.sig`; declared
  signatures are always conformance-checked.
- TypeScript targets use `ts:path/to/spec#symbol` IDs. `--language py|ts`
  narrows a mixed version-2 workspace.

## Writing a good magic spec

The primary style is module-level. Call `jaunt.magic_module(__name__)` once at the
top of a file, then write stubs — every top-level stub below becomes a spec:

```python
import jaunt

jaunt.magic_module(__name__)


def slugify(title: str) -> str:
    """
    Convert a title to a URL-safe slug.

    Rules:
    - Lowercase the input.
    - Replace whitespace runs with a single "-".
    - Remove characters that are not ASCII alphanumerics, "-", or "_".
    - Raise ValueError if the result is empty.
    """
    ...
```

The scan governs only top-level stubs (`...`, a bare docstring, `pass`, or
`raise NotImplementedError`). A function with a real body, or one carrying a
non-jaunt decorator like `@property`, stays handwritten: the model reads it as
context but never regenerates it. `magic_module` takes the same kwargs as
`@jaunt.magic` (`deps=`, `prompt=`, `infer_deps=`, `test=`) as module-wide
defaults. Keep module-level code that calls, instantiates, or subclasses a
governed spec inside a function — at module level it would see the pre-rebind
stub.

Drop to a decorator when you want per-symbol control. `@jaunt.magic(deps=[...],
prompt="...")` overrides the module defaults for one symbol, and decorating a
symbol is how you opt it in against the scan. `@jaunt.magic` also works on
individual class methods and whole classes (see the method tiers below).

Principles:

- Be explicit about behavior: inputs, outputs, invariants, what "correct" means.
- Name the failure modes: which exception, under which condition.
- Cover edge cases: empty input, `None`, boundaries, duplicates.
- Use full type annotations on every parameter and the return.
- Prefer pure logic; push I/O behind parameters (dependency injection).
- Declare dependencies with `deps=[other_spec]` when one spec uses another.
- For a whole-class `@jaunt.magic`, the declared members and method signatures are
  part of the exported contract — changing them can invalidate dependents.

## Whole-class `@jaunt.magic` method tiers

Inside a class-level `@jaunt.magic`, each method is in exactly one of three tiers:

- **Preserved** (`@jaunt.preserve`): hand-written; kept verbatim.
- **Sealed** (`@jaunt.sig` on a stub; inner `@jaunt.magic` is a supported alias):
  Jaunt writes the body, but the declared signature is locked — do not change
  params, defaults, annotations, or the return type. Signature drift is a hard
  build error.
- **Guidepost** (an unmarked stub): a sketch of intent; the model may adapt the
  signature or split it into several methods as long as the docstring behavior is
  delivered.

```python
@jaunt.magic()
class Cache:
    """A tiny key/value cache. Design the internals."""

    @jaunt.sig
    def get(self, key: str) -> str | None: ...  # sealed: this signature is locked
```

`@jaunt.sig` here takes no arguments and cannot sit under `@property` (v1). A
spec'd base class in the class header is an always-on dependency (not gated by
`infer_deps`), and a cross-module base's generated public API feeds the subclass's
freshness.

## Writing a good `@jaunt.test` spec

```python
@jaunt.test()
def test_slugify_basic() -> None:
    """Assert slugify("Hello World") == "hello-world" and "  A  B  " -> "a-b"."""
    ...

@jaunt.test()
def test_slugify_rejects_empty() -> None:
    """slugify("!!!") raises ValueError (nothing remains after filtering)."""
    ...
```

Keep tests deterministic (no network/clock unless injected), small, and focused on
the public contract. Include negative/error-path cases. Names must start with
`test_`.

## Commands

{{COMMAND_TABLE}}

For exact flags on any command, run `jaunt <cmd> --help`. Common flags:
`--root`, `--config`, `--json`, `--force`, `--target MODULE`,
`--language {py,ts}`, `--no-infer-deps`.
Progress: `--progress {auto,rich,plain,none}` (`auto` = rich on TTY, plain lines
off-TTY; explicit `plain` works with `--json`).

Agent loop (default propose-only daemon): `git commit … && jaunt jobs wait
--timeout 1800 && jaunt jobs land --all`. The daemon parks each green job as a
reviewable proposal (`[daemon] auto_commit = false`, the default); `jaunt jobs
wait` treats a proposal as terminal-green, and `jaunt jobs land --all` lands the
fresh proposals as provenance commits (`jaunt jobs discard <id>` drops one). With
`[daemon] auto_commit = true` the daemon commits green jobs itself, so the loop is
just `git commit … && jaunt jobs wait --timeout 1800`.

## Exit codes

{{EXIT_CODES}}

## Incremental builds & freshness

- `jaunt status` lists stale vs fresh modules. A dependency's API change (signature,
  whole-class members, contract docstring) makes its dependents stale too.
- `jaunt build --force` ignores the incremental cache and regenerates everything.
- `jaunt build --target my_pkg.mymod` rebuilds one module (and its dependents).

## Anti-patterns to avoid

- Editing `__generated__/` by hand (it will be overwritten).
- Writing real logic inside a spec body (the body must stay a stub, or it is read
  as a handwritten function and never regenerated).
- Vague docstrings ("does X") with no semantics, edge cases, or error behavior.
- Shipping a `@jaunt.magic` spec with no `@jaunt.test` coverage.
- Over-constraining the implementation with details the contract does not require.
