# Contract Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a second authoring mode to Jaunt where committed code is the source of truth, the docstring is the contract, and Jaunt maintains a derived, committed pytest battery instead of generating the implementation.

**Architecture:** A new `@jaunt.contract` decorator is a runtime no-op that only registers a `kind="contract"` SpecEntry. A new `src/jaunt/contract/` package holds the deterministic core (digests, battery file format, drift state machine, mutation strength score) plus the model-backed derivation. New CLI commands (`check`, `reconcile`, `adopt`, `eject`) drive it; `status` is extended. `check` is fully deterministic and needs no API key — only `reconcile` calls the model. Magic mode is untouched and coexists.

**Tech Stack:** Python 3.12+, stdlib `ast`/`hashlib`, pytest (subprocess), the existing `GeneratorBackend` LLM abstraction, argparse CLI.

**Source spec:** `docs/superpowers/specs/2026-06-23-contract-mode-design.md`. Open questions resolved with the spec's recommended defaults: names kept as-is; homegrown AST mutator; v1 = top-level sync functions only; `battery_dir = "tests/contract"` (project-root-relative); `reconcile` surfaces failures and stops (no `--regen-body`); lifecycle plain→adopt→tracked→eject.

## Global Constraints

- Python 3.12+ syntax; `from __future__ import annotations` at the top of every new module (matches existing files).
- Ruff: line-length 100, rules E/F/I/UP/B. Run `uv run ruff check --fix .` and `uv run ruff format .` before every commit.
- Type check: `uv run ty check` must pass.
- Full unit suite must stay green: `uv run pytest`.
- Never break Magic mode: `@jaunt.magic`/`@jaunt.test`, `build`, `test`, and existing `status` output keep working unchanged.
- `check` must run with **no API key set** and **no network**. Only `reconcile` (and `adopt`, which calls reconcile) may construct an LLM backend.
- v1 scope: **top-level sync functions only**. Class/method/async contracts are out of scope and must raise a clear "not supported in v1" error if encountered, never silently mis-handle.
- Battery files are root-relative under `[contract] battery_dir` (default `tests/contract`), one file per contract function, named `test_<func>.py` under the spec module's path; collision-free and pytest-collectable.
- Exit codes (reuse `cli.py`): `0` ok, `2` config/discovery, `3` generation, `4` battery/pytest failure (used by `check`).

---

## File Structure

**New package `src/jaunt/contract/`:**
- `__init__.py` — package marker, re-exports the public helpers.
- `digests.py` — *(moved into `digest.py` per spec; see Task 3)* — not a separate file.
- `battery.py` — battery file render/parse/merge (the artifact I/O). Depends on `header.py`.
- `derive.py` — prose → structured `ContractBlocks`; deterministic extractor + model fallback; renders battery test bodies; evaluates blocks against a live function.
- `drift.py` — the deterministic six-state drift machine (pure).
- `strength.py` — scoped AST mutator + in-process mutation scoring.

**New prompt templates `src/jaunt/prompts/`:**
- `contract_derive_system.md`, `contract_derive_user.md` — derivation prompt (model fallback for unstructured prose / raises-input inference).

**Modified library files:**
- `runtime.py` — add `contract` decorator (no-op marker + registration).
- `__init__.py` — export `contract`.
- `registry.py` — `kind="contract"` in the Literal; `register_contract`/`get_contract_registry`; extend `clear_registries` and `get_specs_by_module`.
- `header.py` — contract-battery header marker + `format_contract_battery_header`/`parse_contract_battery_header`.
- `digest.py` — `contract_digests(source_file, qualname)` → prose/signature/body digests + `load_function_node`.
- `config.py` — `ContractConfig` + `[contract]` parsing + `JauntConfig.contract` field.
- `generate/base.py` + `generate/openai_backend.py` + `generate/anthropic_backend.py` + `generate/cerebras_backend.py` — `complete_text(*, system, user)` for the derivation model call.
- `cli.py` — new `check`/`reconcile`/`adopt`/`eject` parsers + dispatch + command bodies; extend `cmd_status`.

**New example + docs:**
- `examples/contract_slugify/` — runnable contract-mode example (strong contract + a deliberately weak one).
- `CLAUDE.md`, `README.md`, `.claude/skills/jaunt/SKILL.md` — document Contract mode.

**Test files (new):**
- `tests/test_contract_decorator.py`, `tests/test_contract_digests.py`, `tests/test_contract_header.py`, `tests/test_contract_battery.py`, `tests/test_contract_derive.py`, `tests/test_contract_drift.py`, `tests/test_cli_check.py`, `tests/test_contract_strength.py`, `tests/test_cli_reconcile.py`, `tests/test_cli_adopt.py`, `tests/test_cli_eject.py`, `tests/test_cli_status_contract.py`, `tests/test_contract_config.py`.

---

## Milestone 1 — Deterministic core (no model)

Produces working software: a hand-written or structured-prose battery can be gated by `jaunt check` with no API key.

### Task 1: `@jaunt.contract` decorator + registry

**Files:**
- Modify: `src/jaunt/registry.py:27` (kind Literal), add registry functions
- Modify: `src/jaunt/runtime.py` (add `contract` after `test`, ~line 421)
- Modify: `src/jaunt/__init__.py:13,26`
- Test: `tests/test_contract_decorator.py`

**Interfaces:**
- Consumes: `SpecEntry` (registry.py), `spec_ref_from_object`, `_classify_qualname`, `_source_file` (runtime.py).
- Produces:
  - `registry.register_contract(entry: SpecEntry) -> None`
  - `registry.get_contract_registry() -> dict[SpecRef, SpecEntry]`
  - `jaunt.contract` — decorator usable as bare `@jaunt.contract` and called `@jaunt.contract()`; returns the function unchanged; registers `kind="contract"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_decorator.py
from __future__ import annotations

import jaunt
from jaunt import registry


def teardown_function() -> None:
    registry.clear_registries()


def test_contract_is_noop_identity_bare() -> None:
    registry.clear_registries()

    @jaunt.contract
    def slugify(title: str) -> str:
        """Lowercase. Raises ValueError if empty."""
        return title.strip().lower()

    # The decorated object is the original function and runs its own body.
    assert slugify("  HI ") == "hi"
    assert slugify.__name__ == "slugify"


def test_contract_called_form_registers_kind_contract() -> None:
    registry.clear_registries()

    @jaunt.contract()
    def normalize(x: str) -> str:
        """Strip."""
        return x.strip()

    entries = list(registry.get_contract_registry().values())
    assert len(entries) == 1
    assert entries[0].kind == "contract"
    assert entries[0].qualname == "normalize"
    assert registry.get_contract_registry() is not registry.get_magic_registry()


def test_contract_does_not_raise_not_built() -> None:
    registry.clear_registries()

    @jaunt.contract
    def f() -> int:
        """Return 1."""
        return 1

    # No __generated__ import, no JauntNotBuiltError path.
    assert f() == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_decorator.py -q`
Expected: FAIL — `AttributeError: module 'jaunt' has no attribute 'contract'`.

- [ ] **Step 3: Extend the registry**

In `src/jaunt/registry.py`, change the `SpecEntry.kind` Literal (line 27) and add registry functions:

```python
# line 27: was: kind: Literal["magic", "test"]
    kind: Literal["magic", "test", "contract"]
```

```python
# add after _TEST_REGISTRY (line 43)
_CONTRACT_REGISTRY: dict[SpecRef, SpecEntry] = {}


def register_contract(entry: SpecEntry) -> None:
    """Register a contract spec entry (last write wins)."""

    _CONTRACT_REGISTRY[entry.spec_ref] = entry


def get_contract_registry() -> dict[SpecRef, SpecEntry]:
    """Return the global contract registry (treat as read-only)."""

    return _CONTRACT_REGISTRY
```

Update `clear_registries` (line 70) to also clear contract:

```python
def clear_registries() -> None:
    """Clear all global registries (intended for tests)."""

    _MAGIC_REGISTRY.clear()
    _TEST_REGISTRY.clear()
    _CONTRACT_REGISTRY.clear()
```

Update `get_specs_by_module` signature/body (line 77) to accept `"contract"`:

```python
def get_specs_by_module(
    kind: Literal["magic", "test", "contract"],
) -> dict[str, list[SpecEntry]]:
    """Group specs by entry.module with stable ordering within each module."""

    if kind == "magic":
        entries = _MAGIC_REGISTRY.values()
    elif kind == "test":
        entries = _TEST_REGISTRY.values()
    elif kind == "contract":
        entries = _CONTRACT_REGISTRY.values()
    else:  # pragma: no cover
        raise ValueError(f"unknown kind: {kind!r}")
    ...
```

- [ ] **Step 4: Add the `contract` decorator**

In `src/jaunt/runtime.py`, add the import and the decorator. Update the existing import line 22:

```python
from jaunt.registry import (
    SpecEntry,
    register_contract,
    register_magic,
    register_test,
)
```

Add at the end of the module (after `test`):

```python
def contract(obj: F | None = None, *, deps: object | None = None) -> F | Callable[[F], F]:
    """Mark a fully-implemented function as contract-tracked.

    Runtime no-op: returns the function unchanged (like a type annotation). At
    import time it registers a ``kind="contract"`` SpecEntry so discovery and the
    contract commands (`reconcile`/`check`/`adopt`/`eject`) can find it. There is
    NO import-time substitution and NO ``__generated__`` import — the committed
    body is the thing that runs. Accepts ``@jaunt.contract`` and
    ``@jaunt.contract()``.
    """

    def _decorate(fn: F) -> F:
        if isinstance(fn, (classmethod, staticmethod)):
            raise JauntError("@contract must decorate a plain function (v1: top-level sync only).")
        if isinstance(fn, type):
            raise JauntError("@contract does not support classes in v1 (top-level sync functions).")
        if inspect.iscoroutinefunction(fn):
            raise JauntError("@contract does not support async functions in v1.")

        class_name = _classify_qualname(fn)  # rejects closures/deep nesting
        if class_name is not None:
            raise JauntError("@contract does not support methods in v1 (top-level functions only).")

        f = cast(Any, fn)
        spec_ref = spec_ref_from_object(fn)

        decorator_kwargs: dict[str, object] = {}
        if deps is not None:
            decorator_kwargs["deps"] = deps

        entry = SpecEntry(
            kind="contract",
            spec_ref=spec_ref,
            module=cast(str, f.__module__),
            qualname=cast(str, f.__qualname__),
            source_file=_source_file(fn),
            obj=fn,
            decorator_kwargs=decorator_kwargs,
        )
        register_contract(entry)
        return fn

    if obj is not None:
        return _decorate(cast(F, obj))
    return _decorate
```

In `src/jaunt/__init__.py`, import and export `contract`:

```python
from jaunt.runtime import contract, magic, preserve, test
```

Add `"contract",` to `__all__` (after `"magic",`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_decorator.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/registry.py src/jaunt/runtime.py src/jaunt/__init__.py tests/test_contract_decorator.py
git commit -m "feat(contract): add @jaunt.contract no-op marker + contract registry"
```

---

### Task 2: `[contract]` config section

**Files:**
- Modify: `src/jaunt/config.py` (add `ContractConfig`, parsing, `JauntConfig.contract`)
- Test: `tests/test_contract_config.py`

**Interfaces:**
- Produces: `ContractConfig(battery_dir: str = "tests/contract", derive: list[str] = ["examples", "errors"], strength: bool = True)`; `JauntConfig.contract: ContractConfig`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_config.py
from __future__ import annotations

from pathlib import Path

from jaunt.config import load_config


def _write(tmp_path: Path, body: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(body, encoding="utf-8")
    return tmp_path


def test_contract_defaults(tmp_path: Path) -> None:
    root = _write(tmp_path, 'version = 1\n[paths]\nsource_roots = ["src"]\n')
    cfg = load_config(root=root)
    assert cfg.contract.battery_dir == "tests/contract"
    assert cfg.contract.derive == ["examples", "errors"]
    assert cfg.contract.strength is True


def test_contract_overrides(tmp_path: Path) -> None:
    root = _write(
        tmp_path,
        'version = 1\n[paths]\nsource_roots = ["src"]\n'
        '[contract]\nbattery_dir = "qa/contract"\nderive = ["examples"]\nstrength = false\n',
    )
    cfg = load_config(root=root)
    assert cfg.contract.battery_dir == "qa/contract"
    assert cfg.contract.derive == ["examples"]
    assert cfg.contract.strength is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_config.py -q`
Expected: FAIL — `AttributeError: 'JauntConfig' object has no attribute 'contract'`.

- [ ] **Step 3: Add the dataclass and parsing**

In `src/jaunt/config.py`, add after `AiderConfig` (line 82):

```python
@dataclass(frozen=True)
class ContractConfig:
    battery_dir: str = "tests/contract"
    derive: list[str] = field(default_factory=lambda: ["examples", "errors"])
    strength: bool = True
```

Add the field to `JauntConfig` (after `aider`, line 93):

```python
    contract: ContractConfig = field(default_factory=ContractConfig)
```

In `load_config`, add a `_VALID_DERIVE` constant near the other validity tuples (line 39):

```python
_VALID_DERIVE = ("examples", "errors")
```

Parse the table (after `aider_tbl` is read, ~line 201):

```python
    contract_tbl = _as_table(data.get("contract"), name="contract")

    if "battery_dir" in contract_tbl:
        contract_battery_dir = _as_str(contract_tbl["battery_dir"], name="contract.battery_dir")
    else:
        contract_battery_dir = "tests/contract"

    if "derive" in contract_tbl:
        contract_derive = _as_str_list(contract_tbl["derive"], name="contract.derive")
        for item in contract_derive:
            if item not in _VALID_DERIVE:
                raise JauntConfigError(
                    f"Invalid config: contract.derive entries must be in {_VALID_DERIVE!r}, "
                    f"got {item!r}."
                )
    else:
        contract_derive = ["examples", "errors"]

    if "strength" in contract_tbl:
        contract_strength = _as_bool(contract_tbl["strength"], name="contract.strength")
    else:
        contract_strength = True
```

Pass it into the returned `JauntConfig(...)` (after `aider=...`):

```python
        contract=ContractConfig(
            battery_dir=contract_battery_dir,
            derive=contract_derive,
            strength=contract_strength,
        ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_config.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/config.py tests/test_contract_config.py
git commit -m "feat(contract): add [contract] config section"
```

---

### Task 3: Contract digests (prose / signature / body)

**Files:**
- Modify: `src/jaunt/digest.py` (add `load_function_node`, `contract_digests`)
- Test: `tests/test_contract_digests.py`

**Interfaces:**
- Produces:
  - `load_function_node(source_file: str, qualname: str) -> ast.FunctionDef` (raises `ValueError` if not a top-level sync function)
  - `contract_digests(source_file: str, qualname: str) -> ContractDigests` where `ContractDigests` is a frozen dataclass with `prose: str`, `signature: str`, `body: str` (each a bare hex sha256, no `sha256:` prefix).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_digests.py
from __future__ import annotations

from pathlib import Path

from jaunt.digest import contract_digests

SRC_A = '''
def slugify(title: str) -> str:
    """Lowercase. Raises ValueError if empty."""
    return title.strip().lower()
'''

SRC_PROSE_CHANGED = '''
def slugify(title: str) -> str:
    """Lowercase and trim. Raises ValueError if empty after cleaning."""
    return title.strip().lower()
'''

SRC_BODY_CHANGED = '''
def slugify(title: str) -> str:
    """Lowercase. Raises ValueError if empty."""
    cleaned = title.strip()
    return cleaned.lower()
'''

SRC_SIG_CHANGED = '''
def slugify(title: str, *, sep: str = "-") -> str:
    """Lowercase. Raises ValueError if empty."""
    return title.strip().lower()
'''


def _digests(tmp_path: Path, src: str):
    p = tmp_path / "m.py"
    p.write_text(src, encoding="utf-8")
    return contract_digests(str(p), "slugify")


def test_prose_change_only_moves_prose_digest(tmp_path: Path) -> None:
    a = _digests(tmp_path / "a", make_dir=True) if False else None  # noqa: F841
    base = _digests(tmp_path, SRC_A)
    (tmp_path / "m.py").write_text(SRC_PROSE_CHANGED, encoding="utf-8")
    changed = contract_digests(str(tmp_path / "m.py"), "slugify")
    assert changed.prose != base.prose
    assert changed.signature == base.signature
    assert changed.body == base.body


def test_body_change_only_moves_body_digest(tmp_path: Path) -> None:
    base = _digests(tmp_path, SRC_A)
    (tmp_path / "m.py").write_text(SRC_BODY_CHANGED, encoding="utf-8")
    changed = contract_digests(str(tmp_path / "m.py"), "slugify")
    assert changed.body != base.body
    assert changed.prose == base.prose
    assert changed.signature == base.signature


def test_signature_change_moves_signature_digest(tmp_path: Path) -> None:
    base = _digests(tmp_path, SRC_A)
    (tmp_path / "m.py").write_text(SRC_SIG_CHANGED, encoding="utf-8")
    changed = contract_digests(str(tmp_path / "m.py"), "slugify")
    assert changed.signature != base.signature
    assert changed.prose == base.prose


def test_whitespace_reformat_does_not_change_body(tmp_path: Path) -> None:
    base = _digests(tmp_path, SRC_A)
    reformatted = SRC_A.replace("return title.strip().lower()", "return title.strip().lower()  # x")
    (tmp_path / "m.py").write_text(reformatted, encoding="utf-8")
    changed = contract_digests(str(tmp_path / "m.py"), "slugify")
    # Comments/trailing whitespace do not affect the AST-normalized body digest.
    assert changed.body == base.body
```

> Delete the stray `a = ...` noqa line before committing; it documents that `_digests` reuses the same path. Keep the test body minimal.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_digests.py -q`
Expected: FAIL — `ImportError: cannot import name 'contract_digests'`.

- [ ] **Step 3: Implement the digests**

In `src/jaunt/digest.py`, add imports at top (already imports `ast`, `hashlib`):

```python
from dataclasses import dataclass
```

Add at the end of the module:

```python
@dataclass(frozen=True, slots=True)
class ContractDigests:
    prose: str
    signature: str
    body: str


def load_function_node(source_file: str, qualname: str) -> ast.FunctionDef:
    """Load a top-level sync function node by name (v1: no classes/methods/async)."""

    if "." in qualname:
        raise ValueError(f"Contract specs must be top-level functions in v1, got {qualname!r}.")
    src = Path(source_file).read_text(encoding="utf-8")
    tree = ast.parse(src, filename=source_file)
    for top in tree.body:
        if isinstance(top, ast.AsyncFunctionDef) and top.name == qualname:
            raise ValueError(f"Contract function {qualname!r} is async; unsupported in v1.")
        if isinstance(top, ast.FunctionDef) and top.name == qualname:
            return top
    raise ValueError(f"Top-level function {qualname!r} not found in {source_file}.")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def contract_digests(source_file: str, qualname: str) -> ContractDigests:
    """Compute stable prose/signature/body digests for a contract function.

    - prose: the cleaned docstring (PEP-257), or "" if absent.
    - signature: AST-unparsed argument list + return annotation (normalizes formatting).
    - body: AST-unparsed body with the docstring statement stripped (normalizes
      comments/whitespace; changes only when the executable body changes).
    """

    node = load_function_node(source_file, qualname)
    prose = ast.get_docstring(node, clean=True) or ""

    sig = ast.unparse(node.args) + " -> " + (ast.unparse(node.returns) if node.returns else "")

    body = list(node.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    body_src = "\n".join(ast.unparse(stmt) for stmt in body)

    return ContractDigests(prose=_sha(prose), signature=_sha(sig), body=_sha(body_src))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_digests.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/digest.py tests/test_contract_digests.py
git commit -m "feat(contract): prose/signature/body digests for contract functions"
```

---

### Task 4: Contract-battery header (header.py)

**Files:**
- Modify: `src/jaunt/header.py` (add marker + format/parse for the battery header)
- Test: `tests/test_contract_header.py`

**Interfaces:**
- Produces:
  - `header.CONTRACT_BATTERY_MARKER: str`
  - `format_contract_battery_header(*, derived_from: str, prose_digest: str, signature: str, body_digest: str, strength: str, tool_version: str) -> str`
  - `parse_contract_battery_header(source: str) -> dict[str, str] | None` (returns keys without the `jaunt:` prefix: `contract`, `derived-from`, `prose-digest`, `signature`, `body-digest`, `strength`, `tool_version`; `None` if not a contract battery).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_header.py
from __future__ import annotations

from jaunt.header import (
    CONTRACT_BATTERY_MARKER,
    format_contract_battery_header,
    parse_contract_battery_header,
)


def test_round_trip() -> None:
    text = format_contract_battery_header(
        derived_from="slugify_demo.specs:slugify",
        prose_digest="91a3",
        signature="0c12",
        body_digest="7f55",
        strength="7/8",
        tool_version="0.4.4",
    )
    assert text.splitlines()[0] == CONTRACT_BATTERY_MARKER
    parsed = parse_contract_battery_header(text)
    assert parsed is not None
    assert parsed["derived-from"] == "slugify_demo.specs:slugify"
    assert parsed["prose-digest"] == "sha256:91a3"
    assert parsed["signature"] == "sha256:0c12"
    assert parsed["body-digest"] == "sha256:7f55"
    assert parsed["strength"] == "7/8"
    assert parsed["contract"] == "1"


def test_parse_returns_none_for_non_battery() -> None:
    assert parse_contract_battery_header("import pytest\n") is None
    # A magic-mode generated header is not a contract battery.
    assert parse_contract_battery_header("# This file was generated by jaunt. DO NOT EDIT.\n") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_header.py -q`
Expected: FAIL — `ImportError: cannot import name 'CONTRACT_BATTERY_MARKER'`.

- [ ] **Step 3: Implement the battery header**

In `src/jaunt/header.py`, add after `HEADER_MARKER` (line 8):

```python
CONTRACT_BATTERY_MARKER = (
    "# This file is maintained by jaunt (contract mode). Review like any test."
)


def _with_sha(value: str) -> str:
    return value if value.startswith("sha256:") else f"sha256:{value}"


def format_contract_battery_header(
    *,
    derived_from: str,
    prose_digest: str,
    signature: str,
    body_digest: str,
    strength: str,
    tool_version: str,
) -> str:
    lines = [
        CONTRACT_BATTERY_MARKER,
        "# jaunt:contract=1",
        f"# jaunt:derived-from={derived_from}",
        f"# jaunt:prose-digest={_with_sha(prose_digest)}",
        f"# jaunt:signature={_with_sha(signature)}",
        f"# jaunt:body-digest={_with_sha(body_digest)}",
        f"# jaunt:strength={strength}",
        f"# jaunt:tool_version={tool_version}",
    ]
    return "\n".join(lines) + "\n"


def parse_contract_battery_header(source: str) -> dict[str, str] | None:
    """Parse a contract-battery header. Returns None if not a contract battery."""

    lines = source.splitlines()
    if not lines or lines[0] != CONTRACT_BATTERY_MARKER:
        return None
    out: dict[str, str] = {}
    for line in lines[1:]:
        if not line.startswith("# jaunt:"):
            break
        rest = line[len("# jaunt:") :]
        if "=" not in rest:
            continue
        key, value = rest.split("=", 1)
        out[key.strip()] = value.strip()
    if out.get("contract") != "1":
        return None
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_header.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/header.py tests/test_contract_header.py
git commit -m "feat(contract): contract-battery header format/parse"
```

---

### Task 5: Battery file render / parse / merge (battery.py)

**Files:**
- Create: `src/jaunt/contract/__init__.py`
- Create: `src/jaunt/contract/battery.py`
- Test: `tests/test_contract_battery.py`

**Interfaces:**
- Consumes: `header.format_contract_battery_header`, `header.parse_contract_battery_header` (Task 4).
- Produces:
  - `DerivedRegion(region_id: str, code: str)` — `code` is the full source of one or more test functions for that region, no surrounding markers.
  - `ParsedBattery(header: dict[str, str] | None, regions: dict[str, str], preserved: str)`
  - `render_battery(*, import_module: str, func_name: str, regions: list[DerivedRegion], header_fields: dict[str, str]) -> str`
  - `parse_battery(source: str) -> ParsedBattery`
  - `merge_battery(existing: str | None, *, import_module: str, func_name: str, regions: list[DerivedRegion], header_fields: dict[str, str]) -> str`
  - `header_fields` keys: `derived_from`, `prose_digest`, `signature`, `body_digest`, `strength`, `tool_version` (passed straight to `format_contract_battery_header`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_battery.py
from __future__ import annotations

from jaunt.contract.battery import DerivedRegion, merge_battery, parse_battery, render_battery

FIELDS = {
    "derived_from": "demo:slugify",
    "prose_digest": "aa",
    "signature": "bb",
    "body_digest": "cc",
    "strength": "2/2",
    "tool_version": "0.4.4",
}

EX = DerivedRegion(
    region_id="examples",
    code='@pytest.mark.parametrize("arg,want", [("Hi", "hi")])\n'
    "def test_examples(arg, want):  # derived from: Examples\n"
    "    assert slugify(arg) == want",
)


def test_render_is_parseable_and_round_trips() -> None:
    text = render_battery(
        import_module="demo", func_name="slugify", regions=[EX], header_fields=FIELDS
    )
    assert "import pytest" in text
    assert "from demo import slugify" in text
    parsed = parse_battery(text)
    assert parsed.header is not None
    assert parsed.header["derived-from"] == "demo:slugify"
    assert "test_examples" in parsed.regions["examples"]
    assert parsed.preserved.strip() == ""


def test_merge_preserves_hand_added_cases_and_updates_region() -> None:
    text = render_battery(
        import_module="demo", func_name="slugify", regions=[EX], header_fields=FIELDS
    )
    # User appends a hand-written test outside the derived markers.
    text += "\n\ndef test_hand_added():\n    assert slugify('A') == 'a'\n"

    new_region = DerivedRegion(
        region_id="examples",
        code='@pytest.mark.parametrize("arg,want", [("Hi", "hi"), ("Yo", "yo")])\n'
        "def test_examples(arg, want):  # derived from: Examples\n"
        "    assert slugify(arg) == want",
    )
    merged = merge_battery(
        text,
        import_module="demo",
        func_name="slugify",
        regions=[new_region],
        header_fields={**FIELDS, "body_digest": "dd"},
    )
    assert "test_hand_added" in merged  # preserved
    assert '("Yo", "yo")' in merged  # region updated
    assert parse_battery(merged).header["body-digest"] == "sha256:dd"  # header refreshed
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_battery.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.contract'`.

- [ ] **Step 3: Create the package and battery module**

Create `src/jaunt/contract/__init__.py`:

```python
"""Contract mode: committed code as source of truth, prose as contract."""

from __future__ import annotations
```

Create `src/jaunt/contract/battery.py`:

```python
"""Render, parse, and merge committed contract test batteries (plain pytest)."""

from __future__ import annotations

import re
from dataclasses import dataclass

from jaunt.header import (
    CONTRACT_BATTERY_MARKER,
    format_contract_battery_header,
    parse_contract_battery_header,
)


@dataclass(frozen=True, slots=True)
class DerivedRegion:
    region_id: str
    code: str


@dataclass(frozen=True, slots=True)
class ParsedBattery:
    header: dict[str, str] | None
    regions: dict[str, str]
    preserved: str


def _begin(region_id: str) -> str:
    return f"# >>> jaunt:derived {region_id}"


def _end(region_id: str) -> str:
    return f"# <<< jaunt:derived {region_id}"


def _header_text(header_fields: dict[str, str]) -> str:
    return format_contract_battery_header(
        derived_from=header_fields["derived_from"],
        prose_digest=header_fields["prose_digest"],
        signature=header_fields["signature"],
        body_digest=header_fields["body_digest"],
        strength=header_fields["strength"],
        tool_version=header_fields["tool_version"],
    )


def _region_block(region: DerivedRegion) -> str:
    return f"{_begin(region.region_id)}\n{region.code.rstrip()}\n{_end(region.region_id)}"


def render_battery(
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
    preserved: str = "",
) -> str:
    parts = [
        _header_text(header_fields).rstrip(),
        "import pytest",
        f"from {import_module} import {func_name}",
        "",
    ]
    for region in regions:
        parts.append(_region_block(region))
        parts.append("")
    body = "\n".join(parts).rstrip() + "\n"
    if preserved.strip():
        body += "\n\n" + preserved.strip() + "\n"
    return body


_REGION_RE = re.compile(
    r"^# >>> jaunt:derived (?P<rid>\S+)\n(?P<code>.*?)\n# <<< jaunt:derived (?P=rid)\s*$",
    re.DOTALL | re.MULTILINE,
)


def parse_battery(source: str) -> ParsedBattery:
    header = parse_contract_battery_header(source)

    regions: dict[str, str] = {}
    for m in _REGION_RE.finditer(source):
        regions[m.group("rid")] = m.group("code")

    # Preserved = everything with header lines, the import preamble, and derived
    # regions removed. What remains is hand-added content.
    stripped = _REGION_RE.sub("", source)
    out_lines: list[str] = []
    in_header = stripped.splitlines()[:1] == [CONTRACT_BATTERY_MARKER]
    for line in stripped.splitlines():
        if in_header and (line == CONTRACT_BATTERY_MARKER or line.startswith("# jaunt:")):
            continue
        in_header = False
        if line.strip() == "import pytest":
            continue
        if re.match(r"^from \S+ import \S+$", line.strip()):
            continue
        out_lines.append(line)
    preserved = "\n".join(out_lines).strip()
    return ParsedBattery(header=header, regions=regions, preserved=preserved)


def merge_battery(
    existing: str | None,
    *,
    import_module: str,
    func_name: str,
    regions: list[DerivedRegion],
    header_fields: dict[str, str],
) -> str:
    preserved = ""
    if existing is not None:
        preserved = parse_battery(existing).preserved
    return render_battery(
        import_module=import_module,
        func_name=func_name,
        regions=regions,
        header_fields=header_fields,
        preserved=preserved,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_battery.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/__init__.py src/jaunt/contract/battery.py tests/test_contract_battery.py
git commit -m "feat(contract): battery file render/parse/merge with preserve"
```

---

### Task 6: Deterministic derivation — blocks, rendering, evaluation (derive.py)

**Files:**
- Create: `src/jaunt/contract/derive.py`
- Test: `tests/test_contract_derive.py`

**Interfaces:**
- Consumes: `DerivedRegion` (battery.py).
- Produces:
  - `ExampleRow(input_expr: str, expected_expr: str)`
  - `RaisesRow(input_expr: str, exc_name: str)`
  - `ContractBlocks(examples: tuple[ExampleRow, ...], raises: tuple[RaisesRow, ...])` with `.is_empty() -> bool`
  - `extract_blocks_structured(docstring: str) -> ContractBlocks`
  - `derive_regions(blocks: ContractBlocks, *, func_name: str, derive: list[str]) -> list[DerivedRegion]`
  - `evaluate_blocks(fn: object, blocks: ContractBlocks, namespace: dict[str, object]) -> list[str]` (returns human-readable failure strings; empty == all pass)

v1 grammar (single positional arg):
- `Examples:` rows — `- <input_expr> -> <expected_expr>`
- `Raises:` rows — `- <input_expr> raises <ExcName>` **or** `- <ExcName> on <input_expr>`. Rows without an explicit input (e.g. `- ValueError if empty`) are ignored by the deterministic extractor (model fallback in Task 10 fills inputs).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_derive.py
from __future__ import annotations

from jaunt.contract.derive import (
    ContractBlocks,
    ExampleRow,
    RaisesRow,
    derive_regions,
    evaluate_blocks,
    extract_blocks_structured,
)

DOC = '''
Convert a title to a slug.

Examples:
- "  Hello, World!  " -> "hello-world"
- "C++ > Java" -> "c-java"

Raises:
- "" raises ValueError
- "   " raises ValueError
'''


def test_extract_examples_and_raises() -> None:
    blocks = extract_blocks_structured(DOC)
    assert blocks.examples == (
        ExampleRow('"  Hello, World!  "', '"hello-world"'),
        ExampleRow('"C++ > Java"', '"c-java"'),
    )
    assert blocks.raises == (
        RaisesRow('""', "ValueError"),
        RaisesRow('"   "', "ValueError"),
    )


def test_input_less_raises_row_is_ignored_by_deterministic_path() -> None:
    blocks = extract_blocks_structured("Raises:\n- ValueError if the title is empty.\n")
    assert blocks.raises == ()


def test_derive_regions_emit_parseable_pytest() -> None:
    blocks = extract_blocks_structured(DOC)
    regions = derive_regions(blocks, func_name="slugify", derive=["examples", "errors"])
    ids = {r.region_id for r in regions}
    assert ids == {"examples", "errors"}
    examples = next(r for r in regions if r.region_id == "examples")
    assert "def test_examples(" in examples.code
    assert '"hello-world"' in examples.code
    errors = next(r for r in regions if r.region_id == "errors")
    assert "pytest.raises(ValueError)" in errors.code


def test_evaluate_blocks_against_real_function() -> None:
    import re

    def slugify(title: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
        if not cleaned:
            raise ValueError("empty")
        return cleaned

    blocks = extract_blocks_structured(DOC)
    ns = {"slugify": slugify}
    assert evaluate_blocks(slugify, blocks, ns) == []  # body satisfies its own contract

    def broken(title: str) -> str:
        return title  # ignores the contract

    assert evaluate_blocks(broken, blocks, {"slugify": broken}) != []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_derive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.contract.derive'`.

- [ ] **Step 3: Implement the deterministic deriver**

Create `src/jaunt/contract/derive.py`:

```python
"""Derive a structured contract from docstring prose and render it to pytest.

v1 is deterministic for structured `Examples:`/`Raises:` blocks (single positional
argument). The model fallback for unstructured prose is added in a later task.
"""

from __future__ import annotations

import ast
import builtins
import re
from dataclasses import dataclass

from jaunt.contract.battery import DerivedRegion


@dataclass(frozen=True, slots=True)
class ExampleRow:
    input_expr: str
    expected_expr: str


@dataclass(frozen=True, slots=True)
class RaisesRow:
    input_expr: str
    exc_name: str


@dataclass(frozen=True, slots=True)
class ContractBlocks:
    examples: tuple[ExampleRow, ...] = ()
    raises: tuple[RaisesRow, ...] = ()

    def is_empty(self) -> bool:
        return not self.examples and not self.raises


_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z ]*):\s*$")
_EXC_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _section_lines(docstring: str, name: str) -> list[str]:
    """Return the bullet lines under a `Name:` header until the next header/blank."""

    lines = docstring.splitlines()
    out: list[str] = []
    collecting = False
    for raw in lines:
        line = raw.strip()
        m = _HEADER_RE.match(line)
        if m:
            collecting = m.group(1).strip().lower() == name.lower()
            continue
        if collecting:
            if not line:
                break
            if line.startswith("- "):
                out.append(line[2:].strip())
    return out


def _is_expr(text: str) -> bool:
    try:
        ast.parse(text, mode="eval")
        return True
    except SyntaxError:
        return False


def _parse_examples(docstring: str) -> tuple[ExampleRow, ...]:
    rows: list[ExampleRow] = []
    for line in _section_lines(docstring, "Examples"):
        if "->" not in line:
            continue
        left, right = line.split("->", 1)
        left, right = left.strip(), right.strip()
        if _is_expr(left) and _is_expr(right):
            rows.append(ExampleRow(left, right))
    return tuple(rows)


def _parse_raises(docstring: str) -> tuple[RaisesRow, ...]:
    rows: list[RaisesRow] = []
    for line in _section_lines(docstring, "Raises"):
        # Form A: "<input> raises <Exc>"
        if " raises " in line:
            inp, exc = line.split(" raises ", 1)
            inp, exc = inp.strip(), exc.strip().rstrip(".")
            if _is_expr(inp) and _EXC_NAME_RE.match(exc):
                rows.append(RaisesRow(inp, exc))
                continue
        # Form B: "<Exc> on <input>"
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s+on\s+(.+)$", line)
        if m and _is_expr(m.group(2).strip()):
            rows.append(RaisesRow(m.group(2).strip(), m.group(1)))
    return tuple(rows)


def extract_blocks_structured(docstring: str) -> ContractBlocks:
    return ContractBlocks(examples=_parse_examples(docstring), raises=_parse_raises(docstring))


def _render_examples_region(rows: tuple[ExampleRow, ...], func_name: str) -> DerivedRegion:
    cases = ",\n        ".join(f"({r.input_expr}, {r.expected_expr})" for r in rows)
    code = (
        f'@pytest.mark.parametrize("arg,want", [\n        {cases},\n    ])\n'
        f"def test_examples(arg, want):  # derived from: Examples\n"
        f"    assert {func_name}(arg) == want"
    )
    return DerivedRegion(region_id="examples", code=code)


def _render_errors_region(rows: tuple[RaisesRow, ...], func_name: str) -> DerivedRegion:
    by_exc: dict[str, list[str]] = {}
    for r in rows:
        by_exc.setdefault(r.exc_name, []).append(r.input_expr)
    blocks: list[str] = []
    for exc, inputs in by_exc.items():
        params = ", ".join(inputs)
        fn_suffix = exc.lower()
        blocks.append(
            f'@pytest.mark.parametrize("arg", [{params}])\n'
            f"def test_raises_{fn_suffix}(arg):  # derived from: Raises\n"
            f"    with pytest.raises({exc}):\n"
            f"        {func_name}(arg)"
        )
    return DerivedRegion(region_id="errors", code="\n\n".join(blocks))


def derive_regions(
    blocks: ContractBlocks, *, func_name: str, derive: list[str]
) -> list[DerivedRegion]:
    regions: list[DerivedRegion] = []
    if "examples" in derive and blocks.examples:
        regions.append(_render_examples_region(blocks.examples, func_name))
    if "errors" in derive and blocks.raises:
        regions.append(_render_errors_region(blocks.raises, func_name))
    return regions


def _resolve_exc(name: str, namespace: dict[str, object]) -> type[BaseException]:
    obj = namespace.get(name, getattr(builtins, name, None))
    if isinstance(obj, type) and issubclass(obj, BaseException):
        return obj
    raise ValueError(f"Unknown exception type in contract: {name!r}")


def evaluate_blocks(
    fn: object, blocks: ContractBlocks, namespace: dict[str, object]
) -> list[str]:
    """Run derived cases directly against `fn`. Returns failure descriptions."""

    failures: list[str] = []
    for row in blocks.examples:
        try:
            arg = eval(row.input_expr, dict(namespace))  # noqa: S307 - literal exprs from prose
            want = eval(row.expected_expr, dict(namespace))  # noqa: S307
            got = fn(arg)  # type: ignore[operator]
            if got != want:
                failures.append(f"example {row.input_expr} -> {got!r}, expected {want!r}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"example {row.input_expr} raised {type(exc).__name__}: {exc}")
    for row in blocks.raises:
        exc_type = _resolve_exc(row.exc_name, namespace)
        try:
            arg = eval(row.input_expr, dict(namespace))  # noqa: S307
            fn(arg)  # type: ignore[operator]
            failures.append(f"raises {row.input_expr}: expected {row.exc_name}, none raised")
        except exc_type:
            pass
        except Exception as exc:  # noqa: BLE001
            failures.append(
                f"raises {row.input_expr}: expected {row.exc_name}, got {type(exc).__name__}"
            )
    return failures
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_derive.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/derive.py tests/test_contract_derive.py
git commit -m "feat(contract): deterministic prose->battery derivation + evaluation"
```

---

### Task 7: Drift state machine (drift.py)

**Files:**
- Create: `src/jaunt/contract/drift.py`
- Test: `tests/test_contract_drift.py`

**Interfaces:**
- Produces:
  - `class DriftState(enum.Enum)` with members `UNBUILT`, `STALE_PROSE`, `SIGNATURE_DRIFT`, `BEHAVIOR_DRIFT`, `REFACTORED`, `IN_SYNC`.
  - `compute_drift_state(*, has_battery: bool, prose_match: bool, signature_match: bool, body_match: bool, battery_passed: bool | None) -> DriftState`
  - `is_blocking(state: DriftState) -> bool`
  - `BLOCKING_MESSAGE: dict[DriftState, str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_drift.py
from __future__ import annotations

from jaunt.contract.drift import DriftState, compute_drift_state, is_blocking


def s(**kw):
    base = dict(
        has_battery=True,
        prose_match=True,
        signature_match=True,
        body_match=True,
        battery_passed=True,
    )
    base.update(kw)
    return compute_drift_state(**base)


def test_unbuilt() -> None:
    assert s(has_battery=False) is DriftState.UNBUILT
    assert is_blocking(DriftState.UNBUILT)


def test_stale_prose_precedes_signature() -> None:
    assert s(prose_match=False, signature_match=False) is DriftState.STALE_PROSE
    assert is_blocking(DriftState.STALE_PROSE)


def test_signature_drift() -> None:
    assert s(signature_match=False) is DriftState.SIGNATURE_DRIFT
    assert is_blocking(DriftState.SIGNATURE_DRIFT)


def test_behavior_drift() -> None:
    assert s(battery_passed=False) is DriftState.BEHAVIOR_DRIFT
    assert is_blocking(DriftState.BEHAVIOR_DRIFT)


def test_refactored_benign_passes() -> None:
    st = s(body_match=False, battery_passed=True)
    assert st is DriftState.REFACTORED
    assert not is_blocking(st)


def test_in_sync_passes() -> None:
    st = s()
    assert st is DriftState.IN_SYNC
    assert not is_blocking(st)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_drift.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.contract.drift'`.

- [ ] **Step 3: Implement the state machine**

Create `src/jaunt/contract/drift.py`:

```python
"""Deterministic drift state machine for contract functions (no model)."""

from __future__ import annotations

import enum


class DriftState(enum.Enum):
    UNBUILT = "unbuilt"
    STALE_PROSE = "stale-prose"
    SIGNATURE_DRIFT = "signature-drift"
    BEHAVIOR_DRIFT = "behavior-drift"
    REFACTORED = "refactored"
    IN_SYNC = "in-sync"


_BLOCKING = frozenset(
    {
        DriftState.UNBUILT,
        DriftState.STALE_PROSE,
        DriftState.SIGNATURE_DRIFT,
        DriftState.BEHAVIOR_DRIFT,
    }
)

BLOCKING_MESSAGE: dict[DriftState, str] = {
    DriftState.UNBUILT: "no contract battery; run `jaunt reconcile`.",
    DriftState.STALE_PROSE: "contract prose changed; run `jaunt reconcile`.",
    DriftState.SIGNATURE_DRIFT: "signature changed; run `jaunt reconcile`.",
    DriftState.BEHAVIOR_DRIFT: "body no longer satisfies the contract; fix the body or reconcile.",
}


def is_blocking(state: DriftState) -> bool:
    return state in _BLOCKING


def compute_drift_state(
    *,
    has_battery: bool,
    prose_match: bool,
    signature_match: bool,
    body_match: bool,
    battery_passed: bool | None,
) -> DriftState:
    """Resolve state in precedence order (§5 of the design).

    Steps 1-3 short-circuit before the battery runs, so `battery_passed` may be
    None when an earlier hashing check already determined the state.
    """

    if not has_battery:
        return DriftState.UNBUILT
    if not prose_match:
        return DriftState.STALE_PROSE
    if not signature_match:
        return DriftState.SIGNATURE_DRIFT
    if battery_passed is False:
        return DriftState.BEHAVIOR_DRIFT
    if not body_match:
        return DriftState.REFACTORED
    return DriftState.IN_SYNC
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_drift.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/drift.py tests/test_contract_drift.py
git commit -m "feat(contract): deterministic drift state machine"
```

---

### Task 8: `jaunt check` command + orchestration (runner.py)

**Files:**
- Create: `src/jaunt/contract/runner.py`
- Modify: `src/jaunt/discovery.py:217` (extend `kind` Literal to include `"contract"`)
- Modify: `src/jaunt/cli.py` (add `_discover_contract_specs`, `cmd_check`, parser, dispatch)
- Test: `tests/test_cli_check.py`

**Interfaces:**
- Consumes: `contract_digests` (digest.py), `parse_battery` (battery.py), `compute_drift_state`/`is_blocking`/`BLOCKING_MESSAGE` (drift.py), `SpecEntry`.
- Produces:
  - `runner.battery_path(root: Path, battery_dir: str, entry: SpecEntry) -> Path`
  - `runner.ContractStatus(spec_ref: str, state: DriftState, strength: str | None, battery_path: Path, detail: str)`
  - `runner.evaluate_entry(root: Path, battery_dir: str, derive: list[str], entry: SpecEntry, *, run_battery: Callable[[Path], bool | None]) -> ContractStatus` — `run_battery` returns True/False/None; only called when steps 1-3 pass.
  - `runner.run_battery_file(path: Path, *, root: Path, source_roots: list[str]) -> bool` (subprocess pytest)
  - `cli._discover_contract_specs(root, cfg) -> dict[SpecRef, SpecEntry]`
  - `cli.cmd_check(args) -> int`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_check.py
from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import render_battery
from jaunt.digest import contract_digests

SRC = '''
import jaunt


@jaunt.contract
def shout(text: str) -> str:
    """
    Uppercase a string.

    Examples:
    - "hi" -> "HI"

    Raises:
    - "" raises ValueError
    """
    if not text:
        raise ValueError("empty")
    return text.upper()
'''


def _project(tmp_path: Path, *, prose_digest_override: str | None = None) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    digs = contract_digests(str(tmp_path / "src" / "demo.py"), "shout")
    battery_dir = tmp_path / "tests" / "contract" / "demo"
    battery_dir.mkdir(parents=True)
    region_examples = (
        '@pytest.mark.parametrize("arg,want", [("hi", "HI")])\n'
        "def test_examples(arg, want):  # derived from: Examples\n"
        "    assert shout(arg) == want"
    )
    region_errors = (
        '@pytest.mark.parametrize("arg", [""])\n'
        "def test_raises_valueerror(arg):  # derived from: Raises\n"
        "    with pytest.raises(ValueError):\n"
        "        shout(arg)"
    )
    from jaunt.contract.battery import DerivedRegion

    text = render_battery(
        import_module="demo",
        func_name="shout",
        regions=[
            DerivedRegion("examples", region_examples),
            DerivedRegion("errors", region_errors),
        ],
        header_fields={
            "derived_from": "demo:shout",
            "prose_digest": prose_digest_override or digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": "3/3",
            "tool_version": "0.4.4",
        },
    )
    (battery_dir / "test_shout.py").write_text(text, encoding="utf-8")
    return tmp_path


def test_check_passes_when_in_sync(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_OK


def test_check_blocks_on_stale_prose(tmp_path: Path) -> None:
    root = _project(tmp_path, prose_digest_override="sha256:deadbeef")
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE


def test_check_blocks_when_unbuilt(tmp_path: Path) -> None:
    root = _project(tmp_path)
    # Remove the battery -> unbuilt.
    (root / "tests" / "contract" / "demo" / "test_shout.py").unlink()
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_check.py -q`
Expected: FAIL — `AttributeError: module 'jaunt.cli' has no attribute 'cmd_check'`.

- [ ] **Step 3: Implement the runner**

Create `src/jaunt/contract/runner.py`:

```python
"""Wire digests + battery header + drift state for a contract function."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jaunt.contract.battery import parse_battery
from jaunt.contract.drift import DriftState, compute_drift_state
from jaunt.digest import contract_digests
from jaunt.registry import SpecEntry


def battery_path(root: Path, battery_dir: str, entry: SpecEntry) -> Path:
    parts = entry.module.split(".")
    return root / battery_dir / Path(*parts) / f"test_{entry.qualname}.py"


@dataclass(frozen=True, slots=True)
class ContractStatus:
    spec_ref: str
    state: DriftState
    strength: str | None
    battery_path: Path
    detail: str = ""


def _norm(value: str) -> str:
    return value[len("sha256:") :] if value.startswith("sha256:") else value


def evaluate_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    entry: SpecEntry,
    *,
    run_battery: Callable[[Path], bool | None],
) -> ContractStatus:
    path = battery_path(root, battery_dir, entry)
    spec_ref = str(entry.spec_ref)

    if not path.is_file():
        return ContractStatus(spec_ref, DriftState.UNBUILT, None, path)

    parsed = parse_battery(path.read_text(encoding="utf-8"))
    header = parsed.header
    if header is None:
        return ContractStatus(spec_ref, DriftState.UNBUILT, None, path)

    digs = contract_digests(entry.source_file, entry.qualname)
    prose_match = _norm(header.get("prose-digest", "")) == digs.prose
    signature_match = _norm(header.get("signature", "")) == digs.signature
    body_match = _norm(header.get("body-digest", "")) == digs.body
    strength = header.get("strength")

    # Short-circuit before running the battery (steps 1-3).
    if not (prose_match and signature_match):
        state = compute_drift_state(
            has_battery=True,
            prose_match=prose_match,
            signature_match=signature_match,
            body_match=body_match,
            battery_passed=None,
        )
        return ContractStatus(spec_ref, state, strength, path)

    passed = run_battery(path)
    state = compute_drift_state(
        has_battery=True,
        prose_match=prose_match,
        signature_match=signature_match,
        body_match=body_match,
        battery_passed=passed,
    )
    return ContractStatus(spec_ref, state, strength, path)


def run_battery_file(path: Path, *, root: Path, source_roots: list[str]) -> bool:
    """Run a single battery file with pytest in a subprocess. True == all passed."""

    import os

    env = dict(os.environ)
    extra = os.pathsep.join(str((root / sr).resolve()) for sr in source_roots)
    env["PYTHONPATH"] = extra + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-q",
            "--no-header",
            "-p",
            "no:cacheprovider",
            "--import-mode=importlib",
        ],
        cwd=str(root),
        env=env,
        capture_output=True,
        text=True,
    )
    return proc.returncode == 0
```

In `src/jaunt/discovery.py`, extend the `import_and_collect` kind Literal (line 217):

```python
def import_and_collect(
    module_names: list[str], *, kind: Literal["magic", "test", "contract"]
) -> None:
```

- [ ] **Step 4: Add `_discover_contract_specs`, `cmd_check`, parser + dispatch**

In `src/jaunt/cli.py`, add a discovery helper near `_discover_test_spec_modules` (~line 426):

```python
def _discover_contract_specs(*, root: Path, cfg: JauntConfig):
    from jaunt import discovery, registry

    source_dirs = [root / sr for sr in cfg.paths.source_roots]
    _prepend_sys_path([*source_dirs, root])
    registry.clear_registries()
    modules = discovery.discover_modules(
        roots=[d for d in source_dirs if d.exists()],
        exclude=[],
        generated_dir=cfg.paths.generated_dir,
    )
    discovery.evict_modules_for_import(
        module_names=modules, roots=[d for d in source_dirs if d.exists()]
    )
    discovery.import_and_collect(modules, kind="contract")
    return dict(registry.get_contract_registry())
```

Add the command body (near `cmd_status`):

```python
def cmd_check(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.contract import runner
        from jaunt.contract.drift import BLOCKING_MESSAGE, is_blocking

        specs = _discover_contract_specs(root=root, cfg=cfg)
        if not specs:
            if json_mode:
                _emit_json({"command": "check", "ok": True, "blocked": [], "checked": []})
            else:
                print("Contract check: 0 contract function(s).")
            return EXIT_OK

        def _run(path: Path) -> bool:
            return runner.run_battery_file(
                path, root=root, source_roots=cfg.paths.source_roots
            )

        results = [
            runner.evaluate_entry(
                root,
                cfg.contract.battery_dir,
                cfg.contract.derive,
                entry,
                run_battery=_run,
            )
            for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref))
        ]
        blocked = [r for r in results if is_blocking(r.state)]

        if json_mode:
            _emit_json(
                {
                    "command": "check",
                    "ok": not blocked,
                    "blocked": [
                        {"ref": r.spec_ref, "state": r.state.value} for r in blocked
                    ],
                    "checked": [
                        {"ref": r.spec_ref, "state": r.state.value} for r in results
                    ],
                }
            )
        else:
            for r in results:
                mark = "BLOCK" if is_blocking(r.state) else "ok"
                line = f"[{mark}] {r.spec_ref}: {r.state.value}"
                if is_blocking(r.state):
                    line += f" — {BLOCKING_MESSAGE.get(r.state, '')}"
                print(line)
            print(f"Contract check: {len(results)} checked, {len(blocked)} blocked.")

        return EXIT_PYTEST_FAILURE if blocked else EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "check", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
```

Register the parser (after the `status` parser, ~line 178):

```python
    check_p = subparsers.add_parser(
        "check", help="Verify committed contract batteries (deterministic, no model)."
    )
    _add_common_flags(check_p)
```

Add to the dispatch in `main` (after the `status` branch, ~line 1939):

```python
    if args.command == "check":
        return cmd_check(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_check.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Run the full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/runner.py src/jaunt/discovery.py src/jaunt/cli.py tests/test_cli_check.py
git commit -m "feat(contract): jaunt check — deterministic CI gate over batteries"
```

**Milestone 1 complete:** `jaunt check` gates a project on committed batteries with no API key. Hand-written or structured-prose batteries work end-to-end.

---

## Milestone 2 — Derivation & on-ramp

### Task 9: Contract strength — AST mutator + scoring (strength.py)

**Files:**
- Create: `src/jaunt/contract/strength.py`
- Test: `tests/test_contract_strength.py`

**Interfaces:**
- Consumes: `ContractBlocks`, `evaluate_blocks` (derive.py).
- Produces:
  - `iter_mutants(func_source: str) -> Iterator[str]` — yields full mutated function source strings, one mutation each.
  - `compute_strength(func_source: str, func_name: str, blocks: ContractBlocks, namespace: dict[str, object]) -> tuple[int, int]` — `(killed, applicable)`.
  - `format_strength(killed: int, applicable: int) -> str` — `"K/N"`.

Operator set (v1): comparison-op swap, boolean connective swap (`and`/`or`), boundary on int constants (`n`→`n+1`), arithmetic swap (`+`/`-`, `*`/`/`), constant replacement (`True`/`False`, `0`/`1`, str→`""`), statement deletion, and `return X`→`return None`. No timeout in v1 (pure functions run fast); documented as a fast-follow.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_strength.py
from __future__ import annotations

from jaunt.contract.derive import ContractBlocks, ExampleRow, extract_blocks_structured
from jaunt.contract.strength import compute_strength, format_strength, iter_mutants

STRONG_SRC = '''
def clamp(n: int) -> int:
    """Clamp n into [0, 10]."""
    if n < 0:
        return 0
    if n > 10:
        return 10
    return n
'''

STRONG_DOC = """
Examples:
- -5 -> 0
- 5 -> 5
- 15 -> 10
- 0 -> 0
- 10 -> 10
"""


def test_iter_mutants_produces_multiple_variants() -> None:
    mutants = list(iter_mutants(STRONG_SRC))
    assert len(mutants) >= 5
    assert all(m != STRONG_SRC for m in mutants)
    # Each mutant is still parseable Python.
    import ast

    for m in mutants:
        ast.parse(m)


def test_strong_contract_kills_most_mutants() -> None:
    blocks = extract_blocks_structured(STRONG_DOC)
    killed, applicable = compute_strength(STRONG_SRC, "clamp", blocks, {})
    assert applicable >= 5
    assert killed / applicable >= 0.6
    assert "/" in format_strength(killed, applicable)


def test_vacuous_contract_scores_low() -> None:
    # No example/raises rows -> nothing pins the body -> all mutants survive.
    killed, applicable = compute_strength(STRONG_SRC, "clamp", ContractBlocks(), {})
    assert killed == 0


def test_single_weak_example_survives_many_mutants() -> None:
    blocks = ContractBlocks(examples=(ExampleRow("5", "5"),))
    killed, applicable = compute_strength(STRONG_SRC, "clamp", blocks, {})
    # Only the n=5 passthrough is pinned; boundary mutants survive.
    assert killed < applicable
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_strength.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.contract.strength'`.

- [ ] **Step 3: Implement the mutator and scorer**

Create `src/jaunt/contract/strength.py`:

```python
"""Scoped AST mutation scoring: does the contract battery actually pin the body?"""

from __future__ import annotations

import ast
import copy
from collections.abc import Iterator

from jaunt.contract.derive import ContractBlocks, evaluate_blocks

_CMP_SWAP: dict[type[ast.cmpop], type[ast.cmpop]] = {
    ast.Lt: ast.LtE,
    ast.LtE: ast.Lt,
    ast.Gt: ast.GtE,
    ast.GtE: ast.Gt,
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
}

_BINOP_SWAP: dict[type[ast.operator], type[ast.operator]] = {
    ast.Add: ast.Sub,
    ast.Sub: ast.Add,
    ast.Mult: ast.Div,
    ast.Div: ast.Mult,
}

_BOOL_SWAP: dict[type[ast.boolop], type[ast.boolop]] = {
    ast.And: ast.Or,
    ast.Or: ast.And,
}


def _func_node(tree: ast.Module) -> ast.FunctionDef:
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            return node
    raise ValueError("no top-level function in source")


def _mutation_targets(tree: ast.Module) -> list[ast.AST]:
    return list(ast.walk(tree))


def iter_mutants(func_source: str) -> Iterator[str]:
    """Yield one-mutation variants of the function source."""

    base = ast.parse(func_source)
    nodes = _mutation_targets(base)

    for i, node in enumerate(nodes):
        for mutated in _mutate_node(base, i, node):
            yield mutated


def _emit(base: ast.Module, i: int, transform) -> str | None:
    clone = copy.deepcopy(base)
    target = list(ast.walk(clone))[i]
    if not transform(target):
        return None
    ast.fix_missing_locations(clone)
    try:
        return ast.unparse(clone)
    except Exception:  # noqa: BLE001
        return None


def _mutate_node(base: ast.Module, i: int, node: ast.AST) -> Iterator[str]:
    if isinstance(node, ast.Compare) and node.ops and type(node.ops[0]) in _CMP_SWAP:
        out = _emit(base, i, lambda t: _swap_cmp(t))
        if out:
            yield out
    if isinstance(node, ast.BoolOp) and type(node.op) in _BOOL_SWAP:
        out = _emit(base, i, lambda t: _swap_bool(t))
        if out:
            yield out
    if isinstance(node, ast.BinOp) and type(node.op) in _BINOP_SWAP:
        out = _emit(base, i, lambda t: _swap_binop(t))
        if out:
            yield out
    if isinstance(node, ast.Constant):
        out = _emit(base, i, lambda t: _mutate_const(t))
        if out:
            yield out
    if isinstance(node, ast.Return) and node.value is not None:
        out = _emit(base, i, lambda t: _default_return(t))
        if out:
            yield out


def _swap_cmp(t: ast.AST) -> bool:
    if isinstance(t, ast.Compare) and t.ops:
        t.ops[0] = _CMP_SWAP[type(t.ops[0])]()
        return True
    return False


def _swap_bool(t: ast.AST) -> bool:
    if isinstance(t, ast.BoolOp):
        t.op = _BOOL_SWAP[type(t.op)]()
        return True
    return False


def _swap_binop(t: ast.AST) -> bool:
    if isinstance(t, ast.BinOp):
        t.op = _BINOP_SWAP[type(t.op)]()
        return True
    return False


def _mutate_const(t: ast.AST) -> bool:
    if not isinstance(t, ast.Constant):
        return False
    v = t.value
    if isinstance(v, bool):
        t.value = not v
        return True
    if isinstance(v, int):
        t.value = v + 1
        return True
    if isinstance(v, str) and v != "":
        t.value = ""
        return True
    return False


def _default_return(t: ast.AST) -> bool:
    if isinstance(t, ast.Return):
        t.value = ast.Constant(value=None)
        return True
    return False


def compute_strength(
    func_source: str,
    func_name: str,
    blocks: ContractBlocks,
    namespace: dict[str, object],
) -> tuple[int, int]:
    """Return (killed, applicable). A mutant is killed if any derived case fails."""

    if blocks.is_empty():
        # Nothing pins the body; every mutant survives by definition.
        applicable = sum(1 for _ in iter_mutants(func_source))
        return (0, applicable)

    killed = 0
    applicable = 0
    for mutant_src in iter_mutants(func_source):
        ns: dict[str, object] = dict(namespace)
        try:
            exec(compile(mutant_src, "<mutant>", "exec"), ns)  # noqa: S102
        except Exception:  # noqa: BLE001 - non-applicable mutant
            continue
        fn = ns.get(func_name)
        if not callable(fn):
            continue
        applicable += 1
        if evaluate_blocks(fn, blocks, ns):  # any failure -> killed
            killed += 1
    return (killed, applicable)


def format_strength(killed: int, applicable: int) -> str:
    return f"{killed}/{applicable}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_strength.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint, typecheck, commit**

```bash
uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/strength.py tests/test_contract_strength.py
git commit -m "feat(contract): mutation-based strength scoring"
```

---

### Task 10: `jaunt reconcile` (deterministic core)

**Files:**
- Modify: `src/jaunt/cli.py` (add `cmd_reconcile`, parser, dispatch)
- Modify: `src/jaunt/contract/runner.py` (add `reconcile_entry`)
- Test: `tests/test_cli_reconcile.py`

**Interfaces:**
- Consumes: `extract_blocks_structured`, `derive_regions`, `evaluate_blocks` (derive.py); `contract_digests` (digest.py); `merge_battery` (battery.py); `compute_strength`/`format_strength` (strength.py); `battery_path` (runner.py).
- Produces:
  - `runner.ReconcileResult(spec_ref: str, ok: bool, strength: str, failures: list[str], battery_path: Path, wrote: bool)`
  - `runner.reconcile_entry(root: Path, cfg_contract, entry: SpecEntry, *, module_namespace: dict[str, object]) -> ReconcileResult` (deterministic; no model)
  - `cli.cmd_reconcile(args) -> int`

v1 behavior: derive structured blocks → evaluate against the live body. If the body **passes**, write/merge the battery (with refreshed digests + strength) and report in-sync. If it **fails**, surface the failing cases and **do not write** the battery; exit non-zero.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_reconcile.py
from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import parse_battery

GOOD = '''
import jaunt


@jaunt.contract
def shout(text: str) -> str:
    """
    Uppercase a non-empty string.

    Examples:
    - "hi" -> "HI"

    Raises:
    - "" raises ValueError
    """
    if not text:
        raise ValueError("empty")
    return text.upper()
'''

BAD = GOOD.replace("return text.upper()", "return text")  # violates the contract


def _project(tmp_path: Path, src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(src, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_reconcile_writes_battery_when_body_passes(tmp_path: Path) -> None:
    root = _project(tmp_path, GOOD)
    args = cli.parse_args(["reconcile", "--root", str(root)])
    assert cli.cmd_reconcile(args) == cli.EXIT_OK
    battery = root / "tests" / "contract" / "demo" / "test_shout.py"
    assert battery.is_file()
    parsed = parse_battery(battery.read_text(encoding="utf-8"))
    assert parsed.header is not None
    assert parsed.header["derived-from"] == "demo:shout"
    assert "test_examples" in parsed.regions["examples"]
    # A subsequent check passes.
    assert cli.cmd_check(cli.parse_args(["check", "--root", str(root)])) == cli.EXIT_OK


def test_reconcile_fails_and_does_not_write_when_body_violates_contract(tmp_path: Path) -> None:
    root = _project(tmp_path, BAD)
    args = cli.parse_args(["reconcile", "--root", str(root)])
    assert cli.cmd_reconcile(args) == cli.EXIT_PYTEST_FAILURE
    battery = root / "tests" / "contract" / "demo" / "test_shout.py"
    assert not battery.exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_reconcile.py -q`
Expected: FAIL — `AttributeError: module 'jaunt.cli' has no attribute 'cmd_reconcile'`.

- [ ] **Step 3: Add `reconcile_entry` to runner.py**

Append to `src/jaunt/contract/runner.py`:

```python
from dataclasses import dataclass as _dataclass  # (already imported above; reuse `dataclass`)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    spec_ref: str
    ok: bool
    strength: str
    failures: list[str]
    battery_path: Path
    wrote: bool


def reconcile_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    *,
    module_namespace: dict[str, object],
    tool_version: str,
) -> ReconcileResult:
    from jaunt.contract.derive import (
        derive_regions,
        evaluate_blocks,
        extract_blocks_structured,
    )
    from jaunt.contract.strength import compute_strength, format_strength
    from jaunt.digest import contract_digests, load_function_node

    spec_ref = str(entry.spec_ref)
    path = battery_path(root, battery_dir, entry)

    node = load_function_node(entry.source_file, entry.qualname)
    docstring = (node.body and _docstring_of(node)) or ""
    blocks = extract_blocks_structured(docstring)

    fn = module_namespace.get(entry.qualname)
    if not callable(fn):
        return ReconcileResult(spec_ref, False, "0/0", ["function not importable"], path, False)

    failures = evaluate_blocks(fn, blocks, module_namespace)
    if failures:
        return ReconcileResult(spec_ref, False, "0/0", failures, path, False)

    digs = contract_digests(entry.source_file, entry.qualname)
    strength = "0/0"
    if strength_enabled:
        import ast

        func_src = ast.unparse(node)
        killed, applicable = compute_strength(
            func_src, entry.qualname, blocks, module_namespace
        )
        strength = format_strength(killed, applicable)

    regions = derive_regions(blocks, func_name=entry.qualname, derive=derive)
    existing = path.read_text(encoding="utf-8") if path.is_file() else None
    from jaunt.contract.battery import merge_battery

    text = merge_battery(
        existing,
        import_module=entry.module,
        func_name=entry.qualname,
        regions=regions,
        header_fields={
            "derived_from": spec_ref,
            "prose_digest": digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": strength,
            "tool_version": tool_version,
        },
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return ReconcileResult(spec_ref, True, strength, [], path, True)


def _docstring_of(node) -> str:
    import ast

    return ast.get_docstring(node, clean=True) or ""
```

> Remove the unused `_dataclass` alias line; it is only a reminder that `dataclass` is already imported at the top of runner.py. Keep one `dataclass` import.

- [ ] **Step 4: Add `cmd_reconcile`, parser, dispatch**

In `src/jaunt/cli.py`, add the command body:

```python
def cmd_reconcile(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        import importlib

        from jaunt import __version__

        root, cfg = _load_config(args)
        from jaunt.contract import runner

        specs = _discover_contract_specs(root=root, cfg=cfg)
        target_mods = _iter_target_modules(getattr(args, "target", []) or [])

        results = []
        for entry in sorted(specs.values(), key=lambda e: str(e.spec_ref)):
            if target_mods and entry.module not in target_mods:
                continue
            module = importlib.import_module(entry.module)
            results.append(
                runner.reconcile_entry(
                    root,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    cfg.contract.strength,
                    entry,
                    module_namespace=vars(module),
                    tool_version=__version__,
                )
            )

        failed = [r for r in results if not r.ok]
        if json_mode:
            _emit_json(
                {
                    "command": "reconcile",
                    "ok": not failed,
                    "reconciled": [
                        {"ref": r.spec_ref, "strength": r.strength, "wrote": r.wrote}
                        for r in results
                        if r.ok
                    ],
                    "failed": [
                        {"ref": r.spec_ref, "failures": r.failures} for r in failed
                    ],
                }
            )
        else:
            for r in results:
                if r.ok:
                    print(f"[ok] {r.spec_ref}: in sync (strength {r.strength})")
                else:
                    print(f"[FAIL] {r.spec_ref}: body does not satisfy contract")
                    for f in r.failures:
                        print(f"    - {f}")
            print(f"Reconcile: {len(results) - len(failed)} ok, {len(failed)} failed.")

        return EXIT_PYTEST_FAILURE if failed else EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "reconcile", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
```

Register the parser (after `check_p`):

```python
    reconcile_p = subparsers.add_parser(
        "reconcile", help="Derive/refresh committed contract batteries (calls the model)."
    )
    _add_common_flags(reconcile_p)
```

Add to dispatch in `main`:

```python
    if args.command == "reconcile":
        return cmd_reconcile(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_reconcile.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Run full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/cli.py src/jaunt/contract/runner.py tests/test_cli_reconcile.py
git commit -m "feat(contract): jaunt reconcile (deterministic derivation + strength)"
```

---

### Task 11: `jaunt adopt` (on-ramp) + source-edit helpers (edits.py)

**Files:**
- Create: `src/jaunt/contract/edits.py`
- Modify: `src/jaunt/cli.py` (add `cmd_adopt`, parser, dispatch)
- Test: `tests/test_cli_adopt.py`

**Interfaces:**
- Produces:
  - `edits.add_contract_marker(source: str, func_name: str) -> str` (idempotent; inserts `@jaunt.contract` above the def and ensures `import jaunt`)
  - `edits.remove_contract_marker(source: str, func_name: str) -> str` (used by Task 13)
  - `cli.cmd_adopt(args) -> int`
- Consumes: `discovery.discover_module_files`, `runner.reconcile_entry`, `registry`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_adopt.py
from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.edits import add_contract_marker

PLAIN = '''\
def shout(text: str) -> str:
    """
    Uppercase a non-empty string.

    Examples:
    - "hi" -> "HI"
    """
    return text.upper()
'''

PLAIN_BAD = PLAIN.replace("return text.upper()", "return text")


def test_add_contract_marker_inserts_decorator_and_import() -> None:
    out = add_contract_marker(PLAIN, "shout")
    assert "import jaunt" in out
    assert "@jaunt.contract" in out
    # Idempotent.
    assert add_contract_marker(out, "shout").count("@jaunt.contract") == 1


def _project(tmp_path: Path, src: str) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(src, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_adopt_adds_marker_and_writes_battery(tmp_path: Path) -> None:
    root = _project(tmp_path, PLAIN)
    args = cli.parse_args(["adopt", "demo:shout", "--root", str(root)])
    assert cli.cmd_adopt(args) == cli.EXIT_OK
    src = (root / "src" / "demo.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" in src
    assert (root / "tests" / "contract" / "demo" / "test_shout.py").is_file()


def test_adopt_surfaces_body_contract_disagreement(tmp_path: Path) -> None:
    root = _project(tmp_path, PLAIN_BAD)
    args = cli.parse_args(["adopt", "demo:shout", "--root", str(root)])
    assert cli.cmd_adopt(args) == cli.EXIT_PYTEST_FAILURE
    # Battery is not written when the body disagrees with its own docstring.
    assert not (root / "tests" / "contract" / "demo" / "test_shout.py").exists()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_adopt.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.contract.edits'`.

- [ ] **Step 3: Implement the edit helpers**

Create `src/jaunt/contract/edits.py`:

```python
"""Pure source transforms for adopting/ejecting a contract marker."""

from __future__ import annotations

import ast


def _find_func(source: str, func_name: str) -> ast.FunctionDef:
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            return node
    raise ValueError(f"top-level function {func_name!r} not found")


def _ensure_import_jaunt(lines: list[str]) -> list[str]:
    for line in lines:
        stripped = line.strip()
        if stripped == "import jaunt" or stripped.startswith("import jaunt "):
            return lines
    # Insert after a leading `from __future__` block / module docstring, else at top.
    insert_at = 0
    for i, line in enumerate(lines):
        if line.startswith("from __future__"):
            insert_at = i + 1
    return lines[:insert_at] + ["import jaunt"] + lines[insert_at:]


def add_contract_marker(source: str, func_name: str) -> str:
    node = _find_func(source, func_name)

    # Already marked?
    for dec in node.decorator_list:
        text = ast.unparse(dec)
        if text in ("jaunt.contract", "jaunt.contract()") or text.endswith(".contract"):
            return source

    lines = source.splitlines()
    # Insert above the first decorator if present, else above `def`.
    anchor = node.decorator_list[0].lineno if node.decorator_list else node.lineno
    insert_idx = anchor - 1  # 1-based lineno -> 0-based index
    indent = lines[insert_idx][: len(lines[insert_idx]) - len(lines[insert_idx].lstrip())]
    lines.insert(insert_idx, f"{indent}@jaunt.contract")

    lines = _ensure_import_jaunt(lines)
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def remove_contract_marker(source: str, func_name: str) -> str:
    node = _find_func(source, func_name)
    targets = set()
    for dec in node.decorator_list:
        text = ast.unparse(dec)
        if text in ("jaunt.contract", "jaunt.contract()"):
            targets.add(dec.lineno)
    if not targets:
        return source
    lines = source.splitlines()
    kept = [line for i, line in enumerate(lines, start=1) if i not in targets]
    return "\n".join(kept) + ("\n" if source.endswith("\n") else "")
```

- [ ] **Step 4: Add `cmd_adopt`, parser, dispatch**

In `src/jaunt/cli.py`, add a helper to resolve a ref to its source file, then the command:

```python
def _resolve_contract_source_file(*, root: Path, cfg: JauntConfig, module: str) -> Path:
    from jaunt import discovery

    source_dirs = [root / sr for sr in cfg.paths.source_roots if (root / sr).exists()]
    found = discovery.discover_module_files(
        roots=source_dirs,
        exclude=[],
        generated_dir=cfg.paths.generated_dir,
        target_modules={module},
    )
    for mod, path in found:
        if mod == module:
            return path
    raise JauntDiscoveryError(f"Could not locate source module {module!r} under source_roots.")


def cmd_adopt(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        import importlib

        from jaunt import __version__, discovery, registry
        from jaunt.contract import runner
        from jaunt.contract.edits import add_contract_marker

        root, cfg = _load_config(args)
        ref = args.ref
        module, sep, func = ref.partition(":")
        if not sep:
            module, _, func = ref.rpartition(".")
        if not module or not func:
            raise JauntConfigError(f"adopt expects a 'module:func' ref, got {ref!r}.")

        src_path = _resolve_contract_source_file(root=root, cfg=cfg, module=module)
        source = src_path.read_text(encoding="utf-8")
        src_path.write_text(add_contract_marker(source, func), encoding="utf-8")

        # Re-import with the marker present and reconcile this one entry.
        specs = _discover_contract_specs(root=root, cfg=cfg)
        entry = next((e for e in specs.values() if e.module == module and e.qualname == func), None)
        if entry is None:
            raise JauntDiscoveryError(f"Adopted {ref!r} but could not re-discover it.")

        importlib.reload(importlib.import_module(module))
        mod = importlib.import_module(module)
        result = runner.reconcile_entry(
            root,
            cfg.contract.battery_dir,
            cfg.contract.derive,
            cfg.contract.strength,
            entry,
            module_namespace=vars(mod),
            tool_version=__version__,
        )

        if json_mode:
            _emit_json(
                {
                    "command": "adopt",
                    "ok": result.ok,
                    "ref": result.spec_ref,
                    "strength": result.strength,
                    "failures": result.failures,
                }
            )
        elif result.ok:
            print(f"Adopted {result.spec_ref} (strength {result.strength}).")
        else:
            print(f"Adopted {result.spec_ref} but the body disagrees with its docstring:")
            for f in result.failures:
                print(f"    - {f}")

        return EXIT_OK if result.ok else EXIT_PYTEST_FAILURE
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "adopt", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
```

Register the parser (after `reconcile_p`):

```python
    adopt_p = subparsers.add_parser("adopt", help="Add @jaunt.contract to a function and derive.")
    adopt_p.add_argument("ref", help="Spec ref 'module:func'.")
    _add_common_flags(adopt_p)
```

Add to dispatch in `main`:

```python
    if args.command == "adopt":
        return cmd_adopt(args)
```

> Note: `_add_common_flags` adds `--target`; `adopt` ignores it (it operates on the positional `ref`). That is acceptable — the shared flags keep `--root`/`--config`/`--json` consistent.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_adopt.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Run full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/edits.py src/jaunt/cli.py tests/test_cli_adopt.py
git commit -m "feat(contract): jaunt adopt (on-ramp) + marker source edits"
```

---

### Task 12: Model-backed derivation fallback (unstructured prose)

The deterministic extractor (Task 6) covers structured `Examples:`/`Raises:` blocks. This task adds the model path for prose without those blocks (and for `Raises:` clauses that state an exception but no triggering input). This is the **only** LLM call in Contract mode, gated inside `reconcile`. Projects whose prose is fully structured never construct a backend or need an API key.

**Files:**
- Modify: `src/jaunt/generate/base.py` (add `complete_text`)
- Modify: `src/jaunt/generate/openai_backend.py`, `anthropic_backend.py`, `cerebras_backend.py` (implement `complete_text`)
- Create: `src/jaunt/prompts/contract_derive_system.md`, `src/jaunt/prompts/contract_derive_user.md`
- Modify: `src/jaunt/contract/derive.py` (add `extract_blocks_via_model`)
- Modify: `src/jaunt/contract/runner.py` (`reconcile_entry` accepts optional `model_extract`)
- Modify: `src/jaunt/cli.py` (`cmd_reconcile` builds a lazy model extractor)
- Test: `tests/test_contract_derive_model.py`

**Interfaces:**
- Produces:
  - `GeneratorBackend.complete_text(*, system: str, user: str) -> str` (async; default raises `NotImplementedError`)
  - `derive.extract_blocks_via_model(prose: str, *, complete: Callable[[str, str], Awaitable[str]]) -> ContractBlocks`
  - `runner.reconcile_entry(..., model_extract: Callable[[str], ContractBlocks] | None = None)` — used only when `extract_blocks_structured` returns empty.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_derive_model.py
from __future__ import annotations

import asyncio
import json

from jaunt.contract.derive import ContractBlocks, ExampleRow, RaisesRow, extract_blocks_via_model

CANNED = json.dumps(
    {
        "examples": [{"input": '"hi"', "expected": '"HI"'}],
        "raises": [{"input": '""', "exc": "ValueError"}],
    }
)


def test_extract_blocks_via_model_parses_json() -> None:
    async def fake_complete(system: str, user: str) -> str:
        assert "contract" in system.lower()
        return CANNED

    blocks = asyncio.run(extract_blocks_via_model("Shout the text.", complete=fake_complete))
    assert blocks.examples == (ExampleRow('"hi"', '"HI"'),)
    assert blocks.raises == (RaisesRow('""', "ValueError"),)


def test_extract_blocks_via_model_tolerates_fenced_json() -> None:
    async def fake_complete(system: str, user: str) -> str:
        return f"```json\n{CANNED}\n```"

    blocks = asyncio.run(extract_blocks_via_model("x", complete=fake_complete))
    assert not blocks.is_empty()


def test_reconcile_entry_uses_model_when_unstructured(tmp_path) -> None:
    # Body satisfies the model-derived contract; reconcile should write the battery.
    from jaunt import registry
    from jaunt.contract import runner

    registry.clear_registries()
    src = (
        "import jaunt\n\n\n"
        "@jaunt.contract\n"
        "def shout(text):\n"
        '    "Shout the text. Empty input is an error."\n'
        "    if not text:\n"
        "        raise ValueError('empty')\n"
        "    return text.upper()\n"
    )
    mod_dir = tmp_path / "src"
    mod_dir.mkdir()
    (mod_dir / "demo.py").write_text(src, encoding="utf-8")

    import sys

    sys.path.insert(0, str(mod_dir))
    try:
        import importlib

        mod = importlib.import_module("demo")
        entry = next(iter(registry.get_contract_registry().values()))

        def model_extract(prose: str) -> ContractBlocks:
            return ContractBlocks(
                examples=(ExampleRow('"hi"', '"HI"'),),
                raises=(RaisesRow('""', "ValueError"),),
            )

        result = runner.reconcile_entry(
            tmp_path,
            "tests/contract",
            ["examples", "errors"],
            False,
            entry,
            module_namespace=vars(mod),
            tool_version="0.4.4",
            model_extract=model_extract,
        )
        assert result.ok
        assert (tmp_path / "tests" / "contract" / "demo" / "test_shout.py").is_file()
    finally:
        sys.path.remove(str(mod_dir))
        sys.modules.pop("demo", None)
        registry.clear_registries()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_derive_model.py -q`
Expected: FAIL — `ImportError: cannot import name 'extract_blocks_via_model'`.

- [ ] **Step 3: Add `complete_text` to the backend base + providers**

In `src/jaunt/generate/base.py`, add to `GeneratorBackend` (after `generate_interactive`):

```python
    async def complete_text(self, *, system: str, user: str) -> str:
        """Single-shot text completion for contract derivation.

        Default: unsupported. Providers override this. Used only by `jaunt reconcile`
        when docstring prose is unstructured.
        """
        raise NotImplementedError(
            "Contract derivation via model is not supported on this backend."
        )
```

In `src/jaunt/generate/openai_backend.py`, add to `OpenAIBackend`:

```python
    async def complete_text(self, *, system: str, user: str) -> str:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        content, _usage = await self._call_openai(messages, ctx=None)
        return content
```

In `src/jaunt/generate/anthropic_backend.py` and `src/jaunt/generate/cerebras_backend.py`, add the analogous method mirroring each backend's existing single-call helper (the method that `generate_module` uses to send `messages` and return text). Each builds the same `system`/`user` message pair and returns the text content. If a provider's helper bundles the system prompt separately (Anthropic takes `system=` as a top-level field), pass `system` there and `user` as the single user message.

- [ ] **Step 4: Add the prompt templates**

Create `src/jaunt/prompts/contract_derive_system.md`:

```text
You extract a testable contract from a Python function's docstring prose.

You DO NOT judge whether code is correct. You DO NOT invent behavior. You only
transcribe what the prose states into concrete, falsifiable cases.

Return STRICT JSON, no prose, with this shape:
{
  "examples": [{"input": "<python-expr>", "expected": "<python-expr>"}],
  "raises":   [{"input": "<python-expr>", "exc": "<ExceptionName>"}]
}

Rules:
- "input" is a single positional argument as a Python expression (e.g. "\"hi\"", "42").
- "expected" is the documented return value as a Python expression.
- For "raises", choose the simplest input the prose implies triggers that exception.
- Only include cases the prose actually states or directly implies. If none, return
  empty lists. Never guess outputs the prose does not give.
```

Create `src/jaunt/prompts/contract_derive_user.md`:

```text
Function name: {{func_name}}

Docstring prose:
{{prose}}

Extract the contract as STRICT JSON per the system instructions.
```

- [ ] **Step 5: Implement `extract_blocks_via_model` in derive.py**

Add to `src/jaunt/contract/derive.py`:

```python
import json
from collections.abc import Awaitable, Callable

from jaunt.generate.shared import strip_markdown_fences


async def extract_blocks_via_model(
    prose: str,
    *,
    complete: Callable[[str, str], Awaitable[str]],
    func_name: str = "f",
) -> ContractBlocks:
    from jaunt.generate.shared import load_prompt, render_template

    system = load_prompt("contract_derive_system.md", None)
    user = render_template(
        load_prompt("contract_derive_user.md", None),
        {"func_name": func_name, "prose": prose},
    )
    raw = await complete(system, user)
    payload = json.loads(strip_markdown_fences(raw))

    examples = tuple(
        ExampleRow(str(row["input"]), str(row["expected"]))
        for row in payload.get("examples", [])
        if "input" in row and "expected" in row
    )
    raises = tuple(
        RaisesRow(str(row["input"]), str(row["exc"]))
        for row in payload.get("raises", [])
        if "input" in row and "exc" in row
    )
    return ContractBlocks(examples=examples, raises=raises)
```

> Keep the top-of-file imports (`ast`, `builtins`, `re`, `dataclass`) and add `json` there too; the `Awaitable`/`Callable` import can live at module top with the others.

- [ ] **Step 6: Wire the optional model extractor into `reconcile_entry`**

In `src/jaunt/contract/runner.py`, change `reconcile_entry`'s signature to accept `model_extract` and use it when structured extraction is empty (replace the `blocks = extract_blocks_structured(docstring)` region):

```python
def reconcile_entry(
    root: Path,
    battery_dir: str,
    derive: list[str],
    strength_enabled: bool,
    entry: SpecEntry,
    *,
    module_namespace: dict[str, object],
    tool_version: str,
    model_extract: "Callable[[str], ContractBlocks] | None" = None,
) -> ReconcileResult:
    ...
    blocks = extract_blocks_structured(docstring)
    if blocks.is_empty() and model_extract is not None and docstring.strip():
        blocks = model_extract(docstring)
    ...
```

Add the needed import at the top of runner.py:

```python
from jaunt.contract.derive import ContractBlocks
```

- [ ] **Step 7: Build a lazy model extractor in `cmd_reconcile`**

In `src/jaunt/cli.py`, inside `cmd_reconcile`, construct a lazy extractor that only builds a backend on first use (so fully-structured projects need no key), and pass it to `reconcile_entry`:

```python
        import asyncio

        from jaunt.contract.derive import extract_blocks_via_model

        _backend_box: list[object] = []

        def _model_extract(prose: str):
            if not _backend_box:
                _backend_box.append(_build_backend(cfg))
            backend = _backend_box[0]
            return asyncio.run(
                extract_blocks_via_model(prose, complete=backend.complete_text)
            )
```

Pass `model_extract=_model_extract` into each `runner.reconcile_entry(...)` call.

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_derive_model.py tests/test_cli_reconcile.py -q`
Expected: PASS (5 passed — model parsing, fenced JSON, model-backed reconcile, plus the two deterministic reconcile tests still green).

- [ ] **Step 9: Run full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/generate/base.py src/jaunt/generate/openai_backend.py src/jaunt/generate/anthropic_backend.py src/jaunt/generate/cerebras_backend.py src/jaunt/prompts/contract_derive_system.md src/jaunt/prompts/contract_derive_user.md src/jaunt/contract/derive.py src/jaunt/contract/runner.py src/jaunt/cli.py tests/test_contract_derive_model.py
git commit -m "feat(contract): model-backed derivation fallback for unstructured prose"
```

**Milestone 2 complete:** `reconcile` derives committed batteries (deterministic for structured prose, model fallback otherwise); `adopt` is the on-ramp; `check` gates.

---

## Milestone 3 — Off-ramp, status, example, docs

### Task 13: `jaunt eject` (off-ramp)

**Files:**
- Modify: `src/jaunt/contract/battery.py` (add `de_jaunt_battery`)
- Modify: `src/jaunt/contract/strength.py` (add `parse_strength`, `EJECT_STRENGTH_WARN`)
- Modify: `src/jaunt/cli.py` (add `cmd_eject`, parser, dispatch)
- Test: `tests/test_cli_eject.py`

**Interfaces:**
- Produces:
  - `battery.de_jaunt_battery(source: str, *, provenance: str) -> str` (strips the contract header + derived markers, keeps test code, prepends one provenance comment)
  - `strength.parse_strength(text: str) -> tuple[int, int]`; `strength.EJECT_STRENGTH_WARN: float = 0.5`
  - `cli.cmd_eject(args) -> int`
- Consumes: `edits.remove_contract_marker`, `runner.battery_path`, `_discover_contract_specs`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_eject.py
from __future__ import annotations

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import de_jaunt_battery, parse_battery
from jaunt.contract.strength import parse_strength


def test_parse_strength() -> None:
    assert parse_strength("7/8") == (7, 8)
    assert parse_strength("0/0") == (0, 0)


def test_de_jaunt_removes_header_keeps_tests() -> None:
    src = (
        "# This file is maintained by jaunt (contract mode). Review like any test.\n"
        "# jaunt:contract=1\n# jaunt:derived-from=demo:shout\n"
        "# jaunt:prose-digest=sha256:aa\n# jaunt:signature=sha256:bb\n"
        "# jaunt:body-digest=sha256:cc\n# jaunt:strength=2/2\n# jaunt:tool_version=0.4.4\n"
        "import pytest\nfrom demo import shout\n\n"
        "# >>> jaunt:derived examples\n"
        "def test_examples():\n    assert shout('a') == 'A'\n"
        "# <<< jaunt:derived examples\n"
    )
    out = de_jaunt_battery(src, provenance="was demo:shout")
    assert "jaunt:contract" not in out
    assert ">>> jaunt:derived" not in out
    assert "def test_examples" in out
    assert "from demo import shout" in out
    assert out.lstrip().startswith("#")  # provenance comment
    assert parse_battery(out).header is None  # no longer a jaunt battery


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(
        "import jaunt\n\n\n"
        "@jaunt.contract\n"
        "def shout(text: str) -> str:\n"
        '    """Uppercase. Examples:\n    - "hi" -> "HI"\n    """\n'
        "    return text.upper()\n",
        encoding="utf-8",
    )
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_eject_removes_marker_and_dejaunts_battery(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(root)])) == cli.EXIT_OK
    assert cli.cmd_eject(cli.parse_args(["eject", "demo:shout", "--root", str(root)])) == cli.EXIT_OK

    src = (root / "src" / "demo.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" not in src
    battery = (root / "tests" / "contract" / "demo" / "test_shout.py").read_text(encoding="utf-8")
    assert "jaunt:contract" not in battery
    assert "def test_examples" in battery  # tests survive as plain pytest
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_eject.py -q`
Expected: FAIL — `ImportError: cannot import name 'de_jaunt_battery'`.

- [ ] **Step 3: Implement de-jaunt + strength parsing**

Add to `src/jaunt/contract/battery.py`:

```python
def de_jaunt_battery(source: str, *, provenance: str) -> str:
    """Turn a jaunt battery into a plain, hand-owned pytest module."""

    # Drop the contract header lines.
    lines = source.splitlines()
    body_lines: list[str] = []
    in_header = lines[:1] == [CONTRACT_BATTERY_MARKER]
    for line in lines:
        if in_header and (line == CONTRACT_BATTERY_MARKER or line.startswith("# jaunt:")):
            continue
        in_header = False
        # Drop derived-region markers but keep the code between them.
        if line.startswith("# >>> jaunt:derived ") or line.startswith("# <<< jaunt:derived "):
            continue
        body_lines.append(line)
    body = "\n".join(body_lines).strip()
    return f"# {provenance} (ejected from jaunt contract mode; now hand-owned).\n{body}\n"
```

Add to `src/jaunt/contract/strength.py`:

```python
EJECT_STRENGTH_WARN = 0.5


def parse_strength(text: str) -> tuple[int, int]:
    killed_s, _, applicable_s = text.partition("/")
    try:
        return (int(killed_s), int(applicable_s))
    except ValueError:
        return (0, 0)
```

- [ ] **Step 4: Add `cmd_eject`, parser, dispatch**

In `src/jaunt/cli.py`:

```python
def cmd_eject(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        from jaunt.contract import runner
        from jaunt.contract.battery import de_jaunt_battery, parse_battery
        from jaunt.contract.edits import remove_contract_marker
        from jaunt.contract.strength import EJECT_STRENGTH_WARN, parse_strength

        root, cfg = _load_config(args)
        specs = _discover_contract_specs(root=root, cfg=cfg)

        if getattr(args, "all", False):
            targets = list(specs.values())
        else:
            ref = args.ref
            module, sep, func = ref.partition(":")
            if not sep:
                module, _, func = ref.rpartition(".")
            targets = [
                e for e in specs.values() if e.module == module and e.qualname == func
            ]
            if not targets:
                raise JauntDiscoveryError(f"No contract function matches {ref!r}.")

        ejected: list[str] = []
        warnings: list[str] = []
        for entry in targets:
            path = runner.battery_path(root, cfg.contract.battery_dir, entry)
            if path.is_file():
                parsed = parse_battery(path.read_text(encoding="utf-8"))
                strength = (parsed.header or {}).get("strength", "0/0")
                killed, applicable = parse_strength(strength)
                if applicable == 0 or killed / applicable < EJECT_STRENGTH_WARN:
                    warnings.append(
                        f"{entry.spec_ref}: weak contract (strength {strength}); "
                        "freezing weak tests."
                    )
                path.write_text(
                    de_jaunt_battery(
                        path.read_text(encoding="utf-8"),
                        provenance=f"was {entry.spec_ref}",
                    ),
                    encoding="utf-8",
                )
            src = Path(entry.source_file).read_text(encoding="utf-8")
            Path(entry.source_file).write_text(
                remove_contract_marker(src, entry.qualname), encoding="utf-8"
            )
            ejected.append(str(entry.spec_ref))

        if json_mode:
            _emit_json(
                {"command": "eject", "ok": True, "ejected": ejected, "warnings": warnings}
            )
        else:
            for w in warnings:
                print(f"warning: {w}")
            for ref in ejected:
                print(f"Ejected {ref} -> plain Python + plain pytest.")
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError, JauntDependencyCycleError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "eject", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
```

Register the parser (after `adopt_p`):

```python
    eject_p = subparsers.add_parser("eject", help="Remove contract tracking; leave plain pytest.")
    eject_p.add_argument("ref", nargs="?", default=None, help="Spec ref 'module:func'.")
    eject_p.add_argument("--all", action="store_true", help="Eject all contract functions.")
    _add_common_flags(eject_p)
```

Add to dispatch in `main`:

```python
    if args.command == "eject":
        return cmd_eject(args)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_eject.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Run full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/contract/battery.py src/jaunt/contract/strength.py src/jaunt/cli.py tests/test_cli_eject.py
git commit -m "feat(contract): jaunt eject (off-ramp) -> plain Python + plain pytest"
```

---

### Task 14: Extend `jaunt status` with contract state + cascade

**Files:**
- Modify: `src/jaunt/cli.py` (`cmd_status`)
- Test: `tests/test_cli_status_contract.py`

**Interfaces:**
- Consumes: `_discover_contract_specs`, `runner.evaluate_entry`/`run_battery_file`, `DriftState`, `build_spec_graph` (deps.py).
- Produces: extended `cmd_status` text output (a "Contracts" section) and `--json` (a `"contracts"` array of `{ref, state, strength, review}` plus `"contract_review"` refs). Magic-mode output is unchanged.

Cascade (v1, flag-only): a contract whose state is `STALE_PROSE` flags its **dependents** as `review`. Build the dependency graph over the contract registry; reverse it; mark dependents.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cli_status_contract.py
from __future__ import annotations

import json
from pathlib import Path

from jaunt import cli

SRC = (
    "import jaunt\n\n\n"
    "@jaunt.contract\n"
    "def shout(text: str) -> str:\n"
    '    """Uppercase. Examples:\n    - "hi" -> "HI"\n    """\n'
    "    return text.upper()\n"
)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_status_json_includes_contracts(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(root)])) == cli.EXIT_OK
    capsys.readouterr()
    rc = cli.cmd_status(cli.parse_args(["status", "--root", str(root), "--json"]))
    assert rc == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    contracts = {c["ref"]: c for c in data["contracts"]}
    assert "demo:shout" in contracts
    assert contracts["demo:shout"]["state"] == "in-sync"
    assert "/" in contracts["demo:shout"]["strength"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_status_contract.py -q`
Expected: FAIL — `KeyError: 'contracts'`.

- [ ] **Step 3: Compute contract statuses in `cmd_status`**

In `src/jaunt/cli.py`, inside `cmd_status`, after the magic registry is populated by `import_and_collect` and before the JSON/text emit, read the contract registry that the same import already populated, and compute statuses. Add this block just before the `if json_mode:` emit (so both modes can include it):

```python
        from jaunt.contract import runner as contract_runner
        from jaunt.contract.drift import DriftState
        from jaunt.deps import build_spec_graph as _build_contract_graph

        contract_specs = dict(registry.get_contract_registry())
        contract_rows: list[dict[str, object]] = []
        review_refs: set[str] = set()
        if contract_specs:
            def _run_battery(path: Path) -> bool:
                return contract_runner.run_battery_file(
                    path, root=root, source_roots=cfg.paths.source_roots
                )

            statuses = {
                str(e.spec_ref): contract_runner.evaluate_entry(
                    root,
                    cfg.contract.battery_dir,
                    cfg.contract.derive,
                    e,
                    run_battery=_run_battery,
                )
                for e in contract_specs.values()
            }

            # Cascade: prose-stale contracts flag their dependents `review`.
            cgraph = build_spec_graph(contract_specs, infer_default=infer_default)
            stale_prose = {
                ref for ref, st in statuses.items() if st.state is DriftState.STALE_PROSE
            }
            for ref, deps in cgraph.items():
                if any(str(d) in stale_prose for d in deps):
                    review_refs.add(str(ref))

            for ref in sorted(statuses):
                st = statuses[ref]
                contract_rows.append(
                    {
                        "ref": ref,
                        "state": st.state.value,
                        "strength": st.strength or "0/0",
                        "review": ref in review_refs,
                    }
                )
```

> Note: `infer_default` is already computed above in `cmd_status` for the magic graph; reuse it. If the early-return "no magic specs" branch fires before this block, move the contract computation above that branch (or guard the early return when contracts exist) so a contract-only project still reports. Concretely: in the `if not specs:` branch, also build `contract_rows` before returning.

Add `contract_rows`/`review` to both emit paths:

```python
        if json_mode:
            _emit_json(
                {
                    "command": "status",
                    "ok": True,
                    "stale": sorted(stale),
                    "fresh": sorted(fresh),
                    "contracts": contract_rows,
                    "contract_review": sorted(review_refs),
                }
            )
        else:
            ...  # existing magic output unchanged
            if contract_rows:
                print(f"Contracts ({len(contract_rows)}):")
                for row in contract_rows:
                    flag = " [review]" if row["review"] else ""
                    print(f"- {row['ref']}: {row['state']} (strength {row['strength']}){flag}")
```

For the contract-only project (the `if not specs:` early return), build `contract_rows` there too before returning `EXIT_OK`, using the same block, so `jaunt status` works with zero magic specs.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_status_contract.py tests/test_cli_status.py -q`
Expected: PASS (existing magic status tests stay green; the new contract test passes).

- [ ] **Step 5: Run full suite, lint, typecheck, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add src/jaunt/cli.py tests/test_cli_status_contract.py
git commit -m "feat(contract): status reports contract state, strength, and review cascade"
```

---

### Task 15: Runnable example + end-to-end test

**Files:**
- Create: `examples/contract_slugify/jaunt.toml`
- Create: `examples/contract_slugify/src/contract_slugify/__init__.py`
- Create: `examples/contract_slugify/src/contract_slugify/specs.py`
- Create: `examples/contract_slugify/README.md`
- Modify: `src/jaunt/contract/runner.py` (`run_battery_file` treats "no tests collected" as pass)
- Test: `tests/test_contract_example.py`

**Interfaces:**
- Consumes: `cli.cmd_reconcile`, `cli.cmd_check`, `battery.parse_battery`, `strength.parse_strength`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_contract_example.py
from __future__ import annotations

import shutil
from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import parse_battery
from jaunt.contract.strength import parse_strength

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "contract_slugify"


def test_example_reconciles_and_checks(tmp_path: Path) -> None:
    proj = tmp_path / "contract_slugify"
    shutil.copytree(EXAMPLE, proj)

    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(proj)])) == cli.EXIT_OK
    assert cli.cmd_check(cli.parse_args(["check", "--root", str(proj)])) == cli.EXIT_OK

    base = proj / "tests" / "contract" / "contract_slugify" / "specs"
    strong = parse_battery((base / "test_slugify.py").read_text(encoding="utf-8")).header
    weak = parse_battery((base / "test_describe.py").read_text(encoding="utf-8")).header

    sk, sn = parse_strength(strong["strength"])
    wk, wn = parse_strength(weak["strength"])
    # The strong contract pins its body better than the deliberately weak one.
    assert sn > 0 and (sk / sn) > (wk / wn if wn else 1.0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_contract_example.py -q`
Expected: FAIL — `FileNotFoundError` (example does not exist yet).

- [ ] **Step 3: Make `run_battery_file` tolerate case-less batteries**

A weak contract may derive zero cases; pytest exits `5` ("no tests collected"), which is not a behavior failure. In `src/jaunt/contract/runner.py`, change the return of `run_battery_file`:

```python
    # 0 = all passed; 5 = no tests collected (a case-less weak battery is not a failure).
    return proc.returncode in (0, 5)
```

- [ ] **Step 4: Create the example project**

`examples/contract_slugify/jaunt.toml`:

```toml
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]

[llm]
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"

[contract]
battery_dir = "tests/contract"
derive = ["examples", "errors"]
strength = true
```

`examples/contract_slugify/src/contract_slugify/__init__.py`:

```python
```

(empty file)

`examples/contract_slugify/src/contract_slugify/specs.py`:

```python
"""Contract-mode example: committed code is the source of truth."""

from __future__ import annotations

import re

import jaunt

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


@jaunt.contract
def slugify(title: str) -> str:
    """
    Convert a human title into a URL-safe slug.

    Examples:
    - "  Hello, World!  " -> "hello-world"
    - "C++ > Java" -> "c-java"
    - "already-slug" -> "already-slug"

    Raises:
    - "" raises ValueError
    - "!!!" raises ValueError
    """
    cleaned = _NON_ALNUM.sub("-", title.strip().lower()).strip("-")
    if not cleaned:
        raise ValueError("title is empty after cleaning")
    return cleaned


@jaunt.contract
def describe(n: int) -> str:
    """
    Loosely describe a number. (Deliberately weak contract: one example only,
    so its strength score is low — the < 0 branch is unpinned.)

    Examples:
    - 0 -> "zero"
    """
    if n == 0:
        return "zero"
    if n < 0:
        return "negative"
    return "positive"
```

`examples/contract_slugify/README.md`:

```markdown
# Contract mode example

The committed code is the source of truth; the docstring is the contract.
Jaunt derives a committed pytest battery instead of generating the body.

```bash
# Derive/refresh the committed batteries (deterministic here — structured prose,
# no API key needed):
jaunt reconcile

# Gate on them (deterministic, offline, no API key):
jaunt check

# See per-function drift state + strength score:
jaunt status
```

`slugify` has a strong contract (five pinned cases → high strength). `describe`
is deliberately weak (one example → low strength); `jaunt status` shows the gap,
and `jaunt eject contract_slugify.specs:describe` would freeze its tests as plain
pytest (with a low-strength warning).
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_contract_example.py -q`
Expected: PASS (1 passed).

- [ ] **Step 6: Run full suite, lint, commit**

```bash
uv run pytest -q && uv run ruff check --fix . && uv run ruff format . && uv run ty check
git add examples/contract_slugify src/jaunt/contract/runner.py tests/test_contract_example.py
git commit -m "feat(contract): runnable contract-mode example + end-to-end test"
```

---

### Task 16: Documentation

**Files:**
- Modify: `CLAUDE.md` (project root — the Jaunt dev guide)
- Modify: `README.md`
- Modify: `.claude/skills/jaunt/SKILL.md`

**Interfaces:** none (docs only). No code; no test step beyond a docs-link sanity check.

- [ ] **Step 1: Document Contract mode in `CLAUDE.md`**

Add a "Contract mode" subsection under **Key Concepts** describing: committed code is canonical; `@jaunt.contract` is a runtime no-op; the docstring is the contract; Jaunt derives a committed pytest battery in `tests/contract/`; `reconcile` is the only model-calling command; `check` is the deterministic CI gate (no API key). Add the new commands to the **CLI Commands** list:

```markdown
jaunt adopt <module:func>     # Add @jaunt.contract to existing code and derive its battery
jaunt reconcile               # Derive/refresh committed contract batteries (calls the model)
jaunt check                   # Verify committed batteries deterministically (CI gate, no model)
jaunt eject <module:func>     # Remove contract tracking; leave plain Python + plain pytest
```

Note the exit code: `jaunt check` returns `4` on any blocking drift state (unbuilt / stale-prose / signature-drift / behavior-drift).

- [ ] **Step 2: Document Contract mode in `README.md`**

Add a short "Two modes" section contrasting Magic mode (docstring canonical → generated impl in `__generated__/`) and Contract mode (committed code canonical → derived battery in `tests/contract/`), and point to `examples/contract_slugify/`. Use the coexistence framing: both are first-class and selected by decorator.

- [ ] **Step 3: Document Contract mode in the jaunt skill**

In `.claude/skills/jaunt/SKILL.md`, add a "Contract mode" section after the Magic-mode core workflow: when to use it (existing/hand-written code you want pinned by a docstring contract without surrendering the body), the `@jaunt.contract` decorator, the `adopt`/`reconcile`/`check`/`eject` loop, and the rule: **`check` never calls the model; only `reconcile` does.** Add the four commands to the CLI command table. Note the v1 limit: top-level sync functions only.

- [ ] **Step 4: Sanity-check and commit**

```bash
uv run ruff format .  # no-op for markdown, but keep the habit
git add CLAUDE.md README.md .claude/skills/jaunt/SKILL.md
git commit -m "docs(contract): document Contract mode and its relationship to Magic mode"
```

**Milestone 3 complete:** full lifecycle (adopt → reconcile → check → eject), status reporting, a runnable example, and docs.

---

## Self-Review

**1. Spec coverage** — every section of `2026-06-23-contract-mode-design.md` maps to a task:

| Spec section | Task(s) |
|---|---|
| §1 `@jaunt.contract` no-op marker | 1 |
| §2 Coexistence (decorator-keyed, shared infra) | 1, 8, 14 |
| §3 Prose → derived battery (examples + errors) | 6, 12 |
| §4 Committed battery artifact + header | 4, 5 |
| §5 Drift state machine | 7, 8 |
| §6.1 adopt | 11 |
| §6.2 reconcile (only model-calling command) | 10, 12 |
| §6.3 check | 8 |
| §6.4 status (+ cascade) | 14 |
| §6.5 eject | 13 |
| §7 Strength (mutation scoring) | 9, wired in 10 |
| §8 Dependency graph & cascade (flag-only) | 14 |
| §9 Configuration `[contract]` | 2 |
| §10 Deliverables (file-by-file) | all |
| §11 Testing plan | every task's tests |
| §12 Risks (human-reviewed batteries, advisory strength, preserve hand-edits) | 5 (merge/preserve), 9, 13 |

**2. Placeholder scan** — no "TBD"/"add error handling"/"similar to Task N" left. Two intentional cleanup notes are flagged inline (the stray `a = ...` line in Task 3's test; the `_dataclass` reminder alias in Task 10) and must be deleted by the implementer as instructed.

**3. Type consistency** — names checked across tasks: `SpecEntry.kind` Literal gains `"contract"` (Task 1) and is used in `get_specs_by_module`/`import_and_collect` (Tasks 1, 8); `ContractBlocks`/`ExampleRow`/`RaisesRow` defined in Task 6 and consumed unchanged in Tasks 9, 10, 12; `DerivedRegion` defined in Task 5, produced in Task 6; `ContractDigests(prose, signature, body)` from Task 3 used in Tasks 8, 10; `battery_path`/`ContractStatus`/`evaluate_entry`/`reconcile_entry`/`ReconcileResult` all in `runner.py` (Tasks 8, 10, 12); `DriftState` members consistent (Tasks 7, 8, 14); `format_contract_battery_header` keyword args match between Task 4 and the `header_fields` dict assembled in Tasks 5/10.

**Known v1 limitations (documented, not gaps):** single-positional-argument example rows; per-function pytest subprocess in `check`/`status` (no batching); no per-mutant timeout; class/method/async contracts and `reconcile --regen-body`/`jaunt doc` are out of scope per the spec's Non-goals.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-23-contract-mode.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
