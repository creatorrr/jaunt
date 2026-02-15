# Jaunt (MVP)

Note: `DOCS.md` is deprecated. The rendered docs site under `docs-site/` is the canonical documentation going forward.

Jaunt is a small Python library + CLI for **spec-driven code generation**:

- You write **intent** as normal Python functions/classes decorated with `@jaunt.magic(...)`.
- You optionally write **test intent** as Python stubs decorated with `@jaunt.test(...)`.
- Jaunt uses an LLM backend (OpenAI or Anthropic) to generate real modules under `__generated__/`.
- You review the generated output and iterate by editing the specs and rerunning generation.

This repo contains the Jaunt implementation (package: `src/jaunt/`), plus a test suite that documents the intended behavior.

## Repo Tour

- `src/jaunt/`: library + CLI (`jaunt.cli`) and LLM backends (`jaunt.generate.openai_backend`, `jaunt.generate.anthropic_backend`).
- `src/jaunt/prompts/`: default prompt templates packaged into the distribution.
- `src/jaunt/skill/`: assistant-facing docs (a Jaunt “skill” + Cursor rules) packaged as resources.
- `tests/`: unit/integration tests that define the MVP behavior.
- `TASK-*.md` and `jaunt_tdd_plan.md`: architecture + implementation notes used to build the MVP.
- `examples/`: runnable example projects demonstrating Jaunt features.

## Quickstart (Using This Repo)

```bash
uv sync
export OPENAI_API_KEY=...   # or configure llm.api_key_env in jaunt.toml
uv run jaunt --version
```

Jaunt requires a `jaunt.toml` in the project you want to generate into (see below).

## Minimal Consumer Project Layout

Jaunt expects a project root with `jaunt.toml`, and source/tests roots you configure.

Example:

```text
myproj/
  jaunt.toml
  src/
    my_app/
      __init__.py
      specs.py              # contains @jaunt.magic specs (stubs)
  tests/
    __init__.py
    specs_email.py          # contains @jaunt.test specs (stubs)
```

## `jaunt.toml` (Config)

Jaunt discovers the project root by walking upward from the cwd until it finds `jaunt.toml`.

Minimal config:

```toml
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"
```

Full config (all keys optional except `version`):

```toml
version = 1

[paths]
# Directories to scan for specs (relative to project root).
source_roots = ["src", "."]
test_roots = ["tests"]

# Directory name inserted into import paths and used on disk.
# Must be a valid Python identifier. Recommended: "__generated__".
generated_dir = "__generated__"

[llm]
provider = "openai"         # or "anthropic"
model = "gpt-5.2"           # default used by this repo's config loader
api_key_env = "OPENAI_API_KEY"

[build]
jobs = 8
infer_deps = true           # best-effort dependency inference (see "Dependencies")

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]

[prompts]
# If set, these are treated as *file paths* and read at runtime by the OpenAI backend.
# Leave empty to use the packaged defaults in src/jaunt/prompts/.
build_system = ""
build_module = ""
test_system = ""
test_module = ""
```

Notes:

- `paths.source_roots`: the CLI picks the *first existing* source root as the output base for generated build modules.
- `paths.generated_dir`: see "Limitations" about using a non-default value.

## Writing `@jaunt.magic` Specs (Implementation Stubs)

Specs must be **top-level** functions or classes. The body is not used at runtime (it will be replaced/forwarded to generated code), but the *source segment + docstring + type hints* are used as the generation contract and are hashed for incremental rebuilds.

Example `src/my_app/specs.py`:

```python
from __future__ import annotations

import jaunt


@jaunt.magic()
def normalize_email(raw: str) -> str:
    """
    Normalize an email address for stable comparisons.

    Rules:
    - Strip surrounding whitespace.
    - Lowercase the whole string.
    - Must contain exactly one "@".

    Errors:
    - Raise ValueError if the input is not a valid email by these rules.
    """
    raise RuntimeError("spec stub (generated at build time)")


@jaunt.magic(deps=normalize_email)
def is_corporate_email(raw: str) -> bool:
    """
    Return True iff the normalized email domain is "example.com".

    - Uses normalize_email(raw) for parsing/validation.
    - If normalize_email raises ValueError, propagate it unchanged.
    """
    raise RuntimeError("spec stub (generated at build time)")
```

What happens at runtime:

- Calling a `@jaunt.magic` function (or instantiating a `@jaunt.magic` class) forwards to the generated implementation in the corresponding `__generated__` module.
- If you call it before generating, it raises `jaunt.JauntNotBuiltError` with a hint to run `jaunt build`.

Decorator knobs:

- `deps=...`: explicit dependencies (see "Dependencies").
- `prompt="..."`: extra text appended into the spec block for that symbol only.
- `infer_deps=True|False`: per-spec override of dependency inference.

## Writing `@jaunt.test` Specs (Test Stubs)

Test specs are *not real tests*. They are stubs that describe what the generated tests should do.

Rules:

- Must be **top-level** functions.
- Name them like pytest tests (`test_*`) so the *generated* versions are collected by pytest.
- `@jaunt.test` sets `__test__ = False` on the stub so pytest will not collect the stub itself.

Example `tests/specs_email.py`:

```python
from __future__ import annotations

import jaunt

from my_app import specs


@jaunt.test()
def test_normalize_email__lowercases_and_strips() -> None:
    """
    Assert normalize_email:
    - strips surrounding whitespace
    - lowercases
    - raises ValueError for invalid inputs like "no-at-sign"

    Examples:
    - specs.normalize_email("  A@B.COM  ") == "a@b.com"
    """
    raise AssertionError("spec stub (generated at test time)")
```

Jaunt will generate a module under `tests/__generated__/...` with top-level test functions matching your stub names.

## CLI

Entry point: `jaunt = "jaunt.cli:main"` (see `pyproject.toml`).

Build implementations for `@jaunt.magic` specs:

```bash
uv run jaunt build
uv run jaunt build --force
uv run jaunt build --jobs 16
uv run jaunt build --target my_app.specs
uv run jaunt build --no-infer-deps
```

Generate tests for `@jaunt.test` specs and run pytest:

```bash
uv run jaunt test
uv run jaunt test --no-build
uv run jaunt test --no-run
uv run jaunt test --pytest-args=-k --pytest-args email
```

Flags:

- `--root /path/to/project`: override root (otherwise Jaunt searches upward for `jaunt.toml`).
- `--config /path/to/jaunt.toml`: override config path.
- `--target MODULE[:QUALNAME]`: restrict work to one or more modules (currently module-level; the `:QUALNAME` portion is ignored for filtering).
- `--no-infer-deps`: disables best-effort dependency inference (explicit `deps=` still applies).

Exit codes:

- `0`: success
- `2`: config/discovery/dependency-cycle errors
- `3`: generation errors (LLM/backend/validation/import)
- `4`: pytest failure (only when `jaunt test` actually runs pytest)

Important behavior:

- `jaunt test` runs pytest only on the generated test files it just wrote (not the entire suite). Run `pytest` separately for a full test run.

## Where Output Goes

For a spec module `my_app.specs` (under `src/`), Jaunt writes:

- Generated implementation: `src/my_app/__generated__/specs.py`
- Generated import path: `my_app.__generated__.specs`

For a test spec module `tests.specs_email` (under project root), Jaunt writes:

- Generated tests: `tests/__generated__/specs_email.py`
- Generated import path: `tests.__generated__.specs_email`

Generated files include a header like:

```text
# This file was generated by jaunt. DO NOT EDIT.
# jaunt:kind=build|test
# jaunt:source_module=...
# jaunt:module_digest=sha256:...
```

Jaunt uses the header digest to decide whether a module is stale and needs regeneration.

## Dependencies (Ordering + Incremental Rebuild)

Jaunt builds a spec-level dependency graph that is used for:

- build/test generation order (dependencies before dependents)
- incremental rebuilds (a dependent becomes stale if a dependency changes)

You can declare deps explicitly:

```python
@jaunt.magic(deps=["my_app.specs:normalize_email", "my_app.other.Helper"])
def is_corporate_email(raw: str) -> bool: ...
```

Accepted `deps=` formats:

- string `"pkg.mod:Qualname"` (canonical)
- string `"pkg.mod.Qualname"` (dot shorthand; last `.` becomes `:`)
- an object (function/class) that Jaunt can convert to `module:qualname`

Inference (best-effort) can also add edges, controlled by:

- config: `[build].infer_deps` / `[test].infer_deps`
- CLI: `--no-infer-deps`
- per-spec: `infer_deps=...` on the decorator

If a cycle exists, Jaunt raises `JauntDependencyCycleError` and exits with code `2`.

## Backend: OpenAI

The default backend is `jaunt.generate.openai_backend.OpenAIBackend`:

- reads the API key from `os.environ[llm.api_key_env]`
- uses the OpenAI Python SDK (`openai.AsyncOpenAI`)
- calls `chat.completions.create(model=..., messages=[...])`
- strips a single top-level markdown fence (```...```) if present
- retries once if the output fails basic validation (syntax + required top-level names)

Prompt templates live in `src/jaunt/prompts/` and are packaged with the wheel/sdist.

## Limitations / Gotchas (Current Code)

- Generated dir name: runtime forwarding for `@jaunt.magic` uses the `JAUNT_GENERATED_DIR` environment variable (set automatically by the CLI). If you call the runtime outside the CLI, set this env var to match your `paths.generated_dir` config.
- Prompt overrides: `prompts.*` are treated as file paths by the backends. If you set them, ensure those files exist and contain the prompt text.

## Developing Jaunt (This Repo)

```bash
uv sync
uv run ruff check .
uv run ty check
uv run pytest
```
