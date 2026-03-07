"""Prowling the forests of the night -- discovery helpers.

Scan for modules and import them to populate registries. This module is
intentionally lightweight. Callers are responsible for managing `sys.path`
so that discovered modules are importable.
"""

from __future__ import annotations

import fnmatch
import importlib
import sys
from pathlib import Path
from typing import Literal

from jaunt.errors import JauntDiscoveryError


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


def evict_modules_for_import(*, module_names: list[str], roots: list[Path]) -> None:
    """Drop cached modules that would interfere with fresh project imports."""

    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved_roots.append(root.resolve())
        except Exception:
            continue

    exact = set(module_names)
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
        sys.modules.pop(name, None)

    importlib.invalidate_caches()


def discover_modules(
    *,
    roots: list[Path],
    exclude: list[str],
    generated_dir: str,
    module_prefix: str | None = None,
    target_modules: set[str] | None = None,
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

    prefix = module_prefix or None

    # Fast path: resolve target modules directly from their expected file paths.
    if target_modules is not None:
        found: set[str] = set()
        for mod in target_modules:
            # Strip prefix to get the relative module name.
            relative_mod = mod
            if prefix is not None and mod.startswith(f"{prefix}."):
                relative_mod = mod[len(prefix) + 1 :]
            elif prefix is not None and mod == prefix:
                relative_mod = ""

            for root in roots:
                if relative_mod == "":
                    candidate = root / "__init__.py"
                    if candidate.is_file():
                        found.add(mod)
                        break
                else:
                    parts = relative_mod.split(".")
                    # Could be a package (dir/__init__.py) or a module (file.py).
                    file_path = root / Path(*parts).with_suffix(".py")
                    pkg_path = root / Path(*parts) / "__init__.py"
                    if file_path.is_file() or pkg_path.is_file():
                        found.add(mod)
                        break
        return sorted(found)

    module_names: set[str] = set()

    for root in roots:
        for py_file in root.rglob("*.py"):
            if not py_file.is_file():
                continue

            rel = py_file.relative_to(root)
            if generated_dir and generated_dir in rel.parts:
                continue

            rel_posix = rel.as_posix()
            if _is_excluded(rel_posix, exclude=exclude):
                continue

            if rel.name == "__init__.py":
                base_mod = ".".join(rel.parent.parts)
            else:
                base_mod = ".".join(rel.with_suffix("").parts)

            if base_mod == "":
                # Root-level __init__.py doesn't map to a sensible module name
                # unless the caller provides a namespace prefix (ex: tests).
                if prefix is None:
                    continue
                module_names.add(prefix)
                continue

            if prefix is None:
                module_names.add(base_mod)
            else:
                module_names.add(f"{prefix}.{base_mod}")

    return sorted(module_names)


def import_and_collect(module_names: list[str], *, kind: Literal["magic", "test"]) -> None:
    """Import each module by name to trigger decorator registration side effects."""

    for name in module_names:
        try:
            importlib.import_module(name)
        except Exception as e:  # noqa: BLE001 - caller needs a single error type
            raise JauntDiscoveryError(
                f"Failed to import {kind} module '{name}': {type(e).__name__}: {e}"
            ) from e
