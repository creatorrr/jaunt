"""Resolve one Jaunt config into concrete, per-module workspace routes."""

from __future__ import annotations

import glob
import os
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from jaunt.config import JauntConfig
from jaunt.errors import JauntConfigError


_GLOB_CHARS = frozenset("*?[")


def is_glob_pattern(value: str) -> bool:
    return any(char in value for char in _GLOB_CHARS)


def expand_roots(
    root: Path,
    patterns: Sequence[str],
    *,
    setting: str,
    require_one: bool,
    include_missing_literals: bool = False,
) -> tuple[Path, ...]:
    """Expand path entries in configuration order and matches lexically."""

    root = root.resolve()
    expanded: list[Path] = []
    seen: set[Path] = set()
    for raw in patterns:
        configured = Path(raw)
        pattern = str(configured if configured.is_absolute() else root / configured)
        if is_glob_pattern(raw):
            matches = sorted(
                (Path(item).resolve() for item in glob.glob(pattern, recursive=True)),
                key=lambda path: path.as_posix(),
            )
            matches = [path for path in matches if path.is_dir()]
            if not matches:
                raise JauntConfigError(
                    f"Invalid config: {setting} glob {raw!r} matched no directories."
                )
        else:
            literal = (
                configured.resolve() if configured.is_absolute() else (root / configured).resolve()
            )
            matches = [literal] if literal.is_dir() or include_missing_literals else []
        for match in matches:
            if match in seen:
                continue
            expanded.append(match)
            seen.add(match)
    if require_one and not expanded:
        raise JauntConfigError(
            f"Invalid config: none of {setting} exist on disk relative to the project root."
        )
    return tuple(expanded)


def nearest_pyproject(path: Path, *, config_root: Path) -> Path | None:
    """Return the nearest ancestor pyproject without escaping the workspace."""

    config_root = config_root.resolve()
    current = path.resolve()
    if current.is_file() or current.suffix:
        current = current.parent
    while True:
        candidate = current / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if current == config_root or current.parent == current:
            return None
        try:
            current.relative_to(config_root)
        except ValueError:
            return None
        current = current.parent


@dataclass(frozen=True, slots=True)
class TestRoute:
    root: Path
    owner_dir: Path
    owner_pyproject: Path | None
    module_prefix: str


@dataclass(frozen=True, slots=True)
class ModuleRoute:
    module: str
    source_file: Path
    import_root: Path
    owner_pyproject: Path | None
    owner_dir: Path
    test_roots: tuple[Path, ...]
    output_base: Path


@dataclass(frozen=True, slots=True)
class ResolvedWorkspace:
    root: Path
    source_roots: tuple[Path, ...]
    test_roots: tuple[TestRoute, ...]
    modules: tuple[ModuleRoute, ...]

    @property
    def routes(self) -> dict[str, ModuleRoute]:
        return {route.module: route for route in self.modules}

    @property
    def output_bases(self) -> dict[str, Path]:
        return {route.module: route.output_base for route in self.modules}

    @property
    def owner_dirs(self) -> tuple[Path, ...]:
        owners = {route.owner_dir for route in self.modules}
        owners.update(route.owner_dir for route in self.test_roots)
        return tuple(sorted(owners, key=lambda path: path.as_posix()))

    def route_for(self, module: str) -> ModuleRoute:
        try:
            return self.routes[module]
        except KeyError as exc:
            raise JauntConfigError(f"No workspace route for module {module!r}.") from exc

    def tests_for_owner(self, owner_dir: Path) -> tuple[TestRoute, ...]:
        owner_dir = owner_dir.resolve()
        return tuple(route for route in self.test_roots if route.owner_dir == owner_dir)

    def primary_test_root(self, owner_dir: Path) -> TestRoute:
        routes = self.tests_for_owner(owner_dir)
        if routes:
            return routes[0]
        owner_dir = owner_dir.resolve()
        return TestRoute(
            root=owner_dir / "tests",
            owner_dir=owner_dir,
            owner_pyproject=nearest_pyproject(owner_dir, config_root=self.root),
            module_prefix="tests",
        )


def _matched_import_root(path: Path, roots: Sequence[Path]) -> Path | None:
    candidates = [root for root in roots if path == root or path.is_relative_to(root)]
    return max(candidates, key=lambda item: len(item.parts)) if candidates else None


def _module_name(import_root: Path, source_file: Path) -> str | None:
    rel = source_file.relative_to(import_root)
    parts = rel.parent.parts if rel.name == "__init__.py" else rel.with_suffix("").parts
    return ".".join(parts) or None


def _owner(path: Path, *, root: Path) -> tuple[Path | None, Path]:
    pyproject = nearest_pyproject(path, config_root=root)
    return pyproject, pyproject.parent if pyproject is not None else root


def _test_prefix(test_root: Path, owner_dir: Path) -> str:
    try:
        rel = test_root.relative_to(owner_dir)
    except ValueError:
        rel = Path(test_root.name)
    return ".".join(part for part in rel.parts if part not in {"", "."}) or "tests"


def resolve_workspace(
    root: Path,
    cfg: JauntConfig,
    *,
    spec_prescreen: bool = True,
) -> ResolvedWorkspace:
    """Expand roots and derive unique per-module routes before any imports."""

    from jaunt.discovery import _has_jaunt_markers

    root = root.resolve()
    source_roots = expand_roots(
        root,
        cfg.paths.source_roots,
        setting="paths.source_roots",
        require_one=True,
    )
    concrete_tests = expand_roots(
        root,
        cfg.paths.test_roots,
        setting="paths.test_roots",
        require_one=False,
        include_missing_literals=True,
    )

    test_routes: list[TestRoute] = []
    for test_root in concrete_tests:
        owner_pyproject, owner_dir = _owner(test_root, root=root)
        test_routes.append(
            TestRoute(
                root=test_root,
                owner_dir=owner_dir,
                owner_pyproject=owner_pyproject,
                module_prefix=_test_prefix(test_root, owner_dir),
            )
        )

    files: dict[Path, bool] = {}
    for source_root in source_roots:
        for dirpath, dirnames, filenames in os.walk(source_root):
            current = Path(dirpath).resolve()
            dirnames[:] = [
                name
                for name in dirnames
                if not name.startswith(".")
                and name not in {cfg.paths.generated_dir, "__pycache__"}
                and not any((current / name).resolve() == route.root for route in test_routes)
            ]
            for filename in filenames:
                if not filename.endswith(".py"):
                    continue
                path = current / filename
                if not path.is_file():
                    continue
                resolved = path.resolve()
                if any(
                    resolved == route.root or resolved.is_relative_to(route.root)
                    for route in test_routes
                ):
                    continue
                try:
                    source = path.read_text(encoding="utf-8")
                except OSError:
                    continue
                files[resolved] = _has_jaunt_markers(source)

    by_name: dict[str, ModuleRoute] = {}
    seen_names: dict[str, Path] = {}
    collisions: dict[str, set[Path]] = defaultdict(set)
    for source_file in sorted(files, key=lambda path: path.as_posix()):
        import_root = _matched_import_root(source_file, source_roots)
        if import_root is None:
            continue
        module = _module_name(import_root, source_file)
        if module is None:
            continue
        owner_pyproject, owner_dir = _owner(source_file, root=root)
        route = ModuleRoute(
            module=module,
            source_file=source_file,
            import_root=import_root,
            owner_pyproject=owner_pyproject,
            owner_dir=owner_dir,
            test_roots=tuple(item.root for item in test_routes if item.owner_dir == owner_dir),
            output_base=import_root,
        )
        previous_file = seen_names.get(module)
        if previous_file is not None and previous_file != source_file:
            collisions[module].update({previous_file, source_file})
        seen_names.setdefault(module, source_file)
        if not spec_prescreen or files[source_file]:
            by_name[module] = route

    if collisions:
        details = "; ".join(
            f"{module}: " + ", ".join(path.as_posix() for path in sorted(paths))
            for module, paths in sorted(collisions.items())
        )
        raise JauntConfigError(f"Duplicate module names across source_roots: {details}")

    return ResolvedWorkspace(
        root=root,
        source_roots=source_roots,
        test_roots=tuple(test_routes),
        modules=tuple(by_name[name] for name in sorted(by_name)),
    )


def output_base_for(
    module: str,
    *,
    default: Path,
    module_output_bases: dict[str, Path] | None,
) -> Path:
    return default if module_output_bases is None else module_output_bases.get(module, default)


def group_modules_by_owner(
    routes: Iterable[ModuleRoute],
) -> dict[Path, tuple[ModuleRoute, ...]]:
    grouped: dict[Path, list[ModuleRoute]] = defaultdict(list)
    for route in routes:
        grouped[route.owner_dir].append(route)
    return {
        owner: tuple(sorted(items, key=lambda item: item.module))
        for owner, items in sorted(grouped.items(), key=lambda item: item[0].as_posix())
    }


def resolve_module_source(root: Path, cfg: JauntConfig, module: str) -> Path:
    """Locate one dotted module without scanning/importing the whole workspace."""

    roots = expand_roots(
        root,
        cfg.paths.source_roots,
        setting="paths.source_roots",
        require_one=True,
    )
    candidates: list[tuple[Path, Path]] = []
    parts = module.split(".")
    for import_root in roots:
        file_path = import_root / Path(*parts).with_suffix(".py")
        init_path = import_root / Path(*parts) / "__init__.py"
        for candidate in (file_path, init_path):
            if candidate.is_file():
                candidates.append((import_root, candidate.resolve()))
                break
    unique = {path for _import_root, path in candidates}
    if not unique:
        raise JauntConfigError(f"Could not locate source module {module!r} under source_roots.")
    if len(unique) > 1:
        raise JauntConfigError(
            f"Duplicate module name {module!r} across source_roots: "
            + ", ".join(path.as_posix() for path in sorted(unique))
        )
    return next(iter(unique))
