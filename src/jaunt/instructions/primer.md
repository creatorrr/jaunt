# Jaunt — agent primer

Jaunt is a spec-driven code generation framework for Python. You write
**intent** as decorator-marked stubs; Jaunt generates the **implementation** with
the OpenAI Codex CLI (`codex exec`) and writes it under `__generated__/`.

## Your role (read this first)

You author and refine *specs*. You do **not** write the implementations.

1. **Never hand-write the body of a `@jaunt.magic` symbol.** Its body stays a
   stub (`raise RuntimeError("spec stub ...")`). The docstring is the contract;
   Jaunt fills in the code.
2. **Never edit files under `__generated__/`.** They are overwritten on every
   build. Fix the spec and rebuild instead.
3. **Pair every implementation spec with test intent.** A `@jaunt.magic` symbol
   without `@jaunt.test` coverage is unfinished work.

## Mental model

- A **spec** is a decorated stub describing *what* to build. The full, cleaned
  docstring is the behavioral contract — later lines matter as much as the first.
- `jaunt build` generates implementations into `__generated__/`; importing the
  symbol transparently resolves to the generated code.
- Builds are **incremental**: Jaunt hashes each spec's normalized contract and its
  transitive dependencies. Cosmetic edits (formatting, comments) do not trigger a
  rebuild; signature, docstring-contract, and dependency-API changes do.

## The two modes

Both coexist and are selected per symbol by decorator.

- **Magic mode** — `@jaunt.magic` / `@jaunt.test`. The docstring is canonical and
  Jaunt generates the implementation (and tests) under `__generated__/`. Use this
  when you want Jaunt to write the code.
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

## Writing a good `@jaunt.magic` spec

```python
import jaunt

@jaunt.magic()
def slugify(title: str) -> str:
    """
    Convert a title to a URL-safe slug.

    Rules:
    - Lowercase the input.
    - Replace whitespace runs with a single "-".
    - Remove characters that are not ASCII alphanumerics, "-", or "_".
    - Raise ValueError if the result is empty.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

Principles:

- Be explicit about behavior: inputs, outputs, invariants, what "correct" means.
- Name the failure modes: which exception, under which condition.
- Cover edge cases: empty input, `None`, boundaries, duplicates.
- Use full type annotations on every parameter and the return.
- Prefer pure logic; push I/O behind parameters (dependency injection).
- Declare dependencies with `deps=[other_spec]` when one spec uses another.
- For a whole-class `@jaunt.magic`, the declared members and method signatures are
  part of the exported contract — changing them can invalidate dependents.

## Writing a good `@jaunt.test` spec

```python
@jaunt.test()
def test_slugify_basic() -> None:
    """Assert slugify("Hello World") == "hello-world" and "  A  B  " -> "a-b"."""
    raise AssertionError("spec stub (generated at test time)")

@jaunt.test()
def test_slugify_rejects_empty() -> None:
    """slugify("!!!") raises ValueError (nothing remains after filtering)."""
    raise AssertionError("spec stub (generated at test time)")
```

Keep tests deterministic (no network/clock unless injected), small, and focused on
the public contract. Include negative/error-path cases. Names must start with
`test_`.

## Commands

{{COMMAND_TABLE}}

For exact flags on any command, run `jaunt <cmd> --help`. Common flags:
`--root`, `--config`, `--json`, `--force`, `--target MODULE`, `--no-infer-deps`.

## Exit codes

{{EXIT_CODES}}

## Incremental builds & freshness

- `jaunt status` lists stale vs fresh modules. A dependency's API change (signature,
  whole-class members, contract docstring) makes its dependents stale too.
- `jaunt build --force` ignores the incremental cache and regenerates everything.
- `jaunt build --target my_pkg.mymod` rebuilds one module (and its dependents).

## Anti-patterns to avoid

- Editing `__generated__/` by hand (it will be overwritten).
- Writing real logic inside a `@jaunt.magic` body (the body must stay a stub).
- Vague docstrings ("does X") with no semantics, edge cases, or error behavior.
- Shipping a `@jaunt.magic` spec with no `@jaunt.test` coverage.
- Over-constraining the implementation with details the contract does not require.
