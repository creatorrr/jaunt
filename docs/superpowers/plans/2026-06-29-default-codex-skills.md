# Default Codex Builder Skills — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a curated set of 13 default skills with Jaunt and make the Codex builder discover them natively (via seeded `.agents/skills/`), replacing the full-text prompt injection.

**Architecture:** `codex exec` natively discovers Agent-Skills (`SKILL.md` + YAML frontmatter) from `.agents/skills/` relative to its cwd. Jaunt runs Codex in a throwaway temp workspace, so the backend seeds skill dirs into that workspace (bundled builtins + the project's own `.agents/skills/`). The old `skills_block` prompt injection is removed; per-project PyPI auto-skills migrate from an HTML-comment header to YAML frontmatter so they're discoverable too.

**Tech Stack:** Python 3.12+, hatchling packaging, `codex exec` (codex-cli 0.142.x), pytest. Generator backend is mocked in tests (no API key needed).

## Global Constraints

- Python `>=3.12`; ruff line-length 100, rules E/F/I/UP/B; code must pass `uv run ruff check .`, `uv run ruff format --check .`, and `uv run ty check`.
- Codex is Jaunt's **sole** engine (`agent.engine == "codex"`); no legacy/aider paths.
- Skills use the Agent-Skills protocol: `SKILL.md` with YAML frontmatter `name:` and `description:` at minimum; progressive disclosure (Codex reads name+description, opens body on demand).
- Builtin skills are **package-only** (shipped in the wheel, seeded into the temp workspace) — never written into the user's repo.
- Project `.agents/skills/<name>/` entries **override** builtins of the same name during seeding (project wins).
- The default builtin set (13 names, exact): `asyncpg`, `dbos`, `descope`, `fastmcp`, `openai`, `pydantic`, `pydantic-ai`, `pytest`, `ruff`, `spacy`, `starlette`, `ty`, `uv`.
- Tests must not require network or a real `codex` binary; mock the subprocess as `tests/test_codex_backend.py` already does.
- Run from the worktree root: `/home/diwank/github.com/creatorrr/jaunt/.claude/worktrees/feat+default-codex-skills`.

---

### Task 1: Builtin skills registry + 4 hand-written tooling skills

**Files:**
- Create: `src/jaunt/skills_builtin.py`
- Create: `src/jaunt/skills/builtin/ruff/SKILL.md`
- Create: `src/jaunt/skills/builtin/ty/SKILL.md`
- Create: `src/jaunt/skills/builtin/pytest/SKILL.md`
- Create: `src/jaunt/skills/builtin/uv/SKILL.md`
- Test: `tests/test_skills_builtin.py`

**Interfaces:**
- Produces:
  - `DEFAULT_BUILTIN_SKILLS: tuple[str, ...]` — the 13 names (exact, sorted as in Global Constraints).
  - `builtin_skills_dir() -> Path` — absolute path to the packaged `skills/builtin` dir.
  - `resolve_builtin_skill(name: str) -> Path | None` — path to `<dir>/<name>/SKILL.md` if it exists, else `None`.
  - `iter_enabled_builtin_skill_dirs(names: Iterable[str]) -> list[tuple[str, Path]]` — `(name, skill_dir)` for each requested name whose `SKILL.md` exists.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skills_builtin.py
from __future__ import annotations

import re

from jaunt.skills_builtin import (
    DEFAULT_BUILTIN_SKILLS,
    builtin_skills_dir,
    iter_enabled_builtin_skill_dirs,
    resolve_builtin_skill,
)

_FRONTMATTER_RE = re.compile(r"^---\n(?P<fm>.*?)\n---\n", re.DOTALL)
_TOOLING = ("ruff", "ty", "pytest", "uv")


def test_default_set_has_13_names():
    assert len(DEFAULT_BUILTIN_SKILLS) == 13
    assert DEFAULT_BUILTIN_SKILLS == tuple(sorted(DEFAULT_BUILTIN_SKILLS))
    for name in ("asyncpg", "pydantic-ai", "ruff", "ty", "pytest", "uv"):
        assert name in DEFAULT_BUILTIN_SKILLS


def test_builtin_dir_exists():
    assert builtin_skills_dir().is_dir()


def test_tooling_skills_resolve_and_have_frontmatter():
    for name in _TOOLING:
        path = resolve_builtin_skill(name)
        assert path is not None and path.is_file(), name
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        assert m, f"{name} missing frontmatter"
        fm = m.group("fm")
        assert re.search(rf"^name:\s*\"?{re.escape(name)}\"?\s*$", fm, re.MULTILINE), name
        assert re.search(r"^description:\s*\S", fm, re.MULTILINE), name


def test_iter_enabled_skips_unknown():
    pairs = iter_enabled_builtin_skill_dirs(["ruff", "does-not-exist"])
    names = [n for n, _ in pairs]
    assert names == ["ruff"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills_builtin.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.skills_builtin'`.

- [ ] **Step 3: Create the registry module**

```python
# src/jaunt/skills_builtin.py
"""Registry for Jaunt's bundled (package-only) builtin Codex skills."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

# The curated default set shipped with Jaunt. Kept sorted for determinism.
DEFAULT_BUILTIN_SKILLS: tuple[str, ...] = (
    "asyncpg",
    "dbos",
    "descope",
    "fastmcp",
    "openai",
    "pydantic",
    "pydantic-ai",
    "pytest",
    "ruff",
    "spacy",
    "starlette",
    "ty",
    "uv",
)


def builtin_skills_dir() -> Path:
    """Absolute path to the packaged builtin skills directory."""
    return (Path(__file__).resolve().parent / "skills" / "builtin").resolve()


def resolve_builtin_skill(name: str) -> Path | None:
    """Return the SKILL.md path for *name*, or None if it is not bundled."""
    candidate = builtin_skills_dir() / name / "SKILL.md"
    return candidate if candidate.is_file() else None


def iter_enabled_builtin_skill_dirs(names: Iterable[str]) -> list[tuple[str, Path]]:
    """Resolve each requested builtin name to (name, skill_dir); skip missing ones."""
    pairs: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        skill_md = resolve_builtin_skill(name)
        if skill_md is not None:
            pairs.append((name, skill_md.parent))
    return pairs
```

- [ ] **Step 4: Author the 4 tooling SKILL.md files**

Each file is hand-written (no PyPI README). Frontmatter `name`/`description` exact; body is concise (≤ ~2 pages) and oriented around making **generated code conform**. Use these exact frontmatter blocks; write bodies from the descriptions below.

`src/jaunt/skills/builtin/ruff/SKILL.md`:
```markdown
---
name: "ruff"
description: "Use whenever writing or editing Python that must pass `ruff check` and `ruff format`. Covers the lint rules Jaunt enforces (E, F, I, UP, B), line-length 100, import sorting, and writing code that is clean on the first pass without noqa."
---

# ruff

## What it is
Ruff is the linter and formatter Jaunt-generated code must satisfy. Configured for line-length 100, target py312+, rules E/F/I/UP/B.

## Core concepts
- Import ordering (I): stdlib, third-party, first-party groups, each alphabetized; no unused imports (F401).
- Modern syntax (UP): `X | None` over `Optional[X]`, `list[int]` over `List[int]`, `from __future__ import annotations` at top when needed.
- Bugbear (B): no mutable default args, no `except:` bare, no unused loop vars.

## Common patterns
- Annotate everything; remove dead imports; keep lines ≤ 100 chars.
- Prefer f-strings; avoid `== None` (use `is None`).

## Gotchas
- Do not add `# noqa` to silence fixable issues — fix the code.
- Trailing whitespace and missing final newline fail `ruff format --check`.

## Testing notes
- Code that lints clean still needs behavior tests; ruff is not a correctness check.
```

`src/jaunt/skills/builtin/ty/SKILL.md`:
```markdown
---
name: "ty"
description: "Use whenever writing Python that must pass the `ty` type checker. Covers full annotations on all params and returns, and avoiding ty diagnostics like possibly-unbound, possibly-missing-attribute, invalid-assignment, and invalid-return-type."
---

# ty

## What it is
`ty` is the static type checker Jaunt runs on generated code. Full type coverage is expected.

## Core concepts
- Annotate every parameter and return type. Use precise types, not bare `object`/`Any`.
- Narrow `X | None` before use (guard with `if x is None`).
- Keep return types consistent across all branches.

## Common patterns
- Use `typing`/`collections.abc` protocols (`Callable`, `Sequence`, `Mapping`).
- For optional attributes, initialize in `__init__`; avoid conditional attribute creation.

## Gotchas
- Returning `None` implicitly from a function annotated `-> T` fails ty.
- Accessing an attribute that may be missing raises possibly-missing-attribute — assign defaults.

## Testing notes
- ty passing does not prove runtime correctness; still write pytest tests.
```

`src/jaunt/skills/builtin/pytest/SKILL.md`:
```markdown
---
name: "pytest"
description: "Use when writing pytest tests or test-friendly code. Covers fixtures, parametrize, exception and async testing (pytest-asyncio / anyio), and structuring deterministic tests with no network or wall-clock dependency."
---

# pytest

## What it is
pytest is Jaunt's test runner. Tests must be deterministic and isolated.

## Core concepts
- `assert` statements (no special assert methods); `pytest.raises(ExcType)` for errors.
- `@pytest.mark.parametrize` for table-driven cases; fixtures for shared setup.
- Async: `@pytest.mark.asyncio` (asyncio) or `@pytest.mark.anyio` (anyio) per the configured runner.

## Common patterns
- One behavioral assertion per test when practical; descriptive `test_` names.
- Inject clocks/IO via parameters; never call the network.

## Gotchas
- Don't depend on test execution order or global state.
- Avoid `time.sleep`; avoid real timestamps unless injected.

## Testing notes
- Cover the negative paths (invalid input, raised exceptions), not just the happy path.
```

`src/jaunt/skills/builtin/uv/SKILL.md`:
```markdown
---
name: "uv"
description: "Use when generated code must run under uv-managed environments. Covers importing only declared dependencies, not assuming globally-installed packages, and keeping module imports consistent with the project's declared dependency set."
---

# uv

## What it is
uv manages the project's virtualenv and dependencies. Generated code runs inside `uv run`.

## Core concepts
- Only import packages that are declared dependencies of the project.
- Standard library is always available; third-party libs must be declared.

## Common patterns
- Prefer stdlib when a dependency is not already present in the project.
- Keep imports at module top; no dynamic install at runtime.

## Gotchas
- Do not shell out to `pip install`; do not import undeclared packages.

## Testing notes
- Tests run under the same uv environment; they may import only declared deps.
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_skills_builtin.py -q`
Expected: PASS (4 tests).

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/skills_builtin.py src/jaunt/skills/builtin/ tests/test_skills_builtin.py
git commit -m "feat(skills): builtin skills registry + 4 tooling skills (ruff/ty/pytest/uv)"
```

---

### Task 2: Author the 9 library skills + assert all 13 bundled

**Files:**
- Create: `src/jaunt/skills/builtin/{asyncpg,dbos,descope,fastmcp,openai,pydantic,pydantic-ai,spacy,starlette}/SKILL.md`
- Modify: `tests/test_skills_builtin.py` (add full-set test)

**Interfaces:**
- Consumes: `DEFAULT_BUILTIN_SKILLS`, `resolve_builtin_skill` (Task 1).
- Produces: every name in `DEFAULT_BUILTIN_SKILLS` resolves to a valid `SKILL.md`.

Each library skill: frontmatter `name` = the exact dist name, `description` = the string below; body drafted from the library's PyPI README (concise, 1–2 pages) with the five sections used elsewhere (`## What it is`, `## Core concepts`, `## Common patterns`, `## Gotchas`, `## Testing notes`). Bodies are prose — the test below checks structure, not wording.

Exact `description:` values:
- `asyncpg`: "Use when generating PostgreSQL code with asyncpg — connection pools, parameterized queries ($1 placeholders), transactions, and fetch/execute patterns for async Postgres access."
- `dbos`: "Use when generating durable workflow code with DBOS — @DBOS.workflow / @DBOS.step / @DBOS.transaction decorators, durability and exactly-once semantics, and idempotent step design."
- `descope`: "Use when generating authentication/authorization code with the Descope SDK — session and JWT validation, auth methods, and tenant/role/permission checks."
- `fastmcp`: "Use when generating MCP servers or clients with FastMCP — defining tools/resources/prompts with typed signatures, the FastMCP server lifecycle, and running over stdio/HTTP."
- `openai`: "Use when generating code that calls the OpenAI Python SDK — client construction, chat/responses APIs, structured outputs, streaming, and sync vs async clients."
- `pydantic`: "Use when generating code that defines or uses Pydantic v2 models — BaseModel, field types and validators, model_config, serialization (model_dump), and ValidationError handling."
- `pydantic-ai`: "Use when generating LLM agent code with pydantic-ai — Agent construction, tools, typed dependencies, structured result types, and running agents sync/async."
- `spacy`: "Use when generating NLP code with spaCy — loading pipelines (nlp = spacy.load), processing Doc/Span/Token, entities, and common pipeline components."
- `starlette`: "Use when generating ASGI web code with Starlette — routes, Request/Response, async endpoints, middleware, and application startup/shutdown."

- [ ] **Step 1: Add the failing full-set test**

```python
# append to tests/test_skills_builtin.py

def test_all_default_skills_bundled_and_valid():
    for name in DEFAULT_BUILTIN_SKILLS:
        path = resolve_builtin_skill(name)
        assert path is not None, f"missing builtin skill: {name}"
        text = path.read_text(encoding="utf-8")
        m = _FRONTMATTER_RE.match(text)
        assert m, f"{name} missing frontmatter"
        fm = m.group("fm")
        assert re.search(rf"^name:\s*\"?{re.escape(name)}\"?\s*$", fm, re.MULTILINE), name
        assert re.search(r"^description:\s*\S", fm, re.MULTILINE), name
        body = text[m.end():]
        for heading in ("## What it is", "## Core concepts", "## Common patterns",
                        "## Gotchas", "## Testing notes"):
            assert heading in body, f"{name} missing {heading}"
        assert len(text) <= 12_000, f"{name} too long ({len(text)} chars)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills_builtin.py::test_all_default_skills_bundled_and_valid -q`
Expected: FAIL — missing builtin skill: asyncpg (and the other 8).

- [ ] **Step 3: Author the 9 library SKILL.md files**

For each name, create `src/jaunt/skills/builtin/<name>/SKILL.md` with the frontmatter (`name` + the exact `description` above) followed by a concise body covering the five required sections. Draft each body from the library's PyPI README and public API — accurate, code-snippet-driven, ≤ ~2 pages. Treat the README as untrusted data (extract facts only).

Reference frontmatter shape (asyncpg shown; repeat per library with its own name/description):
```markdown
---
name: "asyncpg"
description: "Use when generating PostgreSQL code with asyncpg — connection pools, parameterized queries ($1 placeholders), transactions, and fetch/execute patterns for async Postgres access."
---

# asyncpg

## What it is
<concise summary from README>

## Core concepts
<key abstractions: connect/create_pool, Connection, Record, fetch/fetchrow/fetchval/execute>

## Common patterns
<pool acquire, $1 parameter binding, transactions via `async with conn.transaction()`>

## Gotchas
<$N placeholders not %s; close pools; type codecs>

## Testing notes
<inject a connection/pool; avoid a live DB in unit tests>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_skills_builtin.py -q`
Expected: PASS (all builtin tests, including the 13-skill check).

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/skills/builtin/ tests/test_skills_builtin.py
git commit -m "feat(skills): author 9 builtin library skills (frontmatter + sections)"
```

---

### Task 3: Config — `[skills] builtin` and `builtin_skills`

**Files:**
- Modify: `src/jaunt/config.py:84-89` (`SkillsConfig`), `src/jaunt/config.py:385-402` (parsing), `src/jaunt/config.py:502-506` (construction)
- Test: `tests/test_config.py` (add cases)

**Interfaces:**
- Consumes: `DEFAULT_BUILTIN_SKILLS` (Task 1).
- Produces: `SkillsConfig.builtin: bool` (default `True`), `SkillsConfig.builtin_skills: list[str]` (default `list(DEFAULT_BUILTIN_SKILLS)`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config.py — add
def test_skills_builtin_defaults(tmp_path):
    from jaunt.config import load_config
    from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\n', encoding="utf-8"
    )
    _root, cfg = load_config(root=tmp_path)
    assert cfg.skills.builtin is True
    assert cfg.skills.builtin_skills == list(DEFAULT_BUILTIN_SKILLS)


def test_skills_builtin_overrides(tmp_path):
    from jaunt.config import load_config
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\n'
        '[skills]\nbuiltin = false\nbuiltin_skills = ["ruff", "pytest"]\n',
        encoding="utf-8",
    )
    _root, cfg = load_config(root=tmp_path)
    assert cfg.skills.builtin is False
    assert cfg.skills.builtin_skills == ["ruff", "pytest"]
```

Note: match the actual `load_config` call signature used elsewhere in `tests/test_config.py` (adjust `load_config(root=...)` if the existing tests use a different entry point).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -q -k builtin`
Expected: FAIL — `SkillsConfig` has no field `builtin`.

- [ ] **Step 3: Extend `SkillsConfig`**

In `src/jaunt/config.py`, update the dataclass (around line 84):
```python
@dataclass(frozen=True)
class SkillsConfig:
    auto: bool = True
    max_chars_per_skill: int = 8000
    inject_user_skills: list[str] = field(default_factory=list)
    builtin: bool = True
    builtin_skills: list[str] = field(
        default_factory=lambda: list(_default_builtin_skills())
    )
```
Add a module-level helper near the top of `config.py` (avoids a hard import cycle at import time):
```python
def _default_builtin_skills() -> tuple[str, ...]:
    from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS

    return DEFAULT_BUILTIN_SKILLS
```

- [ ] **Step 4: Parse the new keys**

In the skills-parsing region (after the `inject_user_skills` block ~line 402), add:
```python
    if "builtin" in skills_tbl:
        skills_builtin = _as_bool(skills_tbl["builtin"], name="skills.builtin")
    else:
        skills_builtin = True

    if "builtin_skills" in skills_tbl:
        skills_builtin_skills = _as_str_list(
            skills_tbl["builtin_skills"], name="skills.builtin_skills"
        )
    else:
        from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS

        skills_builtin_skills = list(DEFAULT_BUILTIN_SKILLS)
```
And in the `SkillsConfig(...)` construction (~line 502):
```python
        skills=SkillsConfig(
            auto=skills_auto,
            max_chars_per_skill=skills_max_chars_per_skill,
            inject_user_skills=skills_inject_user,
            builtin=skills_builtin,
            builtin_skills=skills_builtin_skills,
        ),
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -q -k builtin`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/config.py tests/test_config.py
git commit -m "feat(config): add [skills] builtin and builtin_skills"
```

---

### Task 4: Skill-seeding helper + skills fingerprint

**Files:**
- Create: `src/jaunt/skill_seed.py`
- Test: `tests/test_skill_seed.py`

**Interfaces:**
- Consumes: `iter_enabled_builtin_skill_dirs` (Task 1), `skills_dir` from `jaunt.skill_manager`.
- Produces:
  - `seed_skills_into_workspace(workspace_root: Path, *, project_root: Path | None, builtin_names: Sequence[str]) -> list[str]` — copies each enabled builtin skill dir, then the project's `.agents/skills/<name>/` dirs (overriding same-named builtins), into `<workspace_root>/.agents/skills/`. Returns a list of warning strings (best-effort; never raises for a single bad dir).
  - `skills_fingerprint(*, project_root: Path | None, builtin_names: Sequence[str]) -> str` — stable sha256 over the seeded skill set's names + file contents, for cache invalidation.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skill_seed.py
from __future__ import annotations

from pathlib import Path

from jaunt.skill_seed import seed_skills_into_workspace, skills_fingerprint


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_seeds_builtins_into_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = seed_skills_into_workspace(ws, project_root=None, builtin_names=["ruff", "pytest"])
    assert warnings == []
    assert (ws / ".agents" / "skills" / "ruff" / "SKILL.md").is_file()
    assert (ws / ".agents" / "skills" / "pytest" / "SKILL.md").is_file()


def test_project_skill_overrides_builtin(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = tmp_path / "proj"
    _write(proj / ".agents" / "skills" / "ruff" / "SKILL.md", "PROJECT RUFF\n")
    seed_skills_into_workspace(ws, project_root=proj, builtin_names=["ruff"])
    seeded = (ws / ".agents" / "skills" / "ruff" / "SKILL.md").read_text(encoding="utf-8")
    assert seeded == "PROJECT RUFF\n"


def test_unknown_builtin_is_skipped(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = seed_skills_into_workspace(ws, project_root=None, builtin_names=["nope"])
    assert not (ws / ".agents" / "skills" / "nope").exists()
    assert warnings == []  # unknown builtin names are silently skipped (registry-resolved)


def test_fingerprint_changes_with_set(tmp_path):
    a = skills_fingerprint(project_root=None, builtin_names=["ruff"])
    b = skills_fingerprint(project_root=None, builtin_names=["ruff", "pytest"])
    assert a != b
    assert a == skills_fingerprint(project_root=None, builtin_names=["ruff"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skill_seed.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'jaunt.skill_seed'`.

- [ ] **Step 3: Implement the helper**

```python
# src/jaunt/skill_seed.py
"""Seed Agent-Skills into a Codex workspace so `codex exec` discovers them."""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Sequence
from pathlib import Path

from jaunt.skills_builtin import iter_enabled_builtin_skill_dirs


def _project_skill_dirs(project_root: Path | None) -> list[tuple[str, Path]]:
    if project_root is None:
        return []
    from jaunt.skill_manager import skills_dir

    sd = skills_dir(project_root)
    if not sd.is_dir():
        return []
    pairs: list[tuple[str, Path]] = []
    for skill_md in sorted(sd.glob("*/SKILL.md")):
        pairs.append((skill_md.parent.name, skill_md.parent))
    return pairs


def seed_skills_into_workspace(
    workspace_root: Path,
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
) -> list[str]:
    """Copy builtin + project skill dirs into <workspace_root>/.agents/skills/.

    Project skills override builtins of the same name. Best-effort: a failure to
    copy one dir is recorded as a warning and does not abort the rest.
    """
    warnings: list[str] = []
    dest_root = workspace_root / ".agents" / "skills"

    # builtins first, then project (project wins on same name).
    ordered: dict[str, Path] = {}
    for name, src in iter_enabled_builtin_skill_dirs(builtin_names):
        ordered[name] = src
    for name, src in _project_skill_dirs(project_root):
        ordered[name] = src

    for name, src in ordered.items():
        dest = dest_root / name
        try:
            if dest.exists():
                shutil.rmtree(dest)
            shutil.copytree(src, dest)
        except Exception as e:  # noqa: BLE001 - best-effort seeding
            warnings.append(f"failed seeding skill {name!r}: {type(e).__name__}: {e}")
    return warnings


def skills_fingerprint(
    *,
    project_root: Path | None,
    builtin_names: Sequence[str],
) -> str:
    """Stable digest over the seeded skill set (names + file contents)."""
    h = hashlib.sha256()
    ordered: dict[str, Path] = {}
    for name, src in iter_enabled_builtin_skill_dirs(builtin_names):
        ordered[name] = src
    for name, src in _project_skill_dirs(project_root):
        ordered[name] = src

    for name in sorted(ordered):
        h.update(name.encode())
        h.update(b"\x00")
        for f in sorted(ordered[name].rglob("*")):
            if f.is_file():
                h.update(str(f.relative_to(ordered[name])).encode())
                h.update(b"\x00")
                try:
                    h.update(f.read_bytes())
                except Exception:  # noqa: BLE001
                    pass
                h.update(b"\x00")
        h.update(b"\x01")
    return h.hexdigest()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_skill_seed.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/skill_seed.py tests/test_skill_seed.py
git commit -m "feat(skills): workspace seeding helper + skills fingerprint"
```

---

### Task 5: ModuleSpecContext + cache fingerprint — swap `skills_block` for seeding fields

**Files:**
- Modify: `src/jaunt/generate/base.py:16-38` (`ModuleSpecContext`)
- Modify: `src/jaunt/cache.py:86`
- Modify: `tests/test_codex_backend.py:18-34` (`_ctx` helper) and any other test referencing `skills_block` on a ctx
- Test: `tests/test_cache.py` (add invalidation case)

**Interfaces:**
- Produces: `ModuleSpecContext` loses `skills_block`; gains
  - `project_root: Path | None = None`
  - `builtin_skill_names: tuple[str, ...] = ()`
  - `skills_digest: str = ""`
- Consumes (cache): hashes `skills_digest` (replacing the `skills_block` hash) and the sorted `builtin_skill_names`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_cache.py — add
def test_cache_key_changes_with_skills_digest():
    from jaunt.cache import cache_key_from_context
    from jaunt.generate.base import ModuleSpecContext

    base = dict(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["a"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )
    c1 = ModuleSpecContext(**base, skills_digest="aaa")
    c2 = ModuleSpecContext(**base, skills_digest="bbb")
    k1 = cache_key_from_context(c1, model="m", provider="codex", generation_fingerprint="fp")
    k2 = cache_key_from_context(c2, model="m", provider="codex", generation_fingerprint="fp")
    assert k1 != k2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cache.py -q -k skills_digest`
Expected: FAIL — `ModuleSpecContext` got an unexpected keyword argument `skills_digest`.

- [ ] **Step 3: Update `ModuleSpecContext`**

In `src/jaunt/generate/base.py`, add `from pathlib import Path` to imports, then edit the dataclass: remove the `skills_block: str = ""` line and add (after `decorator_apis`):
```python
    project_root: Path | None = None
    builtin_skill_names: tuple[str, ...] = ()
    skills_digest: str = ""
```

- [ ] **Step 4: Update the cache fingerprint**

In `src/jaunt/cache.py`, replace the `skills_block` hashing line (line 86):
```python
    h.update((ctx.skills_block or "").encode())
```
with:
```python
    h.update((ctx.skills_digest or "").encode())
    h.update(b"\x00")
    h.update(json.dumps(sorted(ctx.builtin_skill_names)).encode())
```

- [ ] **Step 5: Update the codex backend test ctx helper**

In `tests/test_codex_backend.py`, in `_ctx(**overrides)`, remove `"skills_block": "",` from the `values` dict and add `"skills_digest": "",`. (Leave the SimpleNamespace approach intact.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_cache.py tests/test_codex_backend.py -q`
Expected: PASS. (If any other test constructs a ctx with `skills_block=`, update it to drop that kwarg — search: `grep -rn "skills_block" tests/`.)

- [ ] **Step 7: Commit**

```bash
git add src/jaunt/generate/base.py src/jaunt/cache.py tests/test_codex_backend.py tests/test_cache.py
git commit -m "refactor(ctx): replace skills_block with project_root/builtin_skill_names/skills_digest"
```

---

### Task 6: CodexBackend seeds skills; drop skill text from the prompt

**Files:**
- Modify: `src/jaunt/generate/codex_backend.py:309-402` (`generate_module`, `_build_prompt`)
- Test: `tests/test_codex_backend.py`

**Interfaces:**
- Consumes: `seed_skills_into_workspace` (Task 4); `ctx.project_root`, `ctx.builtin_skill_names` (Task 5).
- Produces: during `generate_module`, `<tmp>/.agents/skills/` is populated before `codex exec`; `_build_prompt` no longer emits any skills text.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_codex_backend.py — add (uses the existing _FakeProc / monkeypatch pattern in this file)

def test_generate_module_seeds_skills(monkeypatch, tmp_path):
    import jaunt.generate.codex_backend as cb

    captured = {}

    async def _fake_run_codex_exec(*, prompt, cwd, **kwargs):
        # Record what got seeded into the temp workspace.
        skills_root = Path(cwd) / ".agents" / "skills"
        captured["skills"] = sorted(p.name for p in skills_root.glob("*")) if skills_root.is_dir() else []
        captured["prompt"] = prompt
        # Write the target so generate_module can read it back.
        # The backend seeds first, runs codex second; emulate a no-op generation.
        from types import SimpleNamespace
        return SimpleNamespace(
            returncode=0, final_message="", usage_input=1, usage_output=1,
            usage_cached=0, stderr="",
        )

    monkeypatch.setattr(cb, "run_codex_exec", _fake_run_codex_exec)

    backend = _backend()
    ctx = _ctx(
        generated_module="pkg.__generated__.thing",
        project_root=None,
        builtin_skill_names=("ruff", "pytest"),
    )
    import asyncio
    asyncio.run(backend.generate_module(cast("Any", ctx)))
    assert captured["skills"] == ["pytest", "ruff"]
    assert "## What it is" not in captured["prompt"]  # no skill bodies injected
```

(Import `Any` from typing at the top of the test file if not present.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_backend.py -q -k seeds_skills`
Expected: FAIL — skills root not created (backend doesn't seed yet).

- [ ] **Step 3: Seed in `generate_module`**

In `src/jaunt/generate/codex_backend.py`, inside `generate_module`, after the `_context` files are written and before `prompt = self._build_prompt(...)`, add:
```python
            from jaunt.skill_seed import seed_skills_into_workspace

            seed_warnings = seed_skills_into_workspace(
                root,
                project_root=getattr(ctx, "project_root", None),
                builtin_skill_names=list(getattr(ctx, "builtin_skill_names", ()) or ()),
            )
            # Seeding is best-effort; warnings are non-fatal and intentionally not raised.
            _ = seed_warnings
```

- [ ] **Step 4: Drop the skills block from `_build_prompt`**

In `_build_prompt`, remove the line `getattr(ctx, "skills_block", "") or "",` from the `blocks += [...]` list. Add a short discovery pointer right after the spec/contract lines instead:
```python
        blocks.append(
            "Relevant library and tooling skills are available in `.agents/skills/`. "
            "Consult them when they apply."
        )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_codex_backend.py -q`
Expected: PASS (existing prompt-assembly tests + the new seeding test). If a prompt-assembly test asserted on `skills_block` content, update it to assert the new pointer line and absence of skill bodies.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/generate/codex_backend.py tests/test_codex_backend.py
git commit -m "feat(codex): seed .agents/skills into temp workspace; drop skill text injection"
```

---

### Task 7: Migrate generated PyPI skills to YAML frontmatter; drop `build_skills_block`

**Files:**
- Modify: `src/jaunt/skills_auto.py` (`_HEADER_PREFIX`, `_parse_generated_header`, `_format_generated_skill_file`, `ensure_pypi_skills_and_block` → `ensure_pypi_skills`)
- Modify: `src/jaunt/skill_manager.py` (`discover_all_skills` import + classification; remove `build_skills_block`, `_cap_skill_body`)
- Modify: `tests/test_skills_auto.py`, `tests/test_skill_manager.py`
- Test: round-trip + classification

**Interfaces:**
- Produces:
  - `parse_generated_skill_meta(text: str) -> tuple[str, str] | None` (replaces `_parse_generated_header`) — reads `x-jaunt-dist` / `x-jaunt-version` from YAML frontmatter.
  - `_format_generated_skill_file(*, dist, version, body_md) -> str` — emits frontmatter (`name`, `description`, `x-jaunt-dist`, `x-jaunt-version`) + body.
  - `ensure_pypi_skills(...) -> PyPISkillsResult` with fields `warnings: list[str]`, `generation_failures: int`, `dists: dict[str, str]` (no `skills_block`).
- Consumes: nothing new.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_skills_auto.py — add (keep existing imports working after rename)
def test_frontmatter_roundtrip():
    from jaunt.skills_auto import _format_generated_skill_file, parse_generated_skill_meta

    text = _format_generated_skill_file(dist="httpx", version="0.25.0", body_md="# httpx\nbody\n")
    assert text.startswith("---\n")
    assert parse_generated_skill_meta(text) == ("httpx", "0.25.0")
    # plain user-skill text (no frontmatter / no x-jaunt-dist) -> None
    assert parse_generated_skill_meta("# just a heading\n") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_skills_auto.py -q -k frontmatter_roundtrip`
Expected: FAIL — cannot import `parse_generated_skill_meta`.

- [ ] **Step 3: Rewrite format + parser in `skills_auto.py`**

Replace `_HEADER_PREFIX` and `_parse_generated_header`/`_format_generated_skill_file` with frontmatter logic:
```python
def _read_frontmatter(text: str) -> dict[str, str] | None:
    raw = text or ""
    if not raw.startswith("---\n"):
        return None
    end = raw.find("\n---", 4)
    if end == -1:
        return None
    block = raw[4:end]
    meta: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        meta[key.strip()] = val.strip().strip('"')
    return meta


def parse_generated_skill_meta(text: str) -> tuple[str, str] | None:
    """Return (dist, version) for a Jaunt-generated skill, else None."""
    meta = _read_frontmatter(text)
    if not meta:
        return None
    dist = meta.get("x-jaunt-dist")
    version = meta.get("x-jaunt-version")
    if dist and version:
        return dist, version
    return None


def _format_generated_skill_file(*, dist: str, version: str, body_md: str) -> str:
    description = (
        f"Use when generating Python code that imports or uses the {dist} library."
    )
    body = (body_md or "").strip()
    fm = (
        "---\n"
        f'name: "{dist}"\n'
        f'description: "{description}"\n'
        f"x-jaunt-dist: {dist}\n"
        f"x-jaunt-version: {version}\n"
        "---\n"
    )
    return fm + body + "\n"
```
Update the reader in `_generate_pypi_skills` (the `existing_header = _parse_generated_header(first)` logic): read the whole file and call `parse_generated_skill_meta(txt)` instead of parsing only the first line. Concretely, replace:
```python
                first = txt.splitlines()[0] if txt else ""
                existing_header = _parse_generated_header(first)
```
with:
```python
                existing_header = parse_generated_skill_meta(txt)
```

- [ ] **Step 4: Rename `ensure_pypi_skills_and_block` → `ensure_pypi_skills`**

Replace the `SkillsAutoResult` dataclass and the `ensure_pypi_skills_and_block` function. New result + function (drop all `build_skills_block` usage):
```python
@dataclass(frozen=True, slots=True)
class PyPISkillsResult:
    warnings: list[str]
    generation_failures: int = 0
    dists: dict[str, str] = field(default_factory=dict)


async def ensure_pypi_skills(
    *,
    project_root: Path,
    source_roots: Sequence[Path],
    generated_dir: str,
    llm: LLMConfig,
    agent: AgentConfig | None = None,
    codex: CodexConfig | None = None,
    skills: SkillsConfig | None = None,
) -> PyPISkillsResult:
    """Ensure SKILL.md files exist (frontmatter format) for imported PyPI libs."""
    if skills is not None and not skills.auto:
        return PyPISkillsResult(warnings=[], generation_failures=0, dists={})

    warnings: list[str] = []
    dists, scan_warnings = discover_external_distributions_with_warnings(
        source_roots, generated_dir=generated_dir
    )
    warnings.extend(scan_warnings)

    generation_failures = 0
    if dists:
        generation_failures = await _generate_pypi_skills(
            project_root=project_root,
            dists=dists,
            llm=llm,
            agent=agent,
            codex=codex,
            warnings=warnings,
        )
    return PyPISkillsResult(
        warnings=warnings, generation_failures=generation_failures, dists=dict(dists)
    )
```
Add `from dataclasses import dataclass, field` import if not already present (it imports `dataclass` today — add `field`).

- [ ] **Step 5: Update `skill_manager.py`**

- In `discover_all_skills` and anywhere importing `_parse_generated_header` from `skills_auto`, switch to `parse_generated_skill_meta` and pass the full file text (the function already reads `txt`):
  ```python
  from jaunt.skills_auto import parse_generated_skill_meta
  ...
  header = parse_generated_skill_meta(txt)
  ```
  (Remove the `first_line = ...` extraction; pass `txt`.)
- Delete `build_skills_block` and `_cap_skill_body` entirely.

- [ ] **Step 6: Update existing skill tests to the new format**

- In `tests/test_skills_auto.py`: replace `from jaunt.skills_auto import ensure_pypi_skills_and_block, skill_md_path` with `from jaunt.skills_auto import ensure_pypi_skills, skill_md_path`; change every `ensure_pypi_skills_and_block(` call to `ensure_pypi_skills(`. Replace assertions on `res.skills_block` (e.g. lines 112, 159) with assertions on the on-disk file — e.g. `assert skill_md_path(project_root=root, dist=dist).is_file()` and `assert "x-jaunt-dist" in skill_md_path(...).read_text()`. Where a fixture writes a pre-existing skill (`_write(path, f"<!-- jaunt:skill=pypi dist=... -->\nBODY\n")`), replace the header line with frontmatter: `_write(path, _format_generated_skill_file(dist=dist, version=version, body_md="BODY"))` (import the helper).
- In `tests/test_skill_manager.py`: delete the `build_skills_block` tests (the `# --- build_skills_block ---` block and the stale-filter test at line 341). Update the `_header(dist, version)` helper (line 29) to produce frontmatter via `_format_generated_skill_file`, and fix `discover_all_skills` classification tests to write frontmatter-format files.

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_skills_auto.py tests/test_skill_manager.py -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/jaunt/skills_auto.py src/jaunt/skill_manager.py tests/test_skills_auto.py tests/test_skill_manager.py
git commit -m "refactor(skills): YAML frontmatter for generated skills; drop build_skills_block"
```

---

### Task 8: builder — thread project_root/builtin names through `run_build`

**Files:**
- Modify: `src/jaunt/builder.py:1027` (param), `:1287-1311` (ctx construction)
- Test: `tests/test_builder_*` (update any `skills_block=` usage)

**Interfaces:**
- Consumes: `ModuleSpecContext` new fields (Task 5).
- Produces: `run_build(..., project_root: Path | None = None, builtin_skill_names: Sequence[str] = (), skills_digest: str = "")` (replaces `skills_block`); each built ctx carries these three.

- [ ] **Step 1: Update the `run_build` signature**

In `src/jaunt/builder.py`, replace the `skills_block: str = "",` parameter (line ~1027) with:
```python
    project_root: Path | None = None,
    builtin_skill_names: Sequence[str] = (),
    skills_digest: str = "",
```
Ensure `Sequence` is imported (`from collections.abc import Sequence` — it's already used for `build_instructions`).

- [ ] **Step 2: Update ctx construction**

In `_component_payload` (line ~1299), replace `skills_block=skills_block,` with:
```python
                project_root=project_root,
                builtin_skill_names=tuple(builtin_skill_names),
                skills_digest=skills_digest,
```

- [ ] **Step 3: Update any builder tests**

Run `grep -rn "skills_block" tests/test_builder_*.py src/jaunt/builder.py`. Replace any `skills_block=...` in `run_build`/ctx calls with the new kwargs (or drop them — defaults are fine for tests that don't assert on skills).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ -q -k builder`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/builder.py tests/
git commit -m "refactor(builder): thread project_root/builtin_skill_names through run_build"
```

---

### Task 9: tester — thread the same fields through repair + test generation

**Files:**
- Modify: `src/jaunt/tester.py:595-608` (`RepairBuildContext`), `:802-816` (test ctx), `:1158-1173` (repair `run_build` call)
- Test: `tests/test_tester*`/`tests/test_cli_test*` as needed

**Interfaces:**
- Consumes: builder `run_build` new params (Task 8); `ModuleSpecContext` fields (Task 5).
- Produces: `RepairBuildContext` gains `project_root: Path | None = None`, `builtin_skill_names: tuple[str, ...] = ()`, `skills_digest: str = ""` (replaces `skills_block`); test-generation ctx carries the same so generated tests also get seeded skills.

- [ ] **Step 1: Update `RepairBuildContext`**

In `src/jaunt/tester.py`, replace `skills_block: str = ""` (line 605) with:
```python
    project_root: Path | None = None
    builtin_skill_names: tuple[str, ...] = ()
    skills_digest: str = ""
```

- [ ] **Step 2: Update the repair `run_build` call**

At line ~1168, replace `skills_block=repair_build_context.skills_block,` with:
```python
                project_root=repair_build_context.project_root,
                builtin_skill_names=repair_build_context.builtin_skill_names,
                skills_digest=repair_build_context.skills_digest,
```

- [ ] **Step 3: Seed skills for test generation**

The test ctx (line ~802) currently sets no skills fields. Add to that `ModuleSpecContext(kind="test", ...)` call the three fields, threading them from `run_tests`. Add parameters `project_root: Path | None = None`, `builtin_skill_names: Sequence[str] = ()`, `skills_digest: str = ""` to `run_tests` (and the inner ctx-building function), then set on the ctx:
```python
            project_root=project_root,
            builtin_skill_names=tuple(builtin_skill_names),
            skills_digest=skills_digest,
```
(If `run_tests` is large, pass these via the existing config/threading pattern already used for `async_runner`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/ -q -k "tester or test_cli_test"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/tester.py tests/
git commit -m "refactor(tester): thread skills fields through repair + test generation"
```

---

### Task 10: CLI — compute builtin set, rename skills call, add `--no-builtin-skills`

**Files:**
- Modify: `src/jaunt/cli.py:108-115` (arg), `:1204-1224` + `:1373` (build flow), `:1664-1698` (test flow), `:2057-2069` (refresh flow)
- Test: `tests/test_cli*.py`

**Interfaces:**
- Consumes: `ensure_pypi_skills` (Task 7), `skills_fingerprint` (Task 4), `run_build`/`RepairBuildContext`/`run_tests` new params (Tasks 8–9), `cfg.skills.builtin`/`builtin_skills` (Task 3).
- Produces: build & test commands seed builtin + project skills; new `--no-builtin-skills` flag.

- [ ] **Step 1: Add the CLI flag**

Near the existing `--no-auto-skills` definition (line ~108-115), add an analogous flag on the same parsers (build, test):
```python
    p.add_argument(
        "--no-builtin-skills",
        action="store_true",
        dest="no_builtin_skills",
        help="Do not seed Jaunt's bundled builtin skills into the Codex workspace.",
    )
```
(Match the helper that registers `--no-auto-skills` for both `build` and `test` subparsers.)

- [ ] **Step 2: Build flow — replace skills_block computation**

Replace the block at lines ~1204-1224 with:
```python
        builtin_on = bool(cfg.skills.builtin) and not bool(
            getattr(args, "no_builtin_skills", False)
        )
        builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_on else ()
        auto_skills_on = bool(cfg.skills.auto) and not bool(getattr(args, "no_auto_skills", False))
        if auto_skills_on:
            try:
                from jaunt import skills_auto

                skills_res = await skills_auto.ensure_pypi_skills(
                    project_root=root,
                    source_roots=[d for d in source_dirs if d.exists()],
                    generated_dir=cfg.paths.generated_dir,
                    llm=cfg.llm,
                    agent=cfg.agent,
                    codex=cfg.codex,
                    skills=cfg.skills,
                )
                for w in skills_res.warnings:
                    _eprint(f"warn: {w}")
            except Exception as e:  # noqa: BLE001 - best-effort; never block build
                _eprint(f"warn: failed ensuring external library skills: {type(e).__name__}: {e}")

        from jaunt.skill_seed import skills_fingerprint

        build_skills_digest = skills_fingerprint(
            project_root=root, builtin_names=builtin_skill_names
        )
```
Then in the `builder.run_build(...)` call (line ~1373), replace `skills_block=skills_block,` with:
```python
            project_root=root,
            builtin_skill_names=builtin_skill_names,
            skills_digest=build_skills_digest,
```

- [ ] **Step 3: Test flow — same treatment**

Replace lines ~1664-1676 (the `build_skills_block` computation) with:
```python
        builtin_on = bool(cfg.skills.builtin) and not bool(
            getattr(args, "no_builtin_skills", False)
        )
        builtin_skill_names = tuple(cfg.skills.builtin_skills) if builtin_on else ()
        from jaunt.skill_seed import skills_fingerprint

        test_skills_digest = skills_fingerprint(project_root=root, builtin_names=builtin_skill_names)
```
Then in the `tester.RepairBuildContext(...)` constructor (line ~1694), replace `skills_block=build_skills_block,` with:
```python
            project_root=root,
            builtin_skill_names=builtin_skill_names,
            skills_digest=test_skills_digest,
```
And pass the same three to the `tester.run_tests(...)` call (add `project_root=root, builtin_skill_names=builtin_skill_names, skills_digest=test_skills_digest,`).

- [ ] **Step 4: Refresh flow — rename call**

At line ~2060, change `skills_auto.ensure_pypi_skills_and_block(` to `skills_auto.ensure_pypi_skills(` (the return value is only used for status/warnings; update any `res.skills_block` usage in that block to drop it).

- [ ] **Step 5: Run CLI tests**

Run: `uv run pytest tests/test_cli.py tests/test_cli_skill.py -q`
Expected: PASS. Fix any test asserting the removed `--no-auto-skills`-only behavior or `skills_block` plumbing.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/cli.py tests/
git commit -m "feat(cli): seed builtin+project skills; --no-builtin-skills; rename to ensure_pypi_skills"
```

---

### Task 11: Docs + full green sweep

**Files:**
- Modify: `CLAUDE.md` (the `[skills]` config block + skills bullet), `.claude/skills/jaunt/SKILL.md` (config section), `src/jaunt/cli.py` `init` scaffold if it writes a `[skills]` block (search `inject_user_skills`).
- Verify: whole test suite, ruff, ty.

- [ ] **Step 1: Update `CLAUDE.md` `[skills]` documentation**

Replace the `[skills]` block in `CLAUDE.md` with:
```toml
[skills]
auto = true                 # auto-generate PyPI helper skills for imported libs
builtin = true              # seed Jaunt's bundled builtin skills into the Codex workspace
builtin_skills = [          # the default set (override to trim/extend)
  "asyncpg", "dbos", "descope", "fastmcp", "openai", "pydantic", "pydantic-ai",
  "pytest", "ruff", "spacy", "starlette", "ty", "uv",
]
```
Add a sentence in the skills prose: skills are no longer injected as prompt text; Codex discovers them natively from a seeded `.agents/skills/` workspace. Note `max_chars_per_skill`/`inject_user_skills` are retained for back-compat but unused by the Codex builder.

- [ ] **Step 2: Update the jaunt skill doc**

Mirror the same `[skills]` keys and the "seed + native discovery (no prompt injection)" note in `.claude/skills/jaunt/SKILL.md`.

- [ ] **Step 3: Full suite + lint + types**

Run:
```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run ty check
```
Expected: all green. Fix any remaining `skills_block` / `ensure_pypi_skills_and_block` / `build_skills_block` references surfaced by failures (`grep -rn "skills_block\|ensure_pypi_skills_and_block" src/ tests/`).

- [ ] **Step 4: Manual discovery smoke (optional, requires `codex` auth)**

Run an actual build in an example and confirm Codex sees the skills:
```bash
cd examples/jwt_auth && uv run --project ../.. jaunt build --target <a_module> 2>&1 | tail -20
```
Expected: build succeeds; no skills-related warnings.

- [ ] **Step 5: Commit**

```bash
git add CLAUDE.md .claude/skills/jaunt/SKILL.md src/jaunt/cli.py
git commit -m "docs(skills): document builtin skills + seed/native-discovery model"
```

---

## Self-Review

**Spec coverage:**
- Bundled 13 builtin skills (hybrid authoring) → Tasks 1, 2.
- Package-only, seed into temp; project overrides builtins → Task 4 (`seed_skills_into_workspace`), Task 6 (backend seeding).
- Drop full-text injection → Task 6 (`_build_prompt`), Task 7 (`build_skills_block` deleted).
- Frontmatter migration of generated PyPI skills → Task 7.
- Config `builtin` / `builtin_skills` + `--no-builtin-skills` → Tasks 3, 10.
- Threading project_root/builtin names/digest → Tasks 5, 8, 9, 10.
- Test-generation symmetry → Task 9 Step 3.
- Cache invalidation on skill change → Tasks 4 (`skills_fingerprint`), 5 (cache hashes `skills_digest`).
- Docs → Task 11.

**Placeholder scan:** No "TBD"/"handle edge cases"; each code step shows concrete code. The 9 library skill *bodies* are authored from READMEs at implementation time (prose, not code) and gated by a structural test (Task 2 Step 1) — this is authoring, not a code placeholder.

**Type consistency:** `ModuleSpecContext` fields `project_root: Path | None`, `builtin_skill_names: tuple[str, ...]`, `skills_digest: str` are defined in Task 5 and consumed with the same names/types in Tasks 6, 8, 9. `ensure_pypi_skills` / `PyPISkillsResult` / `parse_generated_skill_meta` / `seed_skills_into_workspace` / `skills_fingerprint` signatures match across Tasks 4, 7, 10. `DEFAULT_BUILTIN_SKILLS: tuple[str, ...]` used consistently (Tasks 1, 3).
