"""Prowling the forests of the night -- discovery helpers.

Scan for modules and import them to populate registries. This module is
intentionally lightweight. Callers are responsible for managing `sys.path`
so that discovered modules are importable.
"""

from __future__ import annotations

import ast
import fnmatch
import importlib
import importlib.metadata
import sys
import warnings
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal

from jaunt.errors import JauntDiscoveryError

_EMITTED_LAYOUT_WARNINGS: set[str] = set()
_PACKAGES_DISTRIBUTIONS: Mapping[str, list[str]] | None = None

# Top-level package name of the RUNNING framework. When jaunt self-hosts (its own
# ``src`` is a configured source_root), discovered specs are ``jaunt.*`` names and
# the running package must never be evicted/forked out from under the CLI.
_SELF_PACKAGE: str = __package__ or "jaunt"


def reset_discovery_warnings() -> None:
    """Clear process-local discovery warning deduplication state."""

    _EMITTED_LAYOUT_WARNINGS.clear()


def _warn_layout_once(message: str) -> None:
    if message in _EMITTED_LAYOUT_WARNINGS:
        return
    _EMITTED_LAYOUT_WARNINGS.add(message)
    warnings.warn(message, UserWarning, stacklevel=3)


def _packages_distributions() -> Mapping[str, list[str]]:
    global _PACKAGES_DISTRIBUTIONS
    packages = _PACKAGES_DISTRIBUTIONS
    if packages is None:
        packages = importlib.metadata.packages_distributions()
        _PACKAGES_DISTRIBUTIONS = packages
    return packages


def _warn_if_package_source_root(root: Path) -> None:
    if not (root / "__init__.py").is_file():
        return

    root_name = root.name or str(root)
    _warn_layout_once(
        "Configured source root is a package directory; discovered module names will be "
        f"bare (for example, 'timing' not '{root_name}.timing'). source_roots usually "
        "should point at the package parent."
    )


def _warn_if_top_level_shadow(module_name: str) -> None:
    if "." in module_name:
        return

    shadows_stdlib = module_name in getattr(sys, "stdlib_module_names", frozenset())
    distributions = _packages_distributions().get(module_name, [])
    if not shadows_stdlib and not distributions:
        return

    shadowed = "stdlib module" if shadows_stdlib else "installed distribution"
    if shadows_stdlib and distributions:
        shadowed = "stdlib module / installed distribution"
    _warn_layout_once(
        f"Derived top-level module name '{module_name}' may shadow a {shadowed}; "
        "point source_roots at the package parent so discovered names are qualified."
    )


def _is_excluded(rel_posix: str, *, exclude: list[str]) -> bool:
    # Patterns are matched against a posix-style relative path.
    for pat in exclude:
        if fnmatch.fnmatchcase(rel_posix, pat):
            return True

        # `fnmatch` doesn't treat a leading `**/` as "zero or more directories",
        # but the prompt's examples do. Normalize by stripping leading `**/`.
        stripped = pat
        while stripped.startswith("**/"):
            stripped = stripped[3:]
            if fnmatch.fnmatchcase(rel_posix, stripped):
                return True

    return False


def _is_under_roots(path_str: str, *, roots: list[Path]) -> bool:
    try:
        path = Path(path_str).resolve()
    except Exception:
        return False

    for root in roots:
        try:
            if path.is_relative_to(root):
                return True
        except Exception:
            continue
    return False


def is_self_module(name: str) -> bool:
    """True for the running framework's own top package and its submodules."""

    return name == _SELF_PACKAGE or name.startswith(f"{_SELF_PACKAGE}.")


def self_preserved_modules(module_names: Iterable[str]) -> frozenset[str]:
    """Discovered names owned by the running framework that are ALREADY imported.

    Scoped to discovered ∩ imported ∩ self: an adopter's discovery never yields
    ``jaunt.*`` names, so the returned set is empty for them and a subsequent
    ``clear_registries`` stays total (no self-spec leakage into adopter builds).
    """

    return frozenset(n for n in module_names if is_self_module(n) and n in sys.modules)


def evict_modules_for_import(*, module_names: list[str], roots: list[Path]) -> None:
    """Drop cached modules that would interfere with fresh project imports.

    The running framework's own package is never evicted, regardless of which
    rule (exact, prefix, or ``__file__``-under-roots) matched it. Evicting the
    live jaunt package re-executes ``jaunt/__init__.py`` and forks the registry
    the CLI already holds a reference to — the self-hosting split-brain bug.
    """

    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved_roots.append(root.resolve())
        except Exception:
            continue

    exact = set(module_names)
    for name in list(module_names):
        parent = name.rpartition(".")[0]
        while parent:
            exact.add(parent)
            parent = parent.rpartition(".")[0]
    prefixes = tuple(f"{name}." for name in exact)
    to_delete: set[str] = set()

    for name, module in list(sys.modules.items()):
        if exact and (name in exact or name.startswith(prefixes)):
            to_delete.add(name)
            continue

        if module is None:
            continue

        mod_file = getattr(module, "__file__", None)
        if isinstance(mod_file, str) and _is_under_roots(mod_file, roots=resolved_roots):
            to_delete.add(name)
            continue

        mod_path = getattr(module, "__path__", None)
        if mod_path is None:
            continue

        try:
            candidates = list(mod_path)
        except TypeError:
            candidates = []

        for candidate in candidates:
            if isinstance(candidate, str) and _is_under_roots(candidate, roots=resolved_roots):
                to_delete.add(name)
                break

    for name in to_delete:
        if is_self_module(name):
            continue
        sys.modules.pop(name, None)

    importlib.invalidate_caches()


def prepare_import_environment(*, module_names: list[str], roots: list[Path]) -> None:
    """Reset registries + sys.modules for a fresh discovery import pass.

    The one shared entry point for CLI discovery sites: clears the registries
    (preserving the running framework's own already-imported specs) and then
    evicts stale cached modules (carving out the self package). Call
    ``discover_modules(...)`` FIRST so ``module_names`` reflects the current tree,
    then hand those names here before ``import_and_collect``.
    """

    from jaunt.registry import clear_registries

    clear_registries(preserve_modules=self_preserved_modules(module_names))
    evict_modules_for_import(module_names=module_names, roots=roots)


def _module_name_for_file(
    *,
    root: Path,
    py_file: Path,
    module_prefix: str | None = None,
) -> str | None:
    rel = py_file.relative_to(root)
    if rel.name == "__init__.py":
        base_mod = ".".join(rel.parent.parts)
    else:
        base_mod = ".".join(rel.with_suffix("").parts)

    prefix = module_prefix or None
    if base_mod == "":
        if prefix is None:
            return None
        return prefix

    if prefix is None:
        return base_mod
    return f"{prefix}.{base_mod}"


_JAUNT_DECORATOR_NAMES = frozenset({"magic", "test", "contract", "preserve"})


def _has_jaunt_markers(source: str) -> bool:
    """True when the source shows evidence of jaunt specs (import or decorator).

    Cheap textual prefilter first — files without ``jaunt`` or ``magic_module``
    are never parsed. Files that fail to parse cannot define importable specs.
    """
    if "jaunt" not in source and "magic_module" not in source:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name == "jaunt" or a.name.startswith("jaunt.") for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "jaunt" or mod.startswith("jaunt."):
                return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for dec in node.decorator_list:
                target = dec.func if isinstance(dec, ast.Call) else dec
                name = (
                    target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
                )
                if name in _JAUNT_DECORATOR_NAMES:
                    return True
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            func = node.value.func
            fname = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if fname == "magic_module":
                return True
    return False


def discover_module_files(
    *,
    roots: list[Path],
    exclude: list[str],
    generated_dir: str,
    module_prefix: str | None = None,
    target_modules: set[str] | None = None,
    spec_prescreen: bool = True,
) -> list[tuple[str, Path]]:
    """Discover Python modules and their backing files under the provided roots."""

    prefix = module_prefix or None

    if target_modules is not None:
        found: dict[str, Path] = {}
        for mod in target_modules:
            relative_mod = mod
            if prefix is not None and mod.startswith(f"{prefix}."):
                relative_mod = mod[len(prefix) + 1 :]
            elif prefix is not None and mod == prefix:
                relative_mod = ""

            for root in roots:
                if relative_mod == "":
                    candidate = root / "__init__.py"
                    if candidate.is_file():
                        found[mod] = candidate
                        break
                else:
                    parts = relative_mod.split(".")
                    file_path = root / Path(*parts).with_suffix(".py")
                    pkg_path = root / Path(*parts) / "__init__.py"
                    if file_path.is_file():
                        found[mod] = file_path
                        break
                    if pkg_path.is_file():
                        found[mod] = pkg_path
                        break
        return sorted(found.items(), key=lambda item: item[0])

    discovered: dict[str, Path] = {}
    for root in roots:
        if prefix is None:
            _warn_if_package_source_root(root)

        for py_file in root.rglob("*.py"):
            if not py_file.is_file():
                continue

            rel = py_file.relative_to(root)
            if generated_dir and generated_dir in rel.parts:
                continue

            rel_posix = rel.as_posix()
            if _is_excluded(rel_posix, exclude=exclude):
                continue

            if spec_prescreen:
                try:
                    source = py_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                if not _has_jaunt_markers(source):
                    continue

            module_name = _module_name_for_file(root=root, py_file=py_file, module_prefix=prefix)
            if module_name is None:
                continue
            if prefix is None:
                _warn_if_top_level_shadow(module_name)
            discovered[module_name] = py_file

    return sorted(discovered.items(), key=lambda item: item[0])


def discover_modules(
    *,
    roots: list[Path],
    exclude: list[str],
    generated_dir: str,
    module_prefix: str | None = None,
    target_modules: set[str] | None = None,
    spec_prescreen: bool = True,
) -> list[str]:
    """Discover Python module names under the provided roots.

    - Scans for `*.py` files under each root.
    - Converts each path to a module name relative to the root.
    - Excludes any file under a directory named `generated_dir` and any path
      matching a glob in `exclude` (matched against a posix-style relative path).
    - If `module_prefix` is provided, prefixes discovered module names with it.
    - If `target_modules` is provided, fast-path: verify each target exists on
      disk and return only those instead of scanning the full tree.
    """
    return [
        module_name
        for module_name, _path in discover_module_files(
            roots=roots,
            exclude=exclude,
            generated_dir=generated_dir,
            module_prefix=module_prefix,
            target_modules=target_modules,
            spec_prescreen=spec_prescreen,
        )
    ]


def import_and_collect(
    module_names: list[str], *, kind: Literal["magic", "test", "contract"]
) -> None:
    """Import each module by name to trigger decorator registration side effects."""

    for name in module_names:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - caller needs a single error type
            raise JauntDiscoveryError(
                f"Failed to import {kind} module '{name}': {type(e).__name__}: {e}"
            ) from e

    if kind == "magic":
        from jaunt.module_magic import finalize_module_magic
        from jaunt.registry import get_module_magic_registry

        for governed_module in list(get_module_magic_registry()):
            if governed_module in sys.modules:
                try:
                    finalize_module_magic(governed_module)
                except Exception as e:  # noqa: BLE001 - caller needs a single error type
                    raise JauntDiscoveryError(
                        f"Failed to finalize magic module '{governed_module}': "
                        f"{type(e).__name__}: {e}"
                    ) from e
