# Repo-Context Subsystem Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give Jaunt's Codex engine a maintained, injected tree of 1-line descriptions of the repo (`treedocs.yaml`), plus opt-in `colgrep` semantic retrieval seeded into the generation workspace.

**Architecture:** A new `src/jaunt/repo_context/` package maintains a committed `treedocs.yaml` (treedocs v0.2.0) with per-file AST descriptions (optional LLM enrichment), backed by a gitignored `.jaunt/tree-cache.json` source-digest sidecar. The whole-repo map is rendered into a capped `repo_map_block` that is threaded into the build exactly like the existing `skills_block` (computed once in `_cmd_build_async`, passed to `run_build()`, set on `ModuleSpecContext`, hashed into the response-cache key only — never the freshness digest, so a description change does not restage every module). When enabled, `colgrep --json` is queried per-spec before `codex exec` and the hits are written into `_context/relevant_*.py` (the same channel deps use; the agent reads files, it never shells out).

**Tech Stack:** Python 3.12, argparse CLI, `pyyaml` (new base dep, lazy-imported), `hashlib`, `ast`, `subprocess` (colgrep), pytest + pytest-asyncio.

## Global Constraints

- Python 3.12+, line-length 100, ruff rules E/F/I/UP/B. Run `uv run ruff check --fix . && uv run ruff format .` before each commit.
- Type-check with `uv run ty check`; the repo enforces it via pre-commit.
- The test suite must stay **offline and key-free**: no real `codex`/`colgrep`/network in any test. Mock the backend and `subprocess`/`shutil.which`.
- `pyyaml` is the ONLY new runtime dependency. Import it **lazily** inside `repo_context/` functions (never at module top level of widely-imported modules).
- Defaults: `[context] repo_map = true` (AST-only), `enrich = false`, `[context.search] enabled = false`. When a feature is off, prompts and cache keys must be byte-identical to today.
- `treedocs.yaml` conforms to schema v0.2.0: top-level `schema_version: "0.2.0"`, `project{name,version,last_updated}`, `signature: "sha256:<64hex>"`, `tree`. File entries = compact description string; directory entries use `_doc`.
- Never write volatile content (`last_updated`, `signature`, retrieval scores) into the Codex prompt text — only into the file and the cache key.
- Granularity: directories + `.py` source files only; never descend into the generated dir or `__pycache__`.
- Generated/sidecar artifacts live under `root / ".jaunt"` (already Jaunt's cache convention, e.g. `.jaunt/cache`). `treedocs.yaml` lives at the project root and IS committed.
- Commit after every task with a `feat:`/`test:`/`chore:` message.

---

## File Structure

**New package `src/jaunt/repo_context/`:**
- `__init__.py` — exports the public surface.
- `digests.py` — `source_digest(path)` + `TreeCache` sidecar (`.jaunt/tree-cache.json`).
- `describe.py` — `ast_describe(path)`, `describe_dir(path)` (AST baseline) + `enrich(...)` (phase 2).
- `tree.py` — `TreeDoc` (load / atomic write+lock / signature) + `sync(...)` + `SyncResult`.
- `block.py` — `render_repo_map(treedoc, max_chars)` + `annotate_package_tree(block, treedoc, package_dir)`.
- `search.py` — colgrep `available()/ensure_index()/query()/render_relevant_block()` (phase 3).

**Modified:**
- `pyproject.toml` — add `pyyaml`.
- `src/jaunt/config.py` — `ContextConfig` + `ContextSearchConfig` + parsing + `JauntConfig` field.
- `src/jaunt/generate/base.py` — `ModuleSpecContext.repo_map_block`, `.relevant_context_block`.
- `src/jaunt/cache.py` — hash the two new fields into the key.
- `src/jaunt/generate/codex_backend.py` — inject `repo_map_block`; write `_context/relevant_*.py` + prompt pointer.
- `src/jaunt/builder.py` — `run_build()` param + `_component_payload` wiring.
- `src/jaunt/cli.py` — `jaunt tree` (+`--check`,`--enrich`), build integration, `--no-repo-map`.
- `src/jaunt/watcher.py` — plumb `no_repo_map`.

**New tests:** `tests/test_repo_context_digests.py`, `_describe.py`, `_tree.py`, `_block.py`, `_search.py`; extend `tests/test_config.py`, `tests/test_cache.py`, `tests/test_codex_backend.py`, and a CLI test.

---

## Phase 0 — Dependency + config

### Task 1: Add `pyyaml` + `[context]` config

**Files:**
- Modify: `pyproject.toml:18-24` (dependencies)
- Modify: `src/jaunt/config.py` (add dataclasses ~after line 88; parse in `load_config`; add field to `JauntConfig` ~99-109 and the return ~457-512)
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ContextSearchConfig(enabled: bool=False, internal_retrieval: bool=True, max_hits: int=8)`; `ContextConfig(repo_map: bool=True, repo_map_file: str="treedocs.yaml", enrich: bool=False, max_chars: int=6000, search: ContextSearchConfig=...)`; `JauntConfig.context: ContextConfig`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_config.py`:

```python
def test_context_config_defaults(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path, config_path=tmp_path / "jaunt.toml")
    assert cfg.context.repo_map is True
    assert cfg.context.repo_map_file == "treedocs.yaml"
    assert cfg.context.enrich is False
    assert cfg.context.max_chars == 6000
    assert cfg.context.search.enabled is False
    assert cfg.context.search.internal_retrieval is True
    assert cfg.context.search.max_hits == 8


def test_context_config_parsed(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n\n[context]\nrepo_map = false\nmax_chars = 4000\n"
        "\n[context.search]\nenabled = true\nmax_hits = 12\n",
        encoding="utf-8",
    )
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path, config_path=tmp_path / "jaunt.toml")
    assert cfg.context.repo_map is False
    assert cfg.context.max_chars == 4000
    assert cfg.context.search.enabled is True
    assert cfg.context.search.max_hits == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py::test_context_config_defaults -v`
Expected: FAIL with `AttributeError: 'JauntConfig' object has no attribute 'context'`.

- [ ] **Step 3: Add the dependency**

In `pyproject.toml`, change the dependencies array to include pyyaml:

```toml
dependencies = [
  "rich>=13,<15",
  "watchfiles>=1.0.0",
  "pytest>=8",
  "pytest-asyncio>=1.0",
  "anyio>=4.1",
  "pyyaml>=6.0",
]
```

Then run `uv sync`.

- [ ] **Step 4: Add the dataclasses**

In `src/jaunt/config.py`, immediately after the `SkillsConfig` dataclass (line 88):

```python
@dataclass(frozen=True)
class ContextSearchConfig:
    enabled: bool = False
    internal_retrieval: bool = True
    max_hits: int = 8


@dataclass(frozen=True)
class ContextConfig:
    repo_map: bool = True
    repo_map_file: str = "treedocs.yaml"
    enrich: bool = False
    max_chars: int = 6000
    search: ContextSearchConfig = field(default_factory=ContextSearchConfig)
```

- [ ] **Step 5: Add the field to `JauntConfig`**

In the `JauntConfig` dataclass (after the `skills` field, ~line 107):

```python
    context: ContextConfig = field(default_factory=ContextConfig)
```

- [ ] **Step 6: Parse `[context]` in `load_config()`**

In `load_config()`, after the `[skills]` parsing block (~line 402), add:

```python
    context_tbl = _as_table(data.get("context", {}), name="context")
    if "repo_map" in context_tbl:
        context_repo_map = _as_bool(context_tbl["repo_map"], name="context.repo_map")
    else:
        context_repo_map = True
    if "repo_map_file" in context_tbl:
        context_repo_map_file = _as_str(context_tbl["repo_map_file"], name="context.repo_map_file")
    else:
        context_repo_map_file = "treedocs.yaml"
    if "enrich" in context_tbl:
        context_enrich = _as_bool(context_tbl["enrich"], name="context.enrich")
    else:
        context_enrich = False
    if "max_chars" in context_tbl:
        context_max_chars = _as_int(context_tbl["max_chars"], name="context.max_chars")
    else:
        context_max_chars = 6000

    search_tbl = _as_table(context_tbl.get("search", {}), name="context.search")
    if "enabled" in search_tbl:
        search_enabled = _as_bool(search_tbl["enabled"], name="context.search.enabled")
    else:
        search_enabled = False
    if "internal_retrieval" in search_tbl:
        search_internal = _as_bool(
            search_tbl["internal_retrieval"], name="context.search.internal_retrieval"
        )
    else:
        search_internal = True
    if "max_hits" in search_tbl:
        search_max_hits = _as_int(search_tbl["max_hits"], name="context.search.max_hits")
    else:
        search_max_hits = 8
```

- [ ] **Step 7: Add to the `return JauntConfig(...)`**

In the final `return JauntConfig(...)` (after the `skills=SkillsConfig(...)` argument):

```python
        context=ContextConfig(
            repo_map=context_repo_map,
            repo_map_file=context_repo_map_file,
            enrich=context_enrich,
            max_chars=context_max_chars,
            search=ContextSearchConfig(
                enabled=search_enabled,
                internal_retrieval=search_internal,
                max_hits=search_max_hits,
            ),
        ),
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v && uv run ruff check src/jaunt/config.py && uv run ty check src/jaunt/config.py`
Expected: PASS / clean.

- [ ] **Step 9: Commit**

```bash
git add pyproject.toml uv.lock src/jaunt/config.py tests/test_config.py
git commit -m "feat(config): add [context] config + pyyaml dependency"
```

---

## Phase 1 — Repo-map core (default-on, offline)

### Task 2: `repo_context/digests.py` — source digests + sidecar cache

**Files:**
- Create: `src/jaunt/repo_context/__init__.py`
- Create: `src/jaunt/repo_context/digests.py`
- Test: `tests/test_repo_context_digests.py`

**Interfaces:**
- Produces: `source_digest(path: Path) -> str`; `class TreeCache` with `__init__(self, path: Path)`, `get(self, relpath: str) -> CacheRecord | None`, `set(self, relpath: str, *, source_digest: str, description: str, enriched: bool) -> None`, `prune(self, keep: set[str]) -> None`, `save(self) -> None`; `@dataclass CacheRecord(source_digest: str, description: str, enriched: bool)`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_context_digests.py`:

```python
from pathlib import Path

from jaunt.repo_context.digests import TreeCache, source_digest


def test_source_digest_changes_with_content(tmp_path: Path) -> None:
    f = tmp_path / "a.py"
    f.write_text("x = 1\n", encoding="utf-8")
    d1 = source_digest(f)
    f.write_text("x = 2\n", encoding="utf-8")
    d2 = source_digest(f)
    assert d1 != d2
    assert len(d1) == 64


def test_tree_cache_roundtrip_and_prune(tmp_path: Path) -> None:
    cache = TreeCache(tmp_path / ".jaunt" / "tree-cache.json")
    cache.set("src/a.py", source_digest="aa", description="does a", enriched=False)
    cache.set("src/b.py", source_digest="bb", description="does b", enriched=True)
    cache.save()

    reloaded = TreeCache(tmp_path / ".jaunt" / "tree-cache.json")
    rec = reloaded.get("src/a.py")
    assert rec is not None and rec.description == "does a" and rec.enriched is False
    reloaded.prune(keep={"src/a.py"})
    assert reloaded.get("src/b.py") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_digests.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'jaunt.repo_context'`.

- [ ] **Step 3: Create the package init**

Create `src/jaunt/repo_context/__init__.py`:

```python
"""Repo-context subsystem: maintained treedocs tree + colgrep retrieval."""
```

- [ ] **Step 4: Implement `digests.py`**

Create `src/jaunt/repo_context/digests.py`:

```python
"""Source-content digests and the gitignored treedocs sidecar cache.

The treedocs.yaml `signature` only covers descriptions (manual-edit drift).
Detecting "the file changed, its description is now stale" needs a per-file
source-content digest, kept here in .jaunt/tree-cache.json.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


def source_digest(path: Path) -> str:
    """SHA-256 over a file's raw bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheRecord:
    source_digest: str
    description: str
    enriched: bool


class TreeCache:
    """Path -> CacheRecord sidecar persisted as JSON under .jaunt/."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._records: dict[str, CacheRecord] = {}
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                raw = {}
            for rel, rec in (raw.get("entries") or {}).items():
                try:
                    self._records[rel] = CacheRecord(
                        source_digest=str(rec["source_digest"]),
                        description=str(rec["description"]),
                        enriched=bool(rec.get("enriched", False)),
                    )
                except (KeyError, TypeError):
                    continue

    def get(self, relpath: str) -> CacheRecord | None:
        return self._records.get(relpath)

    def set(self, relpath: str, *, source_digest: str, description: str, enriched: bool) -> None:
        self._records[relpath] = CacheRecord(source_digest, description, enriched)

    def prune(self, keep: set[str]) -> None:
        for rel in list(self._records):
            if rel not in keep:
                del self._records[rel]

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "entries": {
                rel: {
                    "source_digest": r.source_digest,
                    "description": r.description,
                    "enriched": r.enriched,
                }
                for rel, r in sorted(self._records.items())
            }
        }
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self._path)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_repo_context_digests.py -v && uv run ruff check src/jaunt/repo_context && uv run ty check src/jaunt/repo_context`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/repo_context/__init__.py src/jaunt/repo_context/digests.py tests/test_repo_context_digests.py
git commit -m "feat(repo-context): source digests + treedocs sidecar cache"
```

---

### Task 3: `repo_context/describe.py` — AST baseline descriptions

**Files:**
- Create: `src/jaunt/repo_context/describe.py`
- Test: `tests/test_repo_context_describe.py`

**Interfaces:**
- Produces: `ast_describe(path: Path, *, max_len: int = 100) -> str`; `describe_dir(path: Path, *, max_len: int = 100) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_context_describe.py`:

```python
from pathlib import Path

from jaunt.repo_context.describe import ast_describe, describe_dir


def test_describe_uses_module_docstring(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text('"""First line of doc.\n\nMore."""\n\ndef foo():\n    pass\n', encoding="utf-8")
    assert ast_describe(f) == "First line of doc."


def test_describe_synthesizes_from_public_names(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def foo():\n    pass\n\nclass Bar:\n    pass\n", encoding="utf-8")
    desc = ast_describe(f)
    assert "Bar" in desc and "foo" in desc


def test_describe_caps_length(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text('"""' + "x" * 500 + '"""\n', encoding="utf-8")
    assert len(ast_describe(f, max_len=80)) <= 80


def test_describe_syntax_error_is_safe(tmp_path: Path) -> None:
    f = tmp_path / "m.py"
    f.write_text("def (:\n", encoding="utf-8")
    assert ast_describe(f) == "Python module"


def test_describe_dir_from_init(tmp_path: Path) -> None:
    d = tmp_path / "pkg"
    d.mkdir()
    (d / "__init__.py").write_text('"""The pkg package."""\n', encoding="utf-8")
    assert describe_dir(d) == "The pkg package."
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_describe.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `describe.py`**

Create `src/jaunt/repo_context/describe.py`:

```python
"""Deterministic AST-based one-line descriptions (the always-on baseline)."""

from __future__ import annotations

import ast
from pathlib import Path

_FALLBACK = "Python module"


def _first_doc_line(tree: ast.Module) -> str | None:
    doc = ast.get_docstring(tree, clean=True)
    if not doc:
        return None
    for line in doc.splitlines():
        line = line.strip()
        if line:
            return line
    return None


def _public_surface(tree: ast.Module) -> str | None:
    names: list[str] = []
    decorated_magic = False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name.startswith("_"):
                continue
            names.append(node.name)
            for dec in node.decorator_list:
                src = ast.unparse(dec)
                if "jaunt.magic" in src or "jaunt.test" in src or "jaunt.contract" in src:
                    decorated_magic = True
    if not names:
        return None
    prefix = "specs: " if decorated_magic else "defines "
    return prefix + ", ".join(names[:6])


def _cap(text: str, max_len: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def ast_describe(path: Path, *, max_len: int = 100) -> str:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, OSError, ValueError):
        return _FALLBACK
    return _cap(_first_doc_line(tree) or _public_surface(tree) or _FALLBACK, max_len)


def describe_dir(path: Path, *, max_len: int = 100) -> str:
    init = path / "__init__.py"
    if init.exists():
        desc = ast_describe(init, max_len=max_len)
        if desc != _FALLBACK:
            return desc
    children = sorted(
        p.stem for p in path.glob("*.py") if not p.name.startswith("_")
    )
    if children:
        return _cap("package: " + ", ".join(children[:6]), max_len)
    return _cap(f"{path.name} package", max_len)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repo_context_describe.py -v && uv run ruff check src/jaunt/repo_context/describe.py && uv run ty check src/jaunt/repo_context/describe.py`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/repo_context/describe.py tests/test_repo_context_describe.py
git commit -m "feat(repo-context): AST baseline description generation"
```

---

### Task 4: `repo_context/tree.py` — TreeDoc model + sync

**Files:**
- Create: `src/jaunt/repo_context/tree.py`
- Test: `tests/test_repo_context_tree.py`

**Interfaces:**
- Consumes: `digests.source_digest`, `digests.TreeCache`, `describe.ast_describe`, `describe.describe_dir`, `discovery.discover_module_files`.
- Produces:
  - `@dataclass SyncResult(added: list[str], removed: list[str], restaled: list[str])`
  - `class TreeDoc` with `tree: dict`, classmethod `load(path: Path) -> TreeDoc`, `signature() -> str`, `write(path: Path) -> bool` (atomic+lock; returns False and writes nothing when content-identical), `paths() -> set[str]`.
  - `sync(*, repo_root: Path, source_roots: list[Path], generated_dir: str, cache: TreeCache, project_name: str, project_version: str, today: str) -> tuple[TreeDoc, SyncResult]`
  - `is_drifted(treedoc: TreeDoc, *, repo_root, source_roots, generated_dir, cache) -> bool`

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_context_tree.py`:

```python
from pathlib import Path

from jaunt.repo_context.digests import TreeCache
from jaunt.repo_context import tree as tree_mod


def _project(tmp_path: Path) -> Path:
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
    (src / "a.py").write_text('"""Module A."""\n', encoding="utf-8")
    return tmp_path


def test_sync_adds_entries_and_signature_stable(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-29",
    )
    assert "src/pkg/a.py" in result.added
    sig1 = doc.signature()
    doc2, _ = tree_mod.sync(
        repo_root=root,
        source_roots=[root / "src"],
        generated_dir="__generated__",
        cache=cache,
        project_name="pkg",
        project_version="0.0.0",
        today="2026-06-30",
    )
    assert doc2.signature() == sig1  # description content unchanged -> stable


def test_sync_drops_ghosts_and_write_skips_when_unchanged(tmp_path: Path) -> None:
    root = _project(tmp_path)
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, _ = tree_mod.sync(
        repo_root=root, source_roots=[root / "src"], generated_dir="__generated__",
        cache=cache, project_name="pkg", project_version="0.0.0", today="2026-06-29",
    )
    out = root / "treedocs.yaml"
    assert doc.write(out) is True
    # Second identical write is a no-op (no churn).
    doc2 = tree_mod.TreeDoc.load(out)
    assert doc2.write(out) is False

    (root / "src" / "pkg" / "a.py").unlink()
    cache2 = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc3, result = tree_mod.sync(
        repo_root=root, source_roots=[root / "src"], generated_dir="__generated__",
        cache=cache2, project_name="pkg", project_version="0.0.0", today="2026-06-29",
    )
    assert "src/pkg/a.py" in result.removed
    assert "src/pkg/a.py" not in doc3.paths()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_tree.py -v`
Expected: FAIL with `ModuleNotFoundError`/`AttributeError`.

- [ ] **Step 3: Implement `tree.py`**

Create `src/jaunt/repo_context/tree.py`:

```python
"""treedocs.yaml model + incremental sync (cross-platform, Python-only)."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from jaunt.repo_context.describe import ast_describe, describe_dir
from jaunt.repo_context.digests import TreeCache, source_digest

SCHEMA_VERSION = "0.2.0"
_SCHEMA_URL = "https://dandylyons.github.io/treedocs/schemas/0.2.0/treedocs.schema.json"


@dataclass
class SyncResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    restaled: list[str] = field(default_factory=list)


@contextlib.contextmanager
def _lock(lock_path: Path, *, timeout: float = 10.0):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() > deadline:
                # Stale lock fallback: proceed without blocking the build forever.
                break
            time.sleep(0.05)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(lock_path)


@dataclass
class TreeDoc:
    project_name: str
    project_version: str
    last_updated: str
    tree: dict  # nested mapping mirroring the filesystem

    @classmethod
    def load(cls, path: Path) -> TreeDoc:
        import yaml  # lazy

        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        project = data.get("project", {}) or {}
        return cls(
            project_name=str(project.get("name", "")),
            project_version=str(project.get("version", "")),
            last_updated=str(project.get("last_updated", "")),
            tree=data.get("tree", {}) or {},
        )

    def signature(self) -> str:
        """sha256 over canonical tree descriptions only (manual-edit drift)."""
        canonical = json.dumps(self.tree, sort_keys=True, ensure_ascii=False)
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def paths(self) -> set[str]:
        out: set[str] = set()

        def walk(node: dict, prefix: str) -> None:
            for key, val in node.items():
                if key in ("_doc", "_description", "_references", "_link"):
                    continue
                rel = f"{prefix}/{key}" if prefix else key
                if isinstance(val, dict):
                    out.add(rel)
                    walk(val, rel)
                else:
                    out.add(rel)

        walk(self.tree, "")
        return out

    def write(self, path: Path) -> bool:
        """Atomic write under a lock. Returns False (no write) if unchanged."""
        import yaml  # lazy

        if path.exists():
            with contextlib.suppress(Exception):
                if TreeDoc.load(path).signature() == self.signature():
                    return False
        payload = {
            "schema_version": SCHEMA_VERSION,
            "project": {
                "name": self.project_name,
                "version": self.project_version,
                "last_updated": self.last_updated,
            },
            "signature": self.signature(),
            "tree": self.tree,
        }
        body = (
            f"# yaml-language-server: $schema={_SCHEMA_URL}\n"
            + yaml.safe_dump(payload, sort_keys=True, allow_unicode=True, default_flow_style=False)
        )
        with _lock(path.parent / ".jaunt" / "tree.lock"):
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(body, encoding="utf-8")
            os.replace(tmp, path)
        return True


def _iter_entries(
    *, source_roots: list[Path], generated_dir: str
) -> list[Path]:
    out: list[Path] = []
    for sr in source_roots:
        if not sr.exists():
            continue
        for p in sr.rglob("*.py"):
            if generated_dir in p.parts or "__pycache__" in p.parts:
                continue
            out.append(p)
    return out


def _insert(tree: dict, parts: list[str], description: str, *, is_dir: bool) -> None:
    node = tree
    for part in parts[:-1]:
        node = node.setdefault(part, {})
        if not isinstance(node, dict):  # a file shadowed a dir name; reset
            node = {}
    leaf = parts[-1]
    if is_dir:
        d = node.setdefault(leaf, {})
        if isinstance(d, dict):
            d["_doc"] = description
    else:
        node[leaf] = description


def sync(
    *,
    repo_root: Path,
    source_roots: list[Path],
    generated_dir: str,
    cache: TreeCache,
    project_name: str,
    project_version: str,
    today: str,
) -> tuple[TreeDoc, SyncResult]:
    result = SyncResult()
    tree: dict = {}
    seen: set[str] = set()
    dirs: set[Path] = set()

    for path in sorted(_iter_entries(source_roots=source_roots, generated_dir=generated_dir)):
        rel = path.resolve().relative_to(repo_root.resolve()).as_posix()
        seen.add(rel)
        digest = source_digest(path)
        rec = cache.get(rel)
        if rec is None:
            result.added.append(rel)
        elif rec.source_digest != digest:
            result.restaled.append(rel)
        description = (
            rec.description
            if rec is not None and rec.source_digest == digest
            else ast_describe(path)
        )
        cache.set(rel, source_digest=digest, description=description, enriched=False)
        _insert(tree, rel.split("/"), description, is_dir=False)
        for parent in path.resolve().parents:
            if parent == repo_root.resolve():
                break
            dirs.add(parent)

    for d in sorted(dirs):
        rel = d.resolve().relative_to(repo_root.resolve()).as_posix()
        if not rel:
            continue
        _insert(tree, rel.split("/"), describe_dir(d), is_dir=True)

    for rel in list(cache._records):  # noqa: SLF001 - prune ghosts
        if rel not in seen:
            result.removed.append(rel)
    cache.prune(keep=seen)

    return (
        TreeDoc(
            project_name=project_name,
            project_version=project_version,
            last_updated=today,
            tree=tree,
        ),
        result,
    )


def is_drifted(
    treedoc: TreeDoc,
    *,
    repo_root: Path,
    source_roots: list[Path],
    generated_dir: str,
    cache: TreeCache,
) -> bool:
    fresh, result = sync(
        repo_root=repo_root,
        source_roots=source_roots,
        generated_dir=generated_dir,
        cache=TreeCache(repo_root / ".jaunt" / "_drift_probe.json"),
        project_name=treedoc.project_name,
        project_version=treedoc.project_version,
        today=treedoc.last_updated,
    )
    if result.added or result.removed or result.restaled:
        return True
    return fresh.signature() != treedoc.signature()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repo_context_tree.py -v && uv run ruff check src/jaunt/repo_context/tree.py && uv run ty check src/jaunt/repo_context/tree.py`
Expected: PASS / clean. (If ruff flags `_lock`'s missing return annotation, add `-> Iterator[None]` with `from collections.abc import Iterator`.)

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/repo_context/tree.py tests/test_repo_context_tree.py
git commit -m "feat(repo-context): TreeDoc model + incremental sync + drift"
```

---

### Task 5: `repo_context/block.py` — render + annotate

**Files:**
- Create: `src/jaunt/repo_context/block.py`
- Test: `tests/test_repo_context_block.py`

**Interfaces:**
- Consumes: `tree.TreeDoc`.
- Produces: `render_repo_map(treedoc: TreeDoc, *, max_chars: int = 6000) -> str`; `annotate_package_tree(block: str, treedoc: TreeDoc, *, package_dir: Path, repo_root: Path) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_context_block.py`:

```python
from pathlib import Path

from jaunt.repo_context.tree import TreeDoc
from jaunt.repo_context.block import render_repo_map, annotate_package_tree


def _doc() -> TreeDoc:
    return TreeDoc(
        project_name="pkg",
        project_version="0",
        last_updated="2026-06-29",
        tree={"src": {"_doc": "source", "a.py": "module a", "b.py": "module b"}},
    )


def test_render_repo_map_includes_descriptions_no_volatile() -> None:
    out = render_repo_map(_doc())
    assert out.startswith("## Repository map")
    assert "a.py" in out and "module a" in out
    assert "2026-06-29" not in out and "sha256:" not in out  # no volatile fields


def test_render_repo_map_caps() -> None:
    big = {f"f{i}.py": "x" * 50 for i in range(1000)}
    doc = TreeDoc("p", "0", "2026-06-29", big)
    out = render_repo_map(doc, max_chars=500)
    assert len(out) <= 500 + 64  # header + truncation marker slack
    assert "truncated" in out.lower()


def test_render_repo_map_empty_is_empty() -> None:
    assert render_repo_map(TreeDoc("p", "0", "2026-06-29", {})) == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_block.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement `block.py`**

Create `src/jaunt/repo_context/block.py`:

```python
"""Render the repo map into prompt text (prompt-cache safe: no volatile fields)."""

from __future__ import annotations

from pathlib import Path

from jaunt.repo_context.tree import TreeDoc

_HEADER = "## Repository map"
_TRUNC = "  ... (repo map truncated to fit budget)"


def _lines(node: dict, prefix: str) -> list[str]:
    out: list[str] = []
    for key in sorted(node):
        if key in ("_doc", "_description", "_references", "_link"):
            continue
        val = node[key]
        path = f"{prefix}/{key}" if prefix else key
        if isinstance(val, dict):
            doc = val.get("_doc") or val.get("_description") or ""
            out.append(f"{path}/ — {doc}" if doc else f"{path}/")
            out.extend(_lines(val, path))
        else:
            out.append(f"{path} — {val}")
    return out


def render_repo_map(treedoc: TreeDoc, *, max_chars: int = 6000) -> str:
    body_lines = _lines(treedoc.tree, "")
    if not body_lines:
        return ""
    out = _HEADER + "\n"
    kept: list[str] = []
    used = len(out)
    truncated = False
    for line in body_lines:
        if used + len(line) + 1 > max_chars:
            truncated = True
            break
        kept.append(line)
        used += len(line) + 1
    text = out + "\n".join(kept)
    if truncated:
        text += "\n" + _TRUNC
    return text


def annotate_package_tree(
    block: str, treedoc: TreeDoc, *, package_dir: Path, repo_root: Path
) -> str:
    """Append descriptions to the existing '## Package tree' lines (best-effort)."""
    if not block or "## Package tree" not in block:
        return block
    flat: dict[str, str] = {}

    def walk(node: dict, prefix: str) -> None:
        for key, val in node.items():
            if key in ("_doc", "_description", "_references", "_link"):
                continue
            path = f"{prefix}/{key}" if prefix else key
            if isinstance(val, dict):
                walk(val, path)
            else:
                flat[path] = val

    walk(treedoc.tree, "")
    try:
        pkg_rel = package_dir.resolve().relative_to(repo_root.resolve()).as_posix()
    except ValueError:
        pkg_rel = ""

    lines = block.splitlines()
    out: list[str] = []
    in_tree = False
    for line in lines:
        if line.startswith("## Package tree"):
            in_tree = True
            out.append(line)
            continue
        if in_tree and line.startswith("## "):
            in_tree = False
        if in_tree and line.strip() and "—" not in line:
            rel = f"{pkg_rel}/{line.strip()}" if pkg_rel else line.strip()
            desc = flat.get(rel)
            out.append(f"{line} — {desc}" if desc else line)
        else:
            out.append(line)
    return "\n".join(out)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repo_context_block.py -v && uv run ruff check src/jaunt/repo_context/block.py && uv run ty check src/jaunt/repo_context/block.py`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/repo_context/block.py tests/test_repo_context_block.py
git commit -m "feat(repo-context): render repo-map block + annotate package tree"
```

---

### Task 6: Wire `repo_map_block` into context, cache key, and prompt

**Files:**
- Modify: `src/jaunt/generate/base.py` (add fields to `ModuleSpecContext`, after line 33 `whole_class: bool = False` — add new fields before it or after, all defaulted)
- Modify: `src/jaunt/cache.py:cache_key_from_context` (add two `h.update(...)` before the `generation_fingerprint` update, ~line 105)
- Modify: `src/jaunt/generate/codex_backend.py:_build_prompt` (inject `repo_map_block`)
- Test: `tests/test_cache.py`, `tests/test_codex_backend.py`

**Interfaces:**
- Produces: `ModuleSpecContext.repo_map_block: str = ""`, `ModuleSpecContext.relevant_context_block: str = ""`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_cache.py` (the file already has `_make_ctx(**overrides)`):

```python
def test_repo_map_block_changes_cache_key() -> None:
    from jaunt.cache import cache_key_from_context

    base = _make_ctx()
    with_map = _make_ctx(repo_map_block="## Repository map\nsrc/a.py — does a")
    k1 = cache_key_from_context(base, model="m", provider="p")
    k2 = cache_key_from_context(with_map, model="m", provider="p")
    assert k1 != k2


def test_relevant_block_changes_cache_key() -> None:
    from jaunt.cache import cache_key_from_context

    k1 = cache_key_from_context(_make_ctx(), model="m", provider="p")
    k2 = cache_key_from_context(
        _make_ctx(relevant_context_block="see _context/relevant_0.py"),
        model="m",
        provider="p",
    )
    assert k1 != k2
```

Add to `tests/test_codex_backend.py` (match its existing ctx-builder/backend pattern):

```python
def test_build_prompt_includes_repo_map_block() -> None:
    from jaunt.generate.codex_backend import CodexBackend

    backend = _make_backend()  # existing helper in this test file
    ctx = _make_build_ctx(repo_map_block="## Repository map\nsrc/a.py — does a")
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/m.py"), None)
    assert "## Repository map" in prompt
    assert prompt.index("## Repository map") > prompt.index("Write a complete Python module")
```

> If `_make_build_ctx`/`_make_backend` don't exist in `tests/test_codex_backend.py`, reuse the construction pattern already present in that file (it builds `ModuleSpecContext` and `CodexBackend(...)` directly) and pass `repo_map_block=...`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cache.py::test_repo_map_block_changes_cache_key tests/test_codex_backend.py -k repo_map -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'repo_map_block'`).

- [ ] **Step 3: Add the fields to `ModuleSpecContext`**

In `src/jaunt/generate/base.py`, inside the `ModuleSpecContext` dataclass, add (after `package_context_block: str = ""`):

```python
    repo_map_block: str = ""
    relevant_context_block: str = ""
```

- [ ] **Step 4: Hash them into the cache key**

In `src/jaunt/cache.py:cache_key_from_context`, immediately before the final
`generation_fingerprint` update (the line `h.update(generation_fingerprint.encode())`), add:

```python
    h.update((ctx.repo_map_block or "").encode())
    h.update(b"\x00")
    h.update((ctx.relevant_context_block or "").encode())
    h.update(b"\x00")
```

- [ ] **Step 5: Inject into the prompt**

In `src/jaunt/generate/codex_backend.py:_build_prompt`, change the `blocks += [...]`
list to add the repo map after `package_context_block`, and append the retrieval
pointer as the final (most volatile) block. Replace the `blocks += [...]` block and
the footer/return with:

```python
        blocks += [
            getattr(ctx, "build_instructions_block", "") or "",
            getattr(ctx, "module_contract_block", "") or "",
            getattr(ctx, "base_contract_block", "") or "",
            getattr(ctx, "package_context_block", "") or "",
            getattr(ctx, "repo_map_block", "") or "",
            getattr(ctx, "skills_block", "") or "",
        ]
        blocks.append(
            "Edit ONLY the target file. Do not create other files, run tests, or modify "
            "anything else. Output the full module - no placeholders."
        )
        relevant = getattr(ctx, "relevant_context_block", "") or ""
        if relevant.strip():
            blocks.append(relevant)
        if extra_error_context:
            blocks.append("Previous attempt problems:\n" + "\n".join(extra_error_context))
        return "\n\n".join(b for b in blocks if b)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_cache.py tests/test_codex_backend.py -v && uv run ty check src/jaunt/generate/base.py src/jaunt/cache.py src/jaunt/generate/codex_backend.py`
Expected: PASS / clean. Also run the existing `_make_ctx` cache tests to confirm defaults keep old keys stable for feature-off.

- [ ] **Step 7: Commit**

```bash
git add src/jaunt/generate/base.py src/jaunt/cache.py src/jaunt/generate/codex_backend.py tests/test_cache.py tests/test_codex_backend.py
git commit -m "feat(repo-context): inject repo_map_block + hash into cache key"
```

---

### Task 7: Thread `repo_map_block` through `run_build`

**Files:**
- Modify: `src/jaunt/builder.py:run_build` signature (~line 1037, add param) and the `_component_payload` `ModuleSpecContext(...)` constructor (~line 1287, add `repo_map_block=repo_map_block`)
- Test: `tests/test_builder_methods.py` (or a new `tests/test_builder_repo_map.py`)

**Interfaces:**
- Consumes: callers pass `repo_map_block: str = ""` to `run_build`.
- Produces: every built `ModuleSpecContext` carries `repo_map_block`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_builder_repo_map.py`. This test asserts the closure sets the field by capturing the ctx the backend receives:

```python
import asyncio
from pathlib import Path

import jaunt.builder as builder


class _CapturingBackend:
    def __init__(self) -> None:
        self.seen: list[str] = []

    @property
    def supports_structured_output(self) -> bool:
        return False

    async def aclose(self) -> None:
        return None

    async def generate_module(self, ctx, *, extra_error_context=None):
        self.seen.append(ctx.repo_map_block)
        return ("x = 1\n", None)


def test_run_build_propagates_repo_map_block(tmp_path: Path) -> None:
    # Minimal: a single magic spec module on disk, then run_build with repo_map_block.
    # Reuse the harness already used by tests/test_builder_methods.py to assemble
    # module_specs/specs/spec_graph/module_dag/stale_modules for a trivial spec.
    # The assertion is backend.seen[0] == "MAP".
    ...
```

> The minimal-build harness is non-trivial to hand-assemble; reuse the existing
> fixtures/builders in `tests/test_builder_methods.py` (it already drives
> `run_build`/`_component_payload` for a trivial module). Copy its setup, add
> `repo_map_block="MAP"` to the `run_build(...)` call, pass `_CapturingBackend()`,
> and assert `backend.seen[0] == "MAP"`. If no such harness exists, assert at the
> unit level instead: call `builder._component_payload` is not exported, so test via
> the public `run_build` path.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_builder_repo_map.py -v`
Expected: FAIL (`TypeError: run_build() got an unexpected keyword argument 'repo_map_block'`).

- [ ] **Step 3: Add the `run_build` parameter**

In `src/jaunt/builder.py:run_build`, add to the keyword-only params (next to
`skills_block: str = ""`, ~line 1027):

```python
    repo_map_block: str = "",
```

- [ ] **Step 4: Set the field in the closure**

In `_component_payload`'s `ModuleSpecContext(...)` constructor (~line 1299, next to
`skills_block=skills_block,`):

```python
                repo_map_block=repo_map_block,
```

(`repo_map_block` is captured from `run_build`'s scope exactly like `skills_block`.)

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/test_builder_repo_map.py -v && uv run ty check src/jaunt/builder.py`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/builder.py tests/test_builder_repo_map.py
git commit -m "feat(repo-context): thread repo_map_block through run_build"
```

---

### Task 8: `jaunt tree` command + build integration + `--no-repo-map`

**Files:**
- Create: `src/jaunt/repo_context/api.py` — a single high-level helper used by both `cmd_tree` and `_cmd_build_async`.
- Modify: `src/jaunt/cli.py` — register `tree` subparser, add `cmd_tree`, dispatch line, add `--no-repo-map` flag to build, and compute+thread `repo_map_block` in `_cmd_build_async`.
- Test: `tests/test_cli_tree.py`

**Interfaces:**
- Produces in `api.py`:
  - `sync_tree(*, root: Path, cfg, today: str, enrich: bool | None = None) -> tuple[TreeDoc, SyncResult]` — loads source roots from cfg, runs `tree.sync`, saves cache, writes `treedocs.yaml` (respecting `repo_map_file`).
  - `repo_map_block_for_build(*, root: Path, cfg, today: str) -> str` — calls `sync_tree` then `block.render_repo_map`; returns `""` when `cfg.context.repo_map` is False or on any error.
  - `check_drift(*, root: Path, cfg) -> SyncResult | None` — returns drift info for `--check` (None means clean).

- [ ] **Step 1: Write the failing test**

Create `tests/test_cli_tree.py`:

```python
import argparse
from pathlib import Path

from jaunt.cli import cmd_tree


def _project(tmp_path: Path) -> Path:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
    (src / "a.py").write_text('"""Module A."""\n', encoding="utf-8")
    return tmp_path


def _args(root: Path, **kw) -> argparse.Namespace:
    ns = argparse.Namespace(
        root=str(root), config=None, json_output=False,
        force=False, enrich=False, no_enrich=False, check=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_tree_creates_treedocs_yaml(tmp_path: Path) -> None:
    root = _project(tmp_path)
    rc = cmd_tree(_args(root))
    assert rc == 0
    assert (root / "treedocs.yaml").exists()
    text = (root / "treedocs.yaml").read_text(encoding="utf-8")
    assert "schema_version" in text and "src" in text


def test_tree_check_detects_drift(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert cmd_tree(_args(root)) == 0           # build the tree
    assert cmd_tree(_args(root, check=True)) == 0  # clean
    (root / "src" / "pkg" / "b.py").write_text('"""B."""\n', encoding="utf-8")
    assert cmd_tree(_args(root, check=True)) == 4   # new path -> drift, exit 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_tree.py -v`
Expected: FAIL with `ImportError: cannot import name 'cmd_tree'`.

- [ ] **Step 3: Implement `repo_context/api.py`**

Create `src/jaunt/repo_context/api.py`:

```python
"""High-level repo-context entry points used by the CLI and build path."""

from __future__ import annotations

from pathlib import Path

from jaunt.repo_context import block as block_mod
from jaunt.repo_context import tree as tree_mod
from jaunt.repo_context.digests import TreeCache


def _source_roots(root: Path, cfg) -> list[Path]:
    return [root / sr for sr in cfg.paths.source_roots]


def sync_tree(*, root: Path, cfg, today: str, enrich: bool | None = None):
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=cfg.paths.generated_dir,
        cache=cache,
        project_name=root.name,
        project_version=str(cfg.version),
        today=today,
    )
    cache.save()
    doc.write(root / cfg.context.repo_map_file)
    return doc, result


def repo_map_block_for_build(*, root: Path, cfg, today: str) -> str:
    if not cfg.context.repo_map:
        return ""
    try:
        doc, _ = sync_tree(root=root, cfg=cfg, today=today)
        return block_mod.render_repo_map(doc, max_chars=cfg.context.max_chars)
    except Exception:  # noqa: BLE001 - never block the build on map maintenance
        return ""


def check_drift(*, root: Path, cfg):
    path = root / cfg.context.repo_map_file
    if not path.exists():
        return tree_mod.SyncResult(added=["<treedocs.yaml missing>"])
    cache = TreeCache(root / ".jaunt" / "tree-cache.json")
    doc = tree_mod.TreeDoc.load(path)
    fresh, result = tree_mod.sync(
        repo_root=root,
        source_roots=_source_roots(root, cfg),
        generated_dir=cfg.paths.generated_dir,
        cache=TreeCache(root / ".jaunt" / "_drift_probe.json"),
        project_name=root.name,
        project_version=str(cfg.version),
        today=doc.last_updated,
    )
    if result.added or result.removed or result.restaled or fresh.signature() != doc.signature():
        return result if (result.added or result.removed or result.restaled) else tree_mod.SyncResult(restaled=["<signature mismatch>"])
    return None
```

- [ ] **Step 4: Add `cmd_tree` to `cli.py`**

Add a `today` helper and `cmd_tree` near the other `cmd_*` handlers in `src/jaunt/cli.py`:

```python
def _today() -> str:
    from datetime import date

    return date.today().isoformat()


def cmd_tree(args: argparse.Namespace) -> int:
    json_mode = _is_json_mode(args)
    try:
        root, cfg = _load_config(args)
        from jaunt.repo_context import api as rc_api

        if getattr(args, "check", False):
            drift = rc_api.check_drift(root=root, cfg=cfg)
            if json_mode:
                _emit_json(
                    {
                        "command": "tree",
                        "ok": drift is None,
                        "drift": None
                        if drift is None
                        else {"added": drift.added, "removed": drift.removed, "restaled": drift.restaled},
                    }
                )
            elif drift is None:
                print("treedocs.yaml is up to date.")
            else:
                _eprint(
                    f"drift: +{len(drift.added)} new, -{len(drift.removed)} removed, "
                    f"~{len(drift.restaled)} stale description(s). Run `jaunt tree`."
                )
            return EXIT_OK if drift is None else 4

        doc, result = rc_api.sync_tree(root=root, cfg=cfg, today=_today())
        if json_mode:
            _emit_json(
                {
                    "command": "tree",
                    "ok": True,
                    "added": result.added,
                    "removed": result.removed,
                    "restaled": result.restaled,
                }
            )
        else:
            print(
                f"Synced {cfg.context.repo_map_file}: "
                f"+{len(result.added)} new, -{len(result.removed)} removed, "
                f"~{len(result.restaled)} updated."
            )
        return EXIT_OK
    except (JauntConfigError, JauntDiscoveryError) as e:
        _print_error(e)
        if json_mode:
            _emit_json({"command": "tree", "ok": False, "error": str(e)})
        return EXIT_CONFIG_OR_DISCOVERY
```

- [ ] **Step 5: Register the `tree` subparser + dispatch**

In `_build_parser()`, after the `status` registration (~line 181):

```python
    tree_p = subparsers.add_parser("tree", help="Maintain treedocs.yaml repo map.")
    _add_common_flags(tree_p)
    tree_p.add_argument("--check", action="store_true", help="Fail (exit 4) if the tree is stale.")
    tree_p.add_argument("--enrich", action="store_true", help="Force LLM enrichment this run.")
    tree_p.add_argument("--no-enrich", action="store_true", help="Force AST-only this run.")
```

In `main()`'s dispatch (next to the `status` line ~2333):

```python
    if args.command == "tree":
        return cmd_tree(args)
```

- [ ] **Step 6: Add `--no-repo-map` to build and compute the block**

In `_build_parser()` where the `build` subparser is set up, add the flag (near `--no-auto-skills`):

```python
    build_p.add_argument(
        "--no-repo-map", action="store_true", help="Disable repo-map injection for this build."
    )
```

In `src/jaunt/cli.py:_cmd_build_async`, immediately AFTER the `skills_block` block
(right before `_prepend_sys_path([*source_dirs, root])`, ~line 1233), add:

```python
        repo_map_block = ""
        if cfg.context.repo_map and not bool(getattr(args, "no_repo_map", False)):
            from jaunt.repo_context import api as rc_api

            repo_map_block = rc_api.repo_map_block_for_build(root=root, cfg=cfg, today=_today())
```

Then in the `await builder.run_build(...)` call (~line 1382), add the argument next
to `skills_block=skills_block,`:

```python
            repo_map_block=repo_map_block,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_tree.py tests/test_cli.py -v && uv run ruff check src/jaunt && uv run ty check src/jaunt`
Expected: PASS / clean.

- [ ] **Step 8: Add `treedocs.yaml` sidecar ignore + ensure committed map**

Append to `.gitignore` (create if needed):

```
.jaunt/
```

(`treedocs.yaml` itself is NOT ignored — it is committed.)

- [ ] **Step 9: Commit**

```bash
git add src/jaunt/repo_context/api.py src/jaunt/cli.py tests/test_cli_tree.py .gitignore
git commit -m "feat(cli): jaunt tree (+--check) and build-time repo-map injection"
```

---

### Task 9: Watcher plumbing + status drift reporting

**Files:**
- Modify: `src/jaunt/watcher.py:build_cycle_runner` (~line 127) — add `no_repo_map` to the build namespace.
- Modify: `src/jaunt/cli.py:cmd_status` — include tree drift counts.
- Test: `tests/test_cli_status.py` (extend) or `tests/test_cli_tree.py`.

**Interfaces:**
- Consumes: `repo_context.api.check_drift`.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_cli_tree.py`:

```python
def test_status_reports_tree_drift(tmp_path: Path, capsys) -> None:
    import argparse
    from jaunt.cli import cmd_status, cmd_tree

    root = _project(tmp_path)
    cmd_tree(_args(root))
    (root / "src" / "pkg" / "c.py").write_text('"""C."""\n', encoding="utf-8")
    ns = argparse.Namespace(
        root=str(root), config=None, json_output=True, jobs=None, force=False,
        target=[], no_infer_deps=False, no_progress=True, no_cache=True,
    )
    cmd_status(ns)
    out = capsys.readouterr().out
    assert "tree" in out.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_cli_tree.py::test_status_reports_tree_drift -v`
Expected: FAIL (no `tree` key in status JSON).

- [ ] **Step 3: Plumb `no_repo_map` in the watcher**

In `src/jaunt/watcher.py:build_cycle_runner`, add to the `build_args = argparse.Namespace(...)` construction:

```python
        no_repo_map=bool(getattr(args, "no_repo_map", False)),
```

- [ ] **Step 4: Add drift to `cmd_status`**

In `src/jaunt/cli.py:cmd_status`, after config is loaded and before the JSON/text
emit, compute drift and include it. Find where the status result dict/print is
assembled and add:

```python
        tree_drift = None
        if cfg.context.repo_map:
            from jaunt.repo_context import api as rc_api

            try:
                d = rc_api.check_drift(root=root, cfg=cfg)
                tree_drift = (
                    None
                    if d is None
                    else {"added": len(d.added), "removed": len(d.removed), "restaled": len(d.restaled)}
                )
            except Exception:  # noqa: BLE001
                tree_drift = None
```

Then include `"tree": tree_drift` in the status JSON payload, and in text mode print
a line when `tree_drift` is not None (e.g. `print(f"tree: {tree_drift} (run jaunt tree)")`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_tree.py tests/test_cli_status.py -v && uv run ty check src/jaunt/watcher.py src/jaunt/cli.py`
Expected: PASS / clean.

- [ ] **Step 6: Run the full suite**

Run: `uv run pytest -q`
Expected: all pass (Phase 1 complete: repo-map default-on, offline).

- [ ] **Step 7: Commit**

```bash
git add src/jaunt/watcher.py src/jaunt/cli.py tests/test_cli_tree.py
git commit -m "feat(repo-context): watcher --no-repo-map plumbing + status drift"
```

---

## Phase 2 — Build-time LLM enrichment

### Task 10: `describe.enrich()` + config wiring

**Files:**
- Modify: `src/jaunt/repo_context/describe.py` — add `enrich(...)`.
- Modify: `src/jaunt/repo_context/tree.py:sync` — accept `enrich`+`backend`; enrich restaled/added entries.
- Modify: `src/jaunt/repo_context/api.py` — pass `enrich`+backend through; honor `--enrich/--no-enrich`.
- Test: `tests/test_repo_context_describe.py` (mocked backend).

**Interfaces:**
- Produces: `enrich(items: list[tuple[str, Path]], *, backend, ast_descriptions: dict[str, str]) -> dict[str, str]` — batched; returns path→description; on any failure returns `ast_descriptions` unchanged for missing keys.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repo_context_describe.py`:

```python
import asyncio


class _FakeBackend:
    def __init__(self, mapping=None, raise_it=False):
        self._mapping = mapping or {}
        self._raise = raise_it

    async def complete_json(self, prompt: str) -> dict:
        if self._raise:
            raise RuntimeError("model down")
        return self._mapping


def test_enrich_returns_model_descriptions(tmp_path: Path) -> None:
    from jaunt.repo_context.describe import enrich

    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n", encoding="utf-8")
    backend = _FakeBackend({"src/a.py": "Authenticates a user and returns a token."})
    out = asyncio.run(
        enrich([("src/a.py", f)], backend=backend, ast_descriptions={"src/a.py": "defines foo"})
    )
    assert out["src/a.py"] == "Authenticates a user and returns a token."


def test_enrich_falls_back_on_error(tmp_path: Path) -> None:
    from jaunt.repo_context.describe import enrich

    f = tmp_path / "a.py"
    f.write_text("def foo():\n    pass\n", encoding="utf-8")
    out = asyncio.run(
        enrich([("src/a.py", f)], backend=_FakeBackend(raise_it=True), ast_descriptions={"src/a.py": "defines foo"})
    )
    assert out["src/a.py"] == "defines foo"  # AST fallback
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_describe.py -k enrich -v`
Expected: FAIL (`ImportError: cannot import name 'enrich'`).

- [ ] **Step 3: Implement `enrich()`**

Add to `src/jaunt/repo_context/describe.py`:

```python
async def enrich(
    items: list[tuple[str, Path]],
    *,
    backend,
    ast_descriptions: dict[str, str],
    head_lines: int = 40,
    max_len: int = 100,
) -> dict[str, str]:
    """Batched one-line enrichment. Falls back to ast_descriptions on any failure.

    `backend` must expose `async complete_json(prompt) -> dict[path, str]`.
    """
    result = dict(ast_descriptions)
    if not items:
        return result
    parts: list[str] = [
        "For each file below, return STRICT JSON mapping the exact path to a single "
        "concise one-line description (<= 100 chars) of what the file does. "
        "Return only the JSON object.\n"
    ]
    for rel, path in items:
        try:
            head = "\n".join(path.read_text(encoding="utf-8").splitlines()[:head_lines])
        except OSError:
            head = ""
        parts.append(f"### {rel}\nAST summary: {ast_descriptions.get(rel, '')}\n```python\n{head}\n```\n")
    try:
        raw = await backend.complete_json("\n".join(parts))
    except Exception:  # noqa: BLE001 - any failure -> AST baseline
        return result
    if not isinstance(raw, dict):
        return result
    for rel, _ in items:
        val = raw.get(rel)
        if isinstance(val, str) and val.strip():
            result[rel] = _cap(val, max_len)
    return result
```

- [ ] **Step 4: Thread enrichment through `sync` and `api`**

In `tree.sync(...)`, add params `enrich: bool = False` and `backend=None`. After the
main file loop builds `cache`/`tree` with AST descriptions, if `enrich and backend`:
collect `items = [(rel, path)]` for entries in `result.added + result.restaled`, build
`ast_descriptions` from the cache, call
`enriched = asyncio.run(describe.enrich(items, backend=backend, ast_descriptions=ast_descriptions))`
(or `await` if `sync` is made async — keep `sync` sync and use `asyncio.run` guarded by
"no running loop"; simpler: add a separate `sync_async`), update the cache records
(`enriched=True`) and re-`_insert` the enriched descriptions into `tree`.

In `api.sync_tree(...)`, resolve effective enrich = `cfg.context.enrich` unless overridden
by `--enrich`/`--no-enrich`, build the backend via the existing `_build_backend(cfg)` only
when enriching, and pass through.

> Keep the AST path 100% synchronous and offline. Only construct a backend when
> enrichment is actually on. The backend's `complete_json` is a thin wrapper over the
> existing Codex executor returning parsed JSON; if no such method exists, add a small
> adapter in `repo_context/api.py` that calls the Codex backend and `json.loads` the
> result, returning `{}` on parse failure (which triggers the AST fallback).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_repo_context_describe.py tests/test_repo_context_tree.py -v && uv run ty check src/jaunt/repo_context`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/jaunt/repo_context/describe.py src/jaunt/repo_context/tree.py src/jaunt/repo_context/api.py tests/test_repo_context_describe.py
git commit -m "feat(repo-context): build-time LLM enrichment (cached, AST fallback)"
```

---

## Phase 3 — colgrep retrieval (opt-in)

### Task 11: `repo_context/search.py` — colgrep wrapper

**Files:**
- Create: `src/jaunt/repo_context/search.py`
- Test: `tests/test_repo_context_search.py` (subprocess + `shutil.which` mocked)

**Interfaces:**
- Produces: `available() -> bool`; `ensure_index(root: Path) -> bool`; `@dataclass Hit(file: str, snippet: str, score: float)`; `query(text: str, *, root: Path, max_hits: int = 8, timeout: float = 5.0) -> list[Hit]`; `render_relevant_block(hits: list[Hit]) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_repo_context_search.py`:

```python
import json
from pathlib import Path

import jaunt.repo_context.search as search


def test_available_false_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: None)
    assert search.available() is False


def test_query_parses_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    class _CP:
        returncode = 0
        stdout = json.dumps(
            [{"unit": {"file": "src/a.py", "snippet": "def f(): ..."}, "score": 0.9}]
        )
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _CP())
    hits = search.query("auth token", root=tmp_path, max_hits=8)
    assert len(hits) == 1 and hits[0].file == "src/a.py"


def test_query_timeout_returns_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    def _boom(*a, **k):
        raise search.subprocess.TimeoutExpired(cmd="colgrep", timeout=5)

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search.query("x", root=tmp_path) == []


def test_query_malformed_json_returns_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    class _CP:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _CP())
    assert search.query("x", root=tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repo_context_search.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement `search.py`**

Create `src/jaunt/repo_context/search.py`:

```python
"""colgrep (LightOn next-plaid) wrapper. Every failure degrades to no hits."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class Hit:
    file: str
    snippet: str
    score: float


def available() -> bool:
    return shutil.which("colgrep") is not None


def ensure_index(root: Path) -> bool:
    if not available():
        return False
    try:
        subprocess.run(
            ["colgrep", "init", str(root)],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def query(text: str, *, root: Path, max_hits: int = 8, timeout: float = 5.0) -> list[Hit]:
    if not available() or not text.strip():
        return []
    try:
        cp = subprocess.run(
            ["colgrep", "--json", "--k", str(max_hits), text],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if cp.returncode != 0 or not cp.stdout.strip():
        return []
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return []
    rows = data if isinstance(data, list) else data.get("results", [])
    hits: list[Hit] = []
    for row in rows:
        unit = row.get("unit", row) if isinstance(row, dict) else {}
        file = str(unit.get("file", "")) if isinstance(unit, dict) else ""
        snippet = str(unit.get("snippet", "")) if isinstance(unit, dict) else ""
        score = float(row.get("score", 0.0)) if isinstance(row, dict) else 0.0
        if file:
            hits.append(Hit(file=file, snippet=snippet, score=score))
    # Deterministic ordering: score desc, then file asc.
    hits.sort(key=lambda h: (-h.score, h.file))
    return hits[:max_hits]


def render_relevant_block(hits: list[Hit]) -> str:
    if not hits:
        return ""
    return "Read `_context/relevant_*.py` for related existing code in the repository."
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_repo_context_search.py -v && uv run ruff check src/jaunt/repo_context/search.py && uv run ty check src/jaunt/repo_context/search.py`
Expected: PASS / clean.

- [ ] **Step 5: Commit**

```bash
git add src/jaunt/repo_context/search.py tests/test_repo_context_search.py
git commit -m "feat(repo-context): colgrep search wrapper with graceful fallback"
```

---

### Task 12: Retrieval into the generation workspace

**Files:**
- Modify: `src/jaunt/generate/codex_backend.py:generate_module` — write `_context/relevant_*.py` from `ctx.relevant_context_block` payload.
- Modify: `src/jaunt/generate/base.py` — change `relevant_context_block` semantics to carry both the snippet files and the prompt pointer (use a small structured field) OR keep the pointer string in `relevant_context_block` and add `relevant_context_files: tuple[tuple[str, str], ...] = ()` (filename, content).
- Modify: `src/jaunt/builder.py:_component_payload` — when search enabled, query colgrep per-spec and populate the fields.
- Modify: `src/jaunt/cli.py:_cmd_build_async` — `ensure_index` when search enabled; pass `search_cfg` into `run_build`.
- Test: `tests/test_codex_backend.py` (writes `_context/relevant_*.py`), `tests/test_repo_context_search.py`.

**Interfaces:**
- Produces: `ModuleSpecContext.relevant_context_files: tuple[tuple[str, str], ...] = ()` (filename → content). `relevant_context_block` stays the prompt pointer string (already added in Task 6; already in the cache key).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_codex_backend.py`:

```python
def test_generate_writes_relevant_context_files(tmp_path, monkeypatch) -> None:
    # Capture the _context dir contents by stubbing run_codex_exec.
    import jaunt.generate.codex_backend as cb

    written: dict[str, str] = {}

    async def _fake_run(*, prompt, cwd, **kw):
        ctx_dir = Path(cwd) / "_context"
        for p in ctx_dir.glob("relevant_*.py"):
            written[p.name] = p.read_text(encoding="utf-8")
        # write the target so generate_module can read it back
        # (locate the single .py outside _context)
        for p in Path(cwd).rglob("*.py"):
            if "_context" not in p.parts:
                p.write_text("x = 1\n", encoding="utf-8")
        class _R:  # minimal result
            usage = None
        return _R()

    monkeypatch.setattr(cb, "run_codex_exec", _fake_run)
    backend = _make_backend()
    ctx = _make_build_ctx(
        relevant_context_block="Read `_context/relevant_*.py` ...",
        relevant_context_files=(("relevant_0.py", "# src/a.py\ndef f(): ...\n"),),
    )
    import asyncio
    asyncio.run(backend.generate_module(ctx))
    assert "relevant_0.py" in written and "def f()" in written["relevant_0.py"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_codex_backend.py -k relevant_context -v`
Expected: FAIL (`TypeError: unexpected keyword argument 'relevant_context_files'`).

- [ ] **Step 3: Add the field**

In `src/jaunt/generate/base.py`, add to `ModuleSpecContext`:

```python
    relevant_context_files: tuple[tuple[str, str], ...] = ()
```

- [ ] **Step 4: Write the files in `generate_module`**

In `src/jaunt/generate/codex_backend.py:generate_module`, after the dep-writing loop
(after line 330, where `dep_*.pyi` are written), add:

```python
            for name, content in getattr(ctx, "relevant_context_files", ()) or ():
                (ctx_dir / name).write_text(content, encoding="utf-8")
```

(The prompt pointer `relevant_context_block` is already appended last by `_build_prompt`
from Task 6.)

- [ ] **Step 5: Populate retrieval in `_component_payload`**

In `src/jaunt/builder.py`, give `run_build` two more keyword params:
`search_enabled: bool = False`, `search_max_hits: int = 8`. In `_component_payload`,
before constructing `ModuleSpecContext`, add:

```python
            relevant_block = ""
            relevant_files: tuple[tuple[str, str], ...] = ()
            if search_enabled:
                from jaunt.repo_context import search as rc_search

                query_text = " ".join(component_expected) + " " + " ".join(
                    decorator_prompts.values()
                )
                hits = rc_search.query(query_text, root=package_dir, max_hits=search_max_hits)
                if hits:
                    relevant_files = tuple(
                        (f"relevant_{i}.py", f"# {h.file}\n{h.snippet}\n")
                        for i, h in enumerate(hits)
                    )
                    relevant_block = rc_search.render_relevant_block(list(hits))
```

Then add to the `ModuleSpecContext(...)` constructor:

```python
                relevant_context_block=relevant_block,
                relevant_context_files=relevant_files,
```

- [ ] **Step 6: Wire search config in `_cmd_build_async`**

In `src/jaunt/cli.py:_cmd_build_async`, after computing `repo_map_block` (Task 8 Step 6),
add index init, and pass the flags into `run_build`:

```python
        if cfg.context.search.enabled:
            from jaunt.repo_context import search as rc_search

            rc_search.ensure_index(package_dir if (package_dir := next((d for d in source_dirs if d.exists()), root)) else root)
```

(Place this AFTER `package_dir` is resolved later in the function; simplest is to add
`ensure_index(package_dir)` right before the `run_build(...)` call.) Then in the
`run_build(...)` call add:

```python
            search_enabled=cfg.context.search.enabled and cfg.context.search.internal_retrieval,
            search_max_hits=cfg.context.search.max_hits,
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run pytest tests/test_codex_backend.py tests/test_repo_context_search.py -v && uv run ty check src/jaunt`
Expected: PASS / clean.

- [ ] **Step 8: Full suite + lint/type gate**

Run: `uv run pytest -q && uv run ruff check . && uv run ty check`
Expected: all green.

- [ ] **Step 9: Commit**

```bash
git add src/jaunt/generate/base.py src/jaunt/generate/codex_backend.py src/jaunt/builder.py src/jaunt/cli.py tests/test_codex_backend.py
git commit -m "feat(repo-context): colgrep retrieval seeded into _context/relevant_*.py"
```

---

## Final verification

- [ ] **Run the full suite offline:** `uv run pytest -q` — all pass, no network/binary needed.
- [ ] **Lint + types:** `uv run ruff check . && uv run ty check` — clean.
- [ ] **Default-on smoke (offline, AST-only):** in an example project, `uv run --project ../.. jaunt tree` writes `treedocs.yaml`; `jaunt tree --check` exits 0; touch a file → `--check` exits 4.
- [ ] **Feature-off parity:** with `[context] repo_map = false`, confirm prompts and cache keys are unchanged vs. `main` (a cache key test with default `_make_ctx()` still matches the pre-change key).
- [ ] **Docs:** update `CLAUDE.md` (CLI table + config section) and `jaunt.toml` example to document `jaunt tree`, `[context]`, and `[context.search]`. Commit as `docs: document repo-context subsystem`.

## Self-review notes (coverage vs spec)

- Tree artifact + treedocs v0.2.0 format → Tasks 4, 8 (`api.sync_tree`, `TreeDoc.write`).
- Two staleness kinds (signature + source digests) → Tasks 2, 4 (`TreeCache`, `signature()`, `is_drifted`/`check_drift`).
- AST baseline + build-time enrichment + AST fallback → Tasks 3, 10.
- Injection + cache-key (not freshness digest) → Tasks 6, 7 (mirrors `skills_block`).
- Prompt-cache hygiene (no volatile text) → Task 5 (`render_repo_map` excludes `last_updated`/signature) + Task 8 (`write` skips churn).
- Atomic write + lock → Task 4 (`_lock`, `os.replace`).
- colgrep pre-computed retrieval into `_context/relevant_*.py`, no agent shell-out, graceful fallback, deterministic ordering → Tasks 11, 12.
- CLI `jaunt tree`/`--check` (exit 4), `--no-repo-map`, watcher + status → Tasks 8, 9.
- Single-root v1; per-directory deferred to v2 (documented, not implemented) → matches spec non-goals.
