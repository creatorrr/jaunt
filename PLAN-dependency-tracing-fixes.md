# Plan: Fix Dependency Tracing Issues in Jaunt

This plan addresses all identified efficiency and effectiveness issues in jaunt's
dependency tracing system. Changes are grouped into independent workstreams
ordered by impact, with each item specifying the files affected, the change, and
any risks.

---

## Workstream 1: Caching & Performance

### 1A. Scope discovery to `--target` builds

**Problem:** `discover_modules()` walks the entire source tree even when the user
passes `--target some.module`. Every `.py` file is visited and converted to a
module name before the target filter is applied in the CLI.

**Files:** `discovery.py`, `cli.py`

**Change:**
- Add an optional `target_modules: set[str] | None` parameter to
  `discover_modules()`.
- When `target_modules` is provided, compute the set of expected file paths from
  the target module names and only verify those paths exist, skipping the
  recursive `rglob("*.py")`.
- Fall back to full scan when targets have transitive deps outside the target
  set (needed for graph construction). For that case, add a two-phase approach:
  1. Fast-path: resolve target paths directly.
  2. Discover only the *additional* modules referenced as deps of the targets
     (requires a lightweight import of targets first to read decorator kwargs,
     then selective discovery of dep modules).
- Update `cli.py` to pass `target_modules` through to discovery.

**Risk:** Medium — must handle the case where a target depends on modules outside
the target set. The two-phase approach adds complexity but avoids breaking the
dependency graph. Needs integration tests with `--target` and cross-module deps.

**Tests:** Add cases to `test_discovery.py` for targeted discovery; add
integration test confirming `--target` with deps across modules.

---

### 1B. Persistent AST parse cache across CLI invocations

**Problem:** Every `jaunt build` invocation re-parses every source file via
`ast.parse()`. The current `_parse_module_once` cache in `deps.py` is a local
dict scoped to a single `build_spec_graph()` call. The same file is also parsed
independently in `digest.py:extract_source_segment()`.

**Files:** `deps.py`, `digest.py`, new file `src/jaunt/parse_cache.py`

**Change:**
- Create a shared `ParseCache` class in a new `parse_cache.py` module:
  ```python
  class ParseCache:
      def __init__(self, cache_dir: Path):
          self._cache_dir = cache_dir  # e.g. .jaunt/cache/ast

      def get_parsed(self, path: str) -> tuple[str, ast.Module] | None:
          """Return (source, tree) from disk cache if file mtime+size match."""

      def parse(self, path: str) -> tuple[str, ast.Module]:
          """Parse and cache. Cache key = (path, mtime_ns, size)."""
  ```
- Cache format: store pickled AST + source hash in
  `.jaunt/cache/ast/<sha256(path)>.pickle` keyed by `(mtime_ns, st_size)`.
- Thread the `ParseCache` instance through `build_spec_graph()` and
  `extract_source_segment()` so both share the same parsed ASTs.
- Add a `jaunt cache clear` CLI subcommand.

**Risk:** Low-medium — pickled ASTs are Python-version-specific. Include Python
version in the cache key. Invalidation is by mtime+size which is standard
(same strategy as `pyc` files).

**Tests:** Unit tests for `ParseCache` round-tripping; test that stale cache
entries are evicted on source change.

---

### 1C. Cache `packages_distributions()` across builds

**Problem:** `external_imports.py` calls `metadata.packages_distributions()` on
every build. This is a moderately expensive call that iterates all installed
distributions.

**Files:** `external_imports.py`

**Change:**
- Cache the result in a module-level `_packages_dist_cache` with a TTL
  (e.g., 60 seconds within a single process) or, for cross-invocation caching,
  write to `.jaunt/cache/pkg_distributions.json` keyed by a hash of
  `sys.path + list of dist names`.
- For the simpler in-process case (sufficient since `jaunt build` is a
  single invocation): just move the call outside the per-module loop to a
  top-level variable. **This is actually already done** — `pkg_to_dists` is
  computed once at line 174. The real cost is the `metadata.version()` call
  inside `_resolve_dist_by_name_heuristic()` which is called per import.
- Memoize `_resolve_dist_by_name_heuristic()` with `@functools.lru_cache`.

**Risk:** Low — `lru_cache` on a pure function with string input is safe.

**Tests:** Verify memoization by mocking `metadata.version` and asserting call
count.

---

### 1D. Parallelize PyPI skill generation

**Problem:** `ensure_pypi_skills_and_block()` generates skills sequentially:
for each missing dist, it fetches the PyPI README then calls the LLM. With many
dependencies this is slow.

**Files:** `skills_auto.py`

**Change:**
- Collect all dists that `needs_generate` into a list first.
- Use `asyncio.gather()` (or `asyncio.Semaphore`-bounded tasks) to fetch
  READMEs and generate skills concurrently, respecting a max-concurrency limit
  (e.g., 4).
- Structure:
  ```python
  async def _generate_one(dist, version, generator, project_root):
      readme, readme_type = fetch_readme(dist, version)
      md = await generator.generate_skill_markdown(...)
      _atomic_write_text(skill_md_path(...), _format_generated_skill_file(...))

  tasks = [_generate_one(d, v, gen, root) for d, v in needs]
  results = await asyncio.gather(*tasks, return_exceptions=True)
  ```
- Keep the existing best-effort error handling: exceptions become warnings.

**Risk:** Low — the operations are already async-compatible (`fetch_readme` is
sync but can be wrapped with `asyncio.to_thread`). The LLM call is already
async.

**Tests:** Extend `test_skills_auto.py` with a test that verifies concurrent
generation of multiple skills.

---

## Workstream 2: Inference Accuracy

### 2A. Support nested specs in inference

**Problem:** `deps.py:158` skips inference entirely when `"." in entry.qualname`
(e.g., `Outer.inner_method`). This means nested class methods or inner
definitions never get inferred deps.

**Files:** `deps.py`

**Change:**
- Instead of skipping, split `entry.qualname` on `"."` and walk the AST
  to find the nested node:
  ```python
  def _find_nested_node(tree: ast.Module, qualname: str) -> ast.AST | None:
      parts = qualname.split(".")
      node: ast.AST = tree
      for part in parts:
          node = _find_child_node(node, name=part)
          if node is None:
              return None
      return node
  ```
- Replace the `_find_top_level_node` call with `_find_nested_node` and remove
  the early `continue` for dotted qualnames.
- Still wrap in try/except for best-effort behavior.

**Risk:** Low — this is additive (currently these specs get zero inferred deps).
Worst case is a false positive dep, which is already handled gracefully.

**Tests:** Add test case to `test_deps.py` with a class containing methods that
reference other specs.

---

### 2B. Support multi-level attribute chains

**Problem:** `_NameUseCollector` only captures `alias.Foo` (single-level
attribute access). Patterns like `alias.sub.Foo` are missed because only
`ast.Name` roots with one `ast.Attribute` level are tracked.

**Files:** `deps.py`

**Change:**
- Extend `visit_Attribute` to walk the full chain and collect
  `(root_name, [attr1, attr2, ...])`:
  ```python
  def _resolve_attr_chain(node: ast.Attribute) -> tuple[str, list[str]] | None:
      attrs = [node.attr]
      current = node.value
      while isinstance(current, ast.Attribute):
          attrs.append(current.attr)
          current = current.value
      if isinstance(current, ast.Name):
          return current.id, list(reversed(attrs))
      return None
  ```
- In the resolution phase, try progressively longer module paths:
  for `alias.sub.Foo` where `alias` maps to `pkg`, try
  `pkg.sub:Foo`, then `pkg:sub.Foo`.

**Risk:** Low — additive inference. May increase false positives slightly but
the existing `if candidate in specs` check limits this.

**Tests:** Add test with multi-level attribute references to `test_deps.py`.

---

### 2C. Follow module re-exports (one level)

**Problem:** If `pkg/__init__.py` re-exports `from pkg.internal import Foo`, and
a spec does `from pkg import Foo`, the inference resolves to `pkg:Foo` which
doesn't match the real spec at `pkg.internal:Foo`.

**Files:** `deps.py`

**Change:**
- After initial resolution fails to find a candidate in `specs`, add a
  fallback: parse the target module's `__init__.py` and check if the name is
  re-exported from a submodule.
- Limit to one level of indirection to avoid performance issues.
- Implementation:
  ```python
  def _resolve_reexport(module: str, name: str, *, cache: dict) -> SpecRef | None:
      init_path = _module_to_init_path(module)  # needs source_roots
      parsed = _parse_module_once(init_path, cache=cache)
      if parsed and name in parsed.from_imports:
          return normalize_spec_ref(parsed.from_imports[name])
      return None
  ```
- Thread `source_roots` into `build_spec_graph()` so we can locate init files.

**Risk:** Medium — requires knowing source roots to find `__init__.py` files.
Adds a new parameter to `build_spec_graph()`. The one-level limit keeps cost
bounded.

**Tests:** Add test with package re-exports to `test_deps.py`.

---

## Workstream 3: Correctness & Architecture

### 3A. Pass dependency API context to the LLM backend

**Problem:** `builder.py:276-277` passes empty dicts for `dependency_apis` and
`dependency_generated_modules`. The LLM generating a spec's implementation has
no visibility into what its dependencies look like, reducing generation quality.

**Files:** `builder.py`, `generate/base.py` (ModuleSpecContext)

**Change:**
- After a dependency module is successfully generated, read its source and
  store it in a dict keyed by module name.
- Before generating a module, collect its dependency modules' generated sources
  and spec sources, and populate `dependency_apis` and
  `dependency_generated_modules` in the `ModuleSpecContext`.
- Implementation in `run_build`:
  ```python
  generated_sources: dict[str, str] = {}  # module_name -> generated source

  async def build_one(module_name: str) -> ...:
      # Collect dependency context
      dep_apis: dict[SpecRef, str] = {}
      dep_gen_modules: dict[str, str] = {}
      for dep_mod in module_dag.get(module_name, set()):
          if dep_mod in generated_sources:
              dep_gen_modules[dep_mod] = generated_sources[dep_mod]
          for entry in module_specs.get(dep_mod, []):
              dep_apis[entry.spec_ref] = extract_source_segment(entry)

      ctx = ModuleSpecContext(
          ...
          dependency_apis=dep_apis,
          dependency_generated_modules=dep_gen_modules,
          ...
      )
      result = await backend.generate_with_retry(ctx)
      if ok:
          generated_sources[module_name] = result.source
      ...
  ```
- The topological build order guarantees dependencies are generated before
  dependents, so `generated_sources` will be populated when needed.

**Risk:** Medium — increases prompt size for the LLM, which may increase cost
and latency. Consider adding a config flag `inject_dependency_context: bool`
(default `true`) and/or truncating very large dependency sources. Needs
careful testing to ensure prompt quality improves rather than degrades.

**Tests:** Add integration test verifying that `dependency_apis` and
`dependency_generated_modules` are non-empty when deps exist. Mock the backend
and assert the context contents.

---

### 3B. Respect configured `generated_dir` in runtime

**Problem:** `runtime.py:49` hardcodes `generated_dir="__generated__"`. If the
user configures a different `generated_dir` in `jaunt.toml`, the builder writes
files to the right place but the runtime forwarder looks in the wrong place.

**Files:** `runtime.py`, `config.py`

**Change:**
- At import time, the runtime doesn't have access to parsed config. Two
  approaches:
  1. **Environment variable approach** (simpler): Have the CLI set
     `JAUNT_GENERATED_DIR` env var before importing spec modules. The runtime
     reads `os.environ.get("JAUNT_GENERATED_DIR", "__generated__")`.
  2. **Module-level config approach**: Add a `jaunt.configure(generated_dir=...)`
     function that sets a module-level default. Users call it in their package
     `__init__.py` or the CLI calls it before discovery.
- Recommend approach (1) for simplicity — the env var is set once by the CLI
  and inherited by all subprocesses (including pytest).
- Implementation:
  ```python
  # runtime.py
  def _import_generated_module(spec_module: str) -> ModuleType:
      gen_dir = os.environ.get("JAUNT_GENERATED_DIR", "__generated__")
      generated = spec_module_to_generated_module(spec_module, generated_dir=gen_dir)
      return importlib.import_module(generated)
  ```
  ```python
  # cli.py (in both build and test commands, before import_and_collect)
  os.environ["JAUNT_GENERATED_DIR"] = config.paths.generated_dir
  ```

**Risk:** Low — backward-compatible (default is still `__generated__`). Env var
approach is simple and works across subprocesses.

**Tests:** Add test to `test_magic_decorator.py` that sets the env var and
verifies forwarding uses it. Add CLI test confirming the var is set.

---

### 3C. Pre-build cycle detection with actionable errors

**Problem:** Dependency cycles are only detected during `toposort()` at build
time, after all modules have been imported and all specs registered. For large
projects, this is a late and confusing failure point.

**Files:** `deps.py`, `cli.py`

**Change:**
- Add a `validate_graph(graph)` function to `deps.py` that checks for cycles
  and returns a list of cycle paths rather than raising:
  ```python
  def find_cycles(graph: dict[K, set[K]]) -> list[list[K]]:
      """Return all distinct cycles in the graph, or empty if acyclic."""
  ```
- Call `find_cycles()` in the CLI immediately after `build_spec_graph()` and
  before `run_build()`. Print clear diagnostics:
  ```
  error: dependency cycle detected
    module_a:FuncX -> module_b:FuncY -> module_a:FuncX
  hint: break the cycle by removing a dep from one of these specs
  ```
- This doesn't change the existing `toposort()` behavior (it still raises),
  but adds an earlier, more informative check.

**Risk:** Low — purely additive diagnostic. The existing toposort cycle
detection remains as a safety net.

**Tests:** Add test for `find_cycles()` with various cycle shapes (self-loop,
two-node, multi-node, multiple independent cycles).

---

## Workstream 4: Low-Priority / Optional

### 4A. Metaclass support for `@magic` classes

**Problem:** `runtime.py:97` rejects classes with `type(obj) is not type`.
This blocks usage with dataclasses using custom metaclasses, ABCs, etc.

**Files:** `runtime.py`

**Change:**
- Relax the check to allow known-safe metaclasses (`ABCMeta`, etc.) or remove
  it entirely. The constraint exists because import-time substitution creates a
  new class with `type(name, (), {...})`, which uses `type` as the metaclass.
- For full support, use the original metaclass when creating the fallback:
  ```python
  metacls = type(obj)
  return metacls(name, obj.__bases__, {"__module__": module, ...})
  ```
- This requires careful handling of metaclass `__new__`/`__init__` signatures.

**Risk:** Medium — metaclass constructors may have side effects or require
specific arguments. Start by supporting `ABCMeta` only (common case), then
generalize.

**Tests:** Add test with `ABC` base class and `ABCMeta` metaclass.

---

### 4B. Warn on unresolved inferred deps (opt-in verbose mode)

**Problem:** Inference failures are completely silent. Users have no way to know
if inferred deps were missed, which can lead to stale builds or incorrect
generation order.

**Files:** `deps.py`, `cli.py`

**Change:**
- Add a `warnings` accumulator to `build_spec_graph()` that collects
  unresolvable references:
  ```python
  @dataclass
  class DepGraphResult:
      graph: dict[SpecRef, set[SpecRef]]
      warnings: list[str]
  ```
- In verbose mode (`-v` flag), print these warnings to stderr.
- In normal mode, suppress them (preserving current behavior).

**Risk:** Low — purely diagnostic, opt-in.

**Tests:** Add test asserting warnings are collected for unresolvable names.

---

## Implementation Order

| Phase | Items | Estimated Scope | Dependencies |
|-------|-------|----------------|--------------|
| 1     | 1C, 3B, 3C | Small, standalone fixes | None |
| 2     | 2A, 2B, 4B | Inference improvements | None |
| 3     | 3A | LLM context injection | Needs 1B for perf |
| 4     | 1B, 1D | Caching infrastructure | None |
| 5     | 1A, 2C | Larger refactors | 1B for parse cache |
| 6     | 4A | Optional metaclass support | None |

Phase 1 items are the highest-value, lowest-risk changes. Phase 2 improves
inference accuracy. Phases 3-5 add infrastructure. Phase 6 is optional.

---

## Test Strategy

Every change includes unit tests in the corresponding `test_*.py` file. For
workstreams that touch multiple modules (1A, 1B, 3A), add or extend integration
tests in `test_integration.py`. Run the full suite after each phase:

```bash
uv run pytest tests/ -x -q
```

Verify no regressions in existing digest stability (critical for incremental
rebuilds) by running `test_digest.py` after any change to parsing or graph
construction.
