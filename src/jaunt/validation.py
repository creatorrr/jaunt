"""Fearful symmetry: AST validation of generated code.

Could frame thy fearful symmetry? -- verify that what the furnace produced
has the right shape.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path

from jaunt.class_analysis import is_stub_body
from jaunt.external_imports import pep503_normalize

_SKIP_LOCAL_DIRS = {
    ".cache",
    ".git",
    ".jaunt",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "site-packages",
    "venv",
}


def _syntax_error_to_str(err: SyntaxError) -> str:
    # Keep formatting stable and readable for retry prompts.
    lineno = getattr(err, "lineno", None)
    offset = getattr(err, "offset", None)
    loc = ""
    if lineno is not None:
        loc = f" (line {lineno}"
        if offset is not None:
            loc += f":{offset}"
        loc += ")"
    msg = getattr(err, "msg", None) or str(err) or "invalid syntax"
    return f"SyntaxError: {msg}{loc}"


def validate_generated_source(source: str, expected_names: list[str]) -> list[str]:
    """Validate generated Python source.

    Checks:
    - parses via `ast.parse` (syntax errors)
    - verifies required *top-level* names exist:
      - function defs (sync + async)
      - class defs
      - simple assignments (`NAME = ...` and `NAME: T = ...`)
    """

    if expected_names is None:
        expected_names = []

    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]

    defined: set[str] = set()
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            continue

        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined.add(tgt.id)
            continue

        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            continue

    errors: list[str] = []
    for name in expected_names:
        if name not in defined:
            errors.append(f"Missing top-level definition: {name}")

    return errors


def validate_build_generated_source(
    source: str,
    expected_names: list[str],
    *,
    spec_module: str,
    handwritten_names: Iterable[str],
    generated_module: str | None = None,
    project_dir: Path | None = None,
    source_roots: Sequence[Path] | None = None,
    first_party_modules: Iterable[str] = (),
    check_imports: bool = False,
    import_allowlist: Iterable[str] = (),
) -> list[str]:
    errors, mod = _base_validation(source, expected_names)
    if mod is None:
        return errors

    errors.extend(
        _validate_build_contract_only(
            mod,
            expected_names=expected_names,
            spec_module=spec_module,
            handwritten_names=handwritten_names,
            generated_module=generated_module,
        )
    )
    protected_modules = {spec_module, spec_module.split(".", 1)[0]}
    protected_modules |= set(first_party_modules)
    protected_modules |= _first_party_top_levels(
        project_dir=project_dir or Path.cwd(),
        source_roots=source_roots or (),
        configured=first_party_modules,
    )
    errors.extend(validate_no_import_fallbacks(mod, protected_modules))
    if check_imports:
        errors.extend(
            _validate_generated_import_provenance(
                mod,
                generated_module=generated_module or spec_module,
                project_dir=project_dir or Path.cwd(),
                source_roots=source_roots or (),
                first_party_modules=first_party_modules,
                allowlist=import_allowlist,
            )
        )
    return errors


def validate_build_contract_only(
    source: str,
    *,
    expected_names: list[str],
    spec_module: str,
    handwritten_names: Iterable[str],
    generated_module: str | None = None,
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    return _validate_build_contract_only(
        mod,
        expected_names=expected_names,
        spec_module=spec_module,
        handwritten_names=handwritten_names,
        generated_module=generated_module,
    )


def _validate_build_contract_only(
    mod: ast.Module,
    *,
    expected_names: list[str],
    spec_module: str,
    handwritten_names: Iterable[str],
    generated_module: str | None = None,
) -> list[str]:
    errors: list[str] = []
    expected = set(expected_names)
    forbidden = set(handwritten_names) - set(expected_names)

    if forbidden:
        for name in _defined_top_level_names(mod):
            if name in forbidden:
                errors.append(
                    "Generated source must not redefine handwritten source-module symbol "
                    f"{name!r}. Import or reuse {name!r} from {spec_module!r} instead."
                )

    for node in ast.walk(mod):
        if not isinstance(node, ast.ImportFrom):
            continue
        level = int(getattr(node, "level", 0) or 0)
        if level == 0:
            resolved = node.module
        else:
            resolved = _resolve_relative_import(generated_module, level, node.module)
        if resolved != spec_module:
            continue
        for alias in node.names:
            if alias.name == "*":
                errors.append(
                    f"generated module re-imports its own spec module {spec_module} via "
                    "'from ... import *'; define its symbols instead"
                )
            elif alias.name in expected:
                errors.append(
                    f"generated module re-imports its own spec symbol {alias.name!r} "
                    f"from {spec_module}; define it instead"
                )
    return errors


def _resolve_relative_import(
    generated_module: str | None, level: int, module: str | None
) -> str | None:
    """Resolve a relative ``from`` import inside the generated module to an absolute name.

    ``generated_module`` is the dotted name of the module the generated source lives in
    (e.g. ``pkg.__generated__.mod``); ``level`` is the ImportFrom dot count. Returns the
    absolute module the import targets, or ``None`` when it cannot be resolved (unknown
    generated module or dots that walk past the top-level package).
    """
    if not generated_module or level <= 0:
        return None
    parts = generated_module.split(".")
    anchor = parts[: len(parts) - level]
    if not anchor:
        return None
    base = ".".join(anchor)
    if module:
        return f"{base}.{module}"
    return base


def validate_no_import_fallbacks(tree: ast.AST, protected_modules: set[str]) -> list[str]:
    protected_top = {m.split(".", 1)[0] for m in protected_modules if m}
    errors: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        if not any(_handler_catches_import_error(handler) for handler in node.handlers):
            continue
        for import_node, name in _protected_imports_in_try_body(
            node,
            protected_modules,
            protected_top,
        ):
            lineno = getattr(import_node, "lineno", "?")
            errors.append(
                f"generated module wraps a guarded import of {name!r} (line {lineno}) "
                "in try/except — no import fallbacks: import failures must raise, never "
                "provide a divergent fallback implementation."
            )
    return errors


def _handler_catches_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return True
    return any(
        name in {"ImportError", "ModuleNotFoundError", "Exception", "BaseException"}
        for name in _caught_type_names(handler.type)
    )


def _caught_type_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, ast.Attribute):
        return {node.attr}
    if isinstance(node, ast.Tuple):
        names: set[str] = set()
        for elt in node.elts:
            names.update(_caught_type_names(elt))
        return names
    return set()


def _protected_imports_in_try_body(
    node: ast.Try,
    protected_modules: set[str],
    protected_top: set[str],
) -> list[tuple[ast.Import | ast.ImportFrom, str]]:
    found: list[tuple[ast.Import | ast.ImportFrom, str]] = []
    stack: list[ast.AST] = list(node.body)
    while stack:
        current = stack.pop()
        if isinstance(current, ast.Import):
            for alias in current.names:
                top = alias.name.split(".", 1)[0]
                if top in protected_top:
                    found.append((current, alias.name))
            continue
        if isinstance(current, ast.ImportFrom):
            level = int(getattr(current, "level", 0) or 0)
            if level > 0:
                found.append((current, _import_from_name(current)))
                continue
            module = current.module or ""
            top = module.split(".", 1)[0]
            if module in protected_modules or top in protected_top:
                found.append((current, _import_from_name(current)))
            continue
        if isinstance(current, ast.Try):
            stack.extend(reversed(current.body))
            continue
        stack.extend(reversed(list(ast.iter_child_nodes(current))))
    return found


def _import_from_name(node: ast.ImportFrom) -> str:
    module = "." * int(getattr(node, "level", 0) or 0) + (node.module or "")
    names = ", ".join(alias.name for alias in node.names)
    if not module:
        return names
    if names:
        return f"{module}.{names}"
    return module


def validate_generated_import_provenance(
    source: str,
    *,
    generated_module: str,
    project_dir: Path,
    source_roots: Sequence[Path] | None = None,
    first_party_modules: Iterable[str] = (),
    allowlist: Iterable[str] = (),
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    return _validate_generated_import_provenance(
        mod,
        generated_module=generated_module,
        project_dir=project_dir,
        source_roots=source_roots or (),
        first_party_modules=first_party_modules,
        allowlist=allowlist,
    )


def _validate_generated_import_provenance(
    mod: ast.Module,
    *,
    generated_module: str,
    project_dir: Path,
    source_roots: Sequence[Path],
    first_party_modules: Iterable[str],
    allowlist: Iterable[str],
) -> list[str]:
    stdlib = getattr(sys, "stdlib_module_names", set())
    allowed_first_party = _first_party_top_levels(
        project_dir=project_dir,
        source_roots=source_roots,
        configured=first_party_modules,
    )
    allowlist_norm = {pep503_normalize(name) for name in allowlist if name.strip()}
    declared_dists = _declared_project_dependencies(_find_pyproject(project_dir))

    importlib_aliases, direct_dynamic_names = _importlib_dynamic_bindings(mod)

    errors: list[str] = []
    errors.extend(
        _nonconstant_dynamic_import_errors(
            mod,
            generated_module=generated_module,
            importlib_aliases=importlib_aliases,
            direct_dynamic_names=direct_dynamic_names,
        )
    )

    for imported in sorted(
        _top_level_imports(
            mod,
            importlib_aliases=importlib_aliases,
            direct_dynamic_names=direct_dynamic_names,
        )
    ):
        top = imported.split(".", 1)[0]
        if not top:
            continue
        if top in stdlib:
            continue
        if top in allowed_first_party:
            continue
        if pep503_normalize(top) in allowlist_norm:
            continue
        if _import_resolves_to_declared_dependency(top, declared_dists=declared_dists):
            continue

        errors.append(
            f"Generated module {generated_module!r} imports undeclared package {top!r}. "
            "Add it to [project.dependencies], make it a first-party module, or add it to "
            "build.generated_import_allowlist if intentional."
        )
    return errors


def _importlib_dynamic_bindings(mod: ast.Module) -> tuple[set[str], set[str]]:
    """Resolve the names in this module that can perform a dynamic import.

    Returns ``(importlib_aliases, direct_dynamic_names)``:
    - ``importlib_aliases``: names bound to the ``importlib`` module, used for
      attribute calls like ``X.import_module(...)`` / ``X.__import__(...)`` —
      seeded with ``"importlib"`` and extended by ``import importlib as X``.
    - ``direct_dynamic_names``: names that are themselves dynamic-import
      callables, used for ``X(...)`` — seeded with the always-available builtin
      ``__import__`` and extended by ``from importlib import import_module as X``.
    """
    importlib_aliases: set[str] = {"importlib"}
    direct_dynamic_names: set[str] = {"__import__"}
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or "importlib")
        elif isinstance(node, ast.ImportFrom):
            if int(getattr(node, "level", 0) or 0) > 0:
                continue
            if node.module == "importlib":
                for alias in node.names:
                    if alias.name in {"import_module", "__import__"}:
                        direct_dynamic_names.add(alias.asname or alias.name)
    return importlib_aliases, direct_dynamic_names


def _top_level_imports(
    mod: ast.Module,
    *,
    importlib_aliases: set[str],
    direct_dynamic_names: set[str],
) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(mod):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    imports.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if int(getattr(node, "level", 0) or 0) > 0:
                continue
            if node.module:
                imports.add(node.module.split(".", 1)[0])
        elif isinstance(node, ast.Call):
            dynamic_import = _constant_dynamic_import_target(
                node,
                importlib_aliases=importlib_aliases,
                direct_dynamic_names=direct_dynamic_names,
            )
            if dynamic_import:
                imports.add(dynamic_import.split(".", 1)[0])
    return imports


def _constant_dynamic_import_target(
    call: ast.Call,
    *,
    importlib_aliases: set[str],
    direct_dynamic_names: set[str],
) -> str | None:
    if (
        _dynamic_import_call_name(
            call,
            importlib_aliases=importlib_aliases,
            direct_dynamic_names=direct_dynamic_names,
        )
        is None
    ):
        return None
    if not call.args:
        return None
    target = call.args[0]
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value
    return None


def _nonconstant_dynamic_import_errors(
    mod: ast.Module,
    *,
    generated_module: str,
    importlib_aliases: set[str],
    direct_dynamic_names: set[str],
) -> list[str]:
    errors: list[str] = []
    for node in ast.walk(mod):
        if not isinstance(node, ast.Call):
            continue
        call_name = _dynamic_import_call_name(
            node,
            importlib_aliases=importlib_aliases,
            direct_dynamic_names=direct_dynamic_names,
        )
        if call_name is None:
            continue
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            continue
        lineno = getattr(node, "lineno", None)
        loc = f" on line {lineno}" if lineno is not None else ""
        errors.append(
            f"Generated module {generated_module!r} uses non-constant dynamic import via "
            f"{call_name}{loc}. Non-constant dynamic imports are not allowed in generated "
            "code because their provenance cannot be checked."
        )
    return errors


def _dynamic_import_call_name(
    call: ast.Call,
    *,
    importlib_aliases: set[str],
    direct_dynamic_names: set[str],
) -> str | None:
    func = call.func
    if isinstance(func, ast.Name) and func.id in direct_dynamic_names:
        return func.id
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        if func.value.id in importlib_aliases and func.attr in {"import_module", "__import__"}:
            return f"importlib.{func.attr}"
    return None


def _first_party_top_levels(
    *,
    project_dir: Path,
    source_roots: Sequence[Path],
    configured: Iterable[str],
) -> set[str]:
    roots = [project_dir, *source_roots]
    first_party = {name.split(".", 1)[0] for name in configured if name.strip()}
    for root in roots:
        first_party.update(_local_top_levels(root))
    return first_party


def _local_top_levels(root: Path) -> set[str]:
    try:
        root = root.resolve()
    except Exception:
        return set()
    if not root.is_dir():
        return set()

    out: set[str] = set()
    try:
        children = list(root.iterdir())
    except OSError:
        return out
    for child in children:
        name = child.name
        if name in _SKIP_LOCAL_DIRS or name.startswith("."):
            continue
        if child.is_file() and child.suffix == ".py" and child.stem.isidentifier():
            out.add(child.stem)
            continue
        if child.is_dir() and name.isidentifier():
            if (child / "__init__.py").is_file() or any(child.glob("*.py")):
                out.add(name)
    return out


def _find_pyproject(start: Path) -> Path | None:
    try:
        cur = start.resolve()
    except Exception:
        cur = start
    if cur.is_file():
        cur = cur.parent
    while True:
        candidate = cur / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if cur.parent == cur:
            return None
        cur = cur.parent


def _declared_project_dependencies(pyproject_path: Path | None) -> frozenset[str]:
    if pyproject_path is None:
        return frozenset()
    try:
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    except Exception:
        return frozenset()
    project = data.get("project")
    if not isinstance(project, dict):
        return frozenset()
    raw_deps = project.get("dependencies")
    if not isinstance(raw_deps, list):
        return frozenset()

    deps: set[str] = set()
    for dep in raw_deps:
        if not isinstance(dep, str):
            continue
        name = _dependency_distribution_name(dep)
        if name:
            deps.add(pep503_normalize(name))
    return frozenset(deps)


def _dependency_distribution_name(requirement: str) -> str:
    text = requirement.strip()
    if not text:
        return ""
    for marker in (";", "[", "<", ">", "=", "!", "~", "@", " "):
        if marker in text:
            text = text.split(marker, 1)[0]
    return text.strip()


def _import_resolves_to_declared_dependency(
    top_level: str,
    *,
    declared_dists: frozenset[str],
) -> bool:
    if not declared_dists:
        return False
    if pep503_normalize(top_level) in declared_dists:
        return True
    try:
        packages_to_dists = metadata.packages_distributions()
    except Exception:
        packages_to_dists = {}
    for dist in packages_to_dists.get(top_level, []):
        if pep503_normalize(dist) in declared_dists:
            return True
    return False


def validate_test_generated_source(
    source: str,
    expected_names: list[str],
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
    target_modules_by_name: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    errors, mod = _base_validation(source, expected_names)
    if mod is None:
        return errors

    errors.extend(
        _validate_test_contract_only(
            mod,
            spec_module=spec_module,
            generated_module=generated_module,
            public_api_only_by_name=public_api_only_by_name,
            target_modules_by_name=target_modules_by_name or {},
        )
    )
    return errors


def validate_test_contract_only(
    source: str,
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
    target_modules_by_name: dict[str, tuple[str, ...]] | None = None,
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    return _validate_test_contract_only(
        mod,
        spec_module=spec_module,
        generated_module=generated_module,
        public_api_only_by_name=public_api_only_by_name,
        target_modules_by_name=target_modules_by_name or {},
    )


def _validate_test_contract_only(
    mod: ast.Module,
    *,
    spec_module: str,
    generated_module: str,
    public_api_only_by_name: dict[str, bool],
    target_modules_by_name: dict[str, tuple[str, ...]],
) -> list[str]:
    errors: list[str] = []
    module_imported_modules, module_imported_symbols = _collect_import_aliases(mod)
    defs_by_name = {
        node.name: node
        for node in mod.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for test_name, public_api_only in sorted(public_api_only_by_name.items()):
        if not public_api_only:
            continue
        node = defs_by_name.get(test_name)
        if node is None:
            continue
        errors.extend(
            _validate_public_api_only_test(
                node,
                spec_module=spec_module,
                generated_module=generated_module,
                target_modules=target_modules_by_name.get(test_name, ()),
                imported_modules=module_imported_modules,
                imported_symbols=module_imported_symbols,
            )
        )
    return errors


def _base_validation(source: str, expected_names: list[str]) -> tuple[list[str], ast.Module | None]:
    if expected_names is None:
        expected_names = []

    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)], None

    errors: list[str] = []
    defined = _defined_top_level_names(mod)
    for name in expected_names:
        if name not in defined:
            errors.append(f"Missing top-level definition: {name}")

    return errors, mod


def _defined_top_level_names(mod: ast.Module) -> set[str]:
    defined: set[str] = set()
    for node in mod.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defined.add(node.name)
            continue

        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    defined.add(tgt.id)
            continue

        if isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                defined.add(node.target.id)
            continue
    return defined


def _validate_public_api_only_test(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    *,
    spec_module: str,
    generated_module: str,
    target_modules: tuple[str, ...],
    imported_modules: dict[str, str],
    imported_symbols: dict[str, str],
) -> list[str]:
    errors: list[str] = []
    forbidden_dunders = {"__globals__", "__dict__", "__code__", "__closure__", "__wrapped__"}
    forbidden_modules = {
        spec_module,
        generated_module,
    }
    imported_modules = dict(imported_modules)
    imported_symbols = dict(imported_symbols)

    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            module = child.module or ""
            if any(module == mod or module.startswith(mod + ".") for mod in forbidden_modules):
                errors.append(
                    f"{node.name}: public_api_only tests must not import from {module!r}."
                )
            for alias in child.names:
                imported_symbols[alias.asname or alias.name] = module
                if alias.name.startswith("_"):
                    errors.append(
                        f"{node.name}: public_api_only tests must not import underscore-prefixed "
                        f"symbol {alias.name!r}."
                    )

        elif isinstance(child, ast.Import):
            for alias in child.names:
                mod_name = alias.name
                imported_modules[alias.asname or mod_name.split(".")[0]] = mod_name
                if any(
                    mod_name == mod or mod_name.startswith(mod + ".") for mod in forbidden_modules
                ):
                    errors.append(
                        f"{node.name}: public_api_only tests must not import {mod_name!r}."
                    )

    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            if child.attr in forbidden_dunders:
                errors.append(
                    f"{node.name}: public_api_only tests must not inspect {child.attr!r}."
                )
            if child.attr.startswith("_") and not child.attr.startswith("__"):
                errors.append(
                    f"{node.name}: public_api_only tests must not access underscore-prefixed "
                    f"attribute {child.attr!r}."
                )

        elif isinstance(child, ast.Call):
            forbidden_target = _monkeypatched_target_module(
                child,
                imported_modules=imported_modules,
                imported_symbols=imported_symbols,
                target_modules=target_modules,
            )
            if forbidden_target:
                errors.append(
                    f"{node.name}: public_api_only tests must not monkeypatch target-module "
                    f"attribute(s) on {forbidden_target!r}."
                )
        elif isinstance(child, ast.Constant) and isinstance(child.value, str):
            if "\x1b[" in child.value or "\\x1b[" in child.value or "\\x1b\\[" in child.value:
                errors.append(
                    f"{node.name}: public_api_only tests must not assert exact ANSI/control-"
                    "sequence patterns."
                )

    return errors


def _collect_import_aliases(node: ast.AST) -> tuple[dict[str, str], dict[str, str]]:
    imported_modules: dict[str, str] = {}
    imported_symbols: dict[str, str] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                mod_name = alias.name
                imported_modules[alias.asname or mod_name.split(".")[0]] = mod_name
        elif isinstance(child, ast.ImportFrom):
            module = child.module or ""
            for alias in child.names:
                imported_symbols[alias.asname or alias.name] = module
    return imported_modules, imported_symbols


def _monkeypatched_target_module(
    call: ast.Call,
    *,
    imported_modules: dict[str, str],
    imported_symbols: dict[str, str],
    target_modules: tuple[str, ...],
) -> str | None:
    func = call.func
    if not isinstance(func, ast.Attribute) or func.attr not in {"setattr", "delattr"}:
        return None

    target_expr = call.args[0] if call.args else None
    if target_expr is None:
        for keyword in call.keywords:
            if keyword.arg == "target":
                target_expr = keyword.value
                break
    if target_expr is None:
        return None

    if isinstance(target_expr, ast.Name):
        return _resolve_forbidden_target_module(
            target_expr.id,
            imported_modules=imported_modules,
            imported_symbols=imported_symbols,
            target_modules=target_modules,
        )

    if isinstance(target_expr, ast.Attribute) and isinstance(target_expr.value, ast.Name):
        return _resolve_forbidden_target_module(
            target_expr.value.id,
            imported_modules=imported_modules,
            imported_symbols=imported_symbols,
            target_modules=target_modules,
        )

    return None


def _resolve_forbidden_target_module(
    name: str,
    *,
    imported_modules: dict[str, str],
    imported_symbols: dict[str, str],
    target_modules: tuple[str, ...],
) -> str | None:
    candidate = imported_modules.get(name) or imported_symbols.get(name)
    if not candidate:
        return None
    if any(candidate == mod or candidate.startswith(mod + ".") for mod in target_modules):
        return candidate
    return None


def compile_check(source: str, filename: str) -> list[str]:
    """Attempt to compile source for syntax-level errors (empty list means ok)."""

    try:
        compile(source or "", filename, "exec")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]
    except Exception as e:  # pragma: no cover - rare, but return a friendly string.
        return [f"CompileError: {e!r}"]
    return []


def _find_class(mod: ast.Module, class_name: str) -> ast.ClassDef | None:
    for node in mod.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    return None


def _method_nodes(cls: ast.ClassDef) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {n.name: n for n in cls.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _class_attribute_nodes(cls: ast.ClassDef) -> dict[str, str]:
    out: dict[str, str] = {}
    for node in cls.body:
        if isinstance(node, ast.Assign):
            rendered = ast.unparse(node)
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = rendered
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            out[node.target.id] = ast.unparse(node)
    return out


def _normalized_ast_dump(src_or_node: str | ast.AST) -> str:
    node = ast.parse(src_or_node).body[0] if isinstance(src_or_node, str) else src_or_node
    # Strip decorators so @jaunt.preserve and formatting don't affect equivalence.
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        node.decorator_list = []
    return ast.dump(node, include_attributes=False)


def validate_build_class_source(
    source: str,
    *,
    class_name: str,
    stub_methods: list[str],
    preserved_segments: dict[str, str],
    declared_bases: list[str],
    class_decorators: list[str],
    required_abstractmethods: list[str],
    spec_docstring: str,
    class_attributes: dict[str, str] | None = None,
    require_public_method: bool = False,
    sealed_signatures: dict[str, str] | None = None,
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError as e:
        return [_syntax_error_to_str(e)]

    cls = _find_class(mod, class_name)
    if cls is None:
        return [f"Missing top-level class definition: {class_name}"]

    errors: list[str] = []
    methods = _method_nodes(cls)

    # Structure: stub methods must exist.
    for name in stub_methods:
        if name not in methods:
            errors.append(f"{class_name}: missing required method {name!r} from spec.")

    # Structure: declared bases preserved by name.
    actual_bases = {ast.unparse(b) for b in cls.bases}
    for base in declared_bases:
        if base not in actual_bases:
            errors.append(f"{class_name}: declared base class {base!r} was not preserved.")

    # Structure: class decorators preserved by source text.
    actual_decos = {ast.unparse(d) for d in cls.decorator_list}
    for deco in class_decorators:
        if deco not in actual_decos:
            errors.append(f"{class_name}: class decorator {deco!r} was not preserved.")

    # Abstractmethods: each required name must be defined on the generated class.
    for name in required_abstractmethods:
        if name not in methods:
            errors.append(f"{class_name}: inherited abstractmethod {name!r} is not implemented.")

    # Preserved-intact: AST-equivalence (decorators stripped).
    for name, spec_seg in preserved_segments.items():
        node = methods.get(name)
        if node is None:
            errors.append(f"{class_name}: preserved method {name!r} is missing from output.")
            continue
        if _normalized_ast_dump(node) != _normalized_ast_dump(spec_seg):
            errors.append(
                f"{class_name}: preserved method {name!r} was modified; it must be kept verbatim."
            )

    # Docstring retained (additions allowed). Compare with whitespace normalized so
    # an LLM reflowing a multi-line docstring's internal spacing is not a failure.
    if spec_docstring:
        actual_doc = ast.get_docstring(cls, clean=True) or ""
        if _normalize_whitespace(spec_docstring) not in _normalize_whitespace(actual_doc):
            errors.append(
                f"{class_name}: the spec docstring must be retained (additions are allowed)."
            )

    # Unfilled-stub detection (AST, not the sentinel comment): each declared stub
    # method must have a real body in the output.
    for name in stub_methods:
        node = methods.get(name)
        if node is not None and is_stub_body(node):
            errors.append(
                f"{class_name}: method {name!r} was left as a stub; implement it per the spec."
            )

    # Sealed methods: signature is the contract -- exact match required.
    if sealed_signatures:
        from jaunt.class_analysis import canonical_signature

        for name, expected_sig in sealed_signatures.items():
            node = methods.get(name)
            if node is None:
                continue  # the stub-existence check above already errored
            if canonical_signature(node) != expected_sig:
                errors.append(
                    f"{class_name}.{name}: sealed method signature drifted; implement "
                    f"exactly the declared signature (params, defaults, annotations, "
                    f"and return type) -- do not rename, add, or remove parameters."
                )

    # Class-attribute preservation: every spec class attribute must survive with the
    # same annotation/value (compared modulo formatting via ast.unparse round-trip).
    if class_attributes:
        actual_attrs = _class_attribute_nodes(cls)
        for attr_name, expected_src in class_attributes.items():
            actual_src = actual_attrs.get(attr_name)
            if actual_src is None:
                errors.append(
                    f"{class_name}: class attribute {attr_name!r} from the spec was not preserved."
                )
            elif actual_src != expected_src:
                errors.append(
                    f"{class_name}: class attribute {attr_name!r} was modified; "
                    "keep it exactly as declared in the spec."
                )

    # Docstring-only completeness: a docstring-only spec must yield a non-trivial class.
    if require_public_method and not any(not name.startswith("_") for name in methods):
        errors.append(f"{class_name}: docstring-only spec must define at least one public method.")

    return errors


def _normalize_whitespace(text: str) -> str:
    """Collapse all runs of whitespace to single spaces for tolerant comparison."""
    return " ".join(text.split())


def class_build_warnings(
    source: str,
    *,
    class_name: str,
    stub_signatures: dict[str, list[str]],
) -> list[str]:
    try:
        mod = ast.parse(source or "")
    except SyntaxError:
        return []
    cls = _find_class(mod, class_name)
    if cls is None:
        return []
    methods = _method_nodes(cls)
    warnings: list[str] = []
    for name, declared_params in stub_signatures.items():
        node = methods.get(name)
        if node is None:
            continue
        actual = {a.arg for a in node.args.args} | {a.arg for a in node.args.kwonlyargs}
        for param in declared_params:
            if param not in actual:
                warnings.append(
                    f"{class_name}.{name}: generated signature dropped declared parameter "
                    f"{param!r}."
                )
    return warnings
