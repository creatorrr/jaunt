from __future__ import annotations

import ast
import functools
import os
import re
import sys
from collections.abc import Iterable, Sequence
from importlib import metadata
from pathlib import Path

_PEP503_RE = re.compile(r"[-_.]+")


def pep503_normalize(name: str) -> str:
    """Normalize a name per PEP 503 (used for dist names and comparisons)."""

    return _PEP503_RE.sub("-", (name or "").strip()).lower()


def _iter_python_files(*, roots: Sequence[Path], generated_dir: str) -> Iterable[Path]:
    skip_dirs = {
        ".git",
        ".venv",
        "venv",
        ".tox",
        "node_modules",
        "__pycache__",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".cache",
        "build",
        "dist",
        "site-packages",
    }

    for root in roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if not root.exists():
            continue

        for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
            # Prune aggressively for safety + speed.
            kept: list[str] = []
            for d in dirnames:
                if d == generated_dir:
                    continue
                if d in skip_dirs:
                    continue
                if d.endswith(".egg-info"):
                    continue
                kept.append(d)
            dirnames[:] = kept

            for fn in filenames:
                if not fn.endswith(".py"):
                    continue
                yield Path(dirpath) / fn


def _imports_from_source(source: str, *, filename: str) -> set[str]:
    try:
        tree = ast.parse(source, filename=filename)
    except Exception:
        return set()

    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name:
                    found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if int(getattr(node, "level", 0) or 0) > 0:
                # Ignore relative imports.
                continue
            mod = getattr(node, "module", None)
            if isinstance(mod, str) and mod:
                found.add(mod)

    return found


def _discover_internal_top_levels(*, source_roots: Sequence[Path]) -> set[str]:
    internal: set[str] = set()
    for root in source_roots:
        try:
            root = root.resolve()
        except Exception:
            continue
        if not root.is_dir():
            continue

        try:
            for child in root.iterdir():
                if child.is_dir():
                    if (child / "__init__.py").is_file():
                        internal.add(child.name)
                elif child.is_file() and child.suffix == ".py":
                    internal.add(child.stem)
        except OSError:
            continue

    return internal


@functools.lru_cache(maxsize=256)
def _resolve_dist_by_name_heuristic(import_mod: str) -> tuple[str, str] | None:
    """Best-effort: try dists derived from dotted module path.

    For import "a.b.c", try: "a-b-c", "a-b", "a".
    """

    parts = [p for p in (import_mod or "").split(".") if p]
    if not parts:
        return None

    for k in range(len(parts), 0, -1):
        candidate = "-".join(parts[:k])
        try:
            v = metadata.version(candidate)
        except metadata.PackageNotFoundError:
            continue
        except Exception:
            continue
        return candidate, str(v)

    return None


def _choose_dist_for_top_level(top_level: str, *, candidates: Sequence[str]) -> str | None:
    if not candidates:
        return None
    if len(candidates) == 1:
        return str(candidates[0])

    norm_top = pep503_normalize(top_level)
    exact = [c for c in candidates if pep503_normalize(c) == norm_top]
    if exact:
        return sorted(exact, key=lambda s: pep503_normalize(s))[0]

    return sorted(candidates, key=lambda s: pep503_normalize(s))[0]


def discover_external_distributions_with_warnings(
    source_roots: Sequence[Path],
    *,
    generated_dir: str,
) -> tuple[dict[str, str], list[str]]:
    """Return (dist->version, warnings) for imported external libraries.

    "External" means:
    - not stdlib (by top-level name)
    - not a project-internal top-level module under source_roots
    - resolvable to an installed distribution
    """

    warnings: list[str] = []
    internal = _discover_internal_top_levels(source_roots=source_roots)
    stdlib = getattr(sys, "stdlib_module_names", set())

    imports: set[str] = set()
    for py_file in _iter_python_files(roots=source_roots, generated_dir=generated_dir):
        try:
            src = py_file.read_text(encoding="utf-8")
        except Exception:
            continue
        imports.update(_imports_from_source(src, filename=str(py_file)))

    # Precompute fallback mapping once.
    try:
        pkg_to_dists = metadata.packages_distributions()
    except Exception:
        pkg_to_dists = {}

    out: dict[str, str] = {}
    for mod in sorted(imports):
        top = mod.split(".", 1)[0]
        if not top:
            continue
        # This repo's `jaunt` may be installed from a local checkout and not
        # published on PyPI, and `jaunt` imports appear in every spec file.
        # Ignore it so auto-skill generation doesn't warn noisily.
        if top == "jaunt":
            continue
        if top in stdlib:
            continue
        if top in internal:
            continue

        resolved = _resolve_dist_by_name_heuristic(mod)
        if resolved is not None:
            dist, ver = resolved
            out.setdefault(dist, ver)
            continue

        candidates = pkg_to_dists.get(top, []) if isinstance(pkg_to_dists, dict) else []
        dist = _choose_dist_for_top_level(top, candidates=candidates)
        if not dist:
            warnings.append(f"could not resolve installed distribution for import '{mod}'")
            continue

        try:
            ver = str(metadata.version(dist))
        except metadata.PackageNotFoundError:
            warnings.append(
                f"could not resolve installed version for distribution '{dist}' (import '{mod}')"
            )
            continue
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            warnings.append(f"could not resolve installed version for distribution '{dist}': {err}")
            continue

        out.setdefault(dist, ver)

    return out, warnings


def discover_external_distributions(
    source_roots: Sequence[Path],
    *,
    generated_dir: str,
) -> dict[str, str]:
    """Return dist->version for imported external libraries (see `*_with_warnings`)."""

    dists, _warnings = discover_external_distributions_with_warnings(
        source_roots, generated_dir=generated_dir
    )
    return dists
