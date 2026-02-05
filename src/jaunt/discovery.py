"""Discovery helpers: scan for modules and import them to populate registries.

This module is intentionally lightweight. Callers are responsible for managing
`sys.path` so that discovered modules are importable.
"""

from __future__ import annotations

import fnmatch
import importlib
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


def discover_modules(
    *,
    roots: list[Path],
    exclude: list[str],
    generated_dir: str,
    module_prefix: str | None = None,
) -> list[str]:
    """Discover Python module names under the provided roots.

    - Scans for `*.py` files under each root.
    - Converts each path to a module name relative to the root.
    - Excludes any file under a directory named `generated_dir` and any path
      matching a glob in `exclude` (matched against a posix-style relative path).
    - If `module_prefix` is provided, prefixes discovered module names with it.
    """

    module_names: set[str] = set()
    prefix = module_prefix or None

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
