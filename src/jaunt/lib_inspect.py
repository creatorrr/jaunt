"""Library inspection: resolve PyPI/local libs and extract metadata for skill bootstrapping."""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

_README_MAX = 10_000
_API_MAX = 5_000
_TREE_MAX = 2_000
_TREE_DEPTH = 3
_TREE_ENTRIES = 50
_MAX_MODULES = 10


@dataclass(frozen=True, slots=True)
class LibRef:
    type: Literal["pypi", "path"]
    name: str
    path: str | None
    version: str | None
    import_roots: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class LibContent:
    ref: LibRef
    summary: str
    readme: str
    module_structure: str
    public_api: str
    version: str


def resolve_lib(lib_str: str) -> LibRef:
    """Resolve a library string to a LibRef.

    If lib_str is an existing directory or contains '/', treat as path.
    Otherwise try importlib.metadata — if found, PyPI.
    Raises ValueError if neither.
    """
    lib_str = lib_str.strip()
    if not lib_str:
        raise ValueError("Library name must not be empty.")

    p = Path(lib_str)
    if p.is_dir() or "/" in lib_str:
        if not p.is_dir():
            raise ValueError(f"Path does not exist or is not a directory: {lib_str}")
        resolved = p.resolve()
        roots = _local_import_roots(resolved)
        return LibRef(
            type="path",
            name=resolved.name,
            path=str(resolved),
            version=None,
            import_roots=roots,
        )

    # Try PyPI / installed package
    try:
        version = importlib.metadata.version(lib_str)
    except importlib.metadata.PackageNotFoundError:
        raise ValueError(
            f"'{lib_str}' is not an existing directory and is not an installed package."
        ) from None

    roots = _resolve_pypi_import_roots(lib_str)
    return LibRef(
        type="pypi",
        name=lib_str,
        path=None,
        version=version,
        import_roots=roots,
    )


def _resolve_pypi_import_roots(dist_name: str) -> list[str]:
    """Resolve importable top-level names for a PyPI distribution."""
    # 1. Try top_level.txt from distribution metadata files
    try:
        files = importlib.metadata.files(dist_name)
        if files:
            for f in files:
                if str(f).endswith("top_level.txt"):
                    content = f.read_text("utf-8")
                    roots = [line.strip() for line in content.splitlines() if line.strip()]
                    if roots:
                        return roots
    except Exception:  # noqa: BLE001
        pass

    # 2. Reverse lookup via packages_distributions()
    try:
        pkg_to_dists = importlib.metadata.packages_distributions()
        found = []
        for pkg, dists in pkg_to_dists.items():
            if dist_name in dists:
                found.append(pkg)
        if found:
            return sorted(found)
    except Exception:  # noqa: BLE001
        pass

    # 3. PEP 503 normalized dist name as fallback
    import re

    fallback = re.sub(r"[-_.]+", "_", dist_name).lower()
    return [fallback]


def _local_import_roots(directory: Path) -> list[str]:
    """Derive import roots from a local directory (supports src/ layout)."""
    roots: list[str] = []
    for search_dir in _local_source_dirs(directory):
        try:
            for child in sorted(search_dir.iterdir()):
                if child.is_dir() and (child / "__init__.py").is_file():
                    roots.append(child.name)
                elif child.is_file() and child.suffix == ".py" and child.stem != "__init__":
                    roots.append(child.stem)
        except OSError:
            pass
    return roots


def _local_source_dirs(directory: Path) -> list[Path]:
    """Return source directories to scan: [directory] or [directory/src] if src-layout."""
    src_dir = directory / "src"
    if src_dir.is_dir():
        # Check if src/ contains Python packages (src-layout)
        has_py = False
        try:
            for child in src_dir.iterdir():
                if child.is_dir() and (child / "__init__.py").is_file():
                    has_py = True
                    break
                if child.is_file() and child.suffix == ".py":
                    has_py = True
                    break
        except OSError:
            pass
        if has_py:
            return [src_dir]
    return [directory]


def inspect_lib(ref: LibRef) -> LibContent:
    """Inspect a library and gather metadata for skill bootstrapping."""
    if ref.type == "pypi":
        return _inspect_pypi(ref)
    return _inspect_local(ref)


def _resolve_import_root(root_name: str) -> tuple[Path, bool] | None:
    """Resolve an import root to (path, is_package).

    Returns None if the root cannot be located.
    For packages: returns (package_dir, True).
    For single-file modules (e.g. six.py): returns (file_path, False).
    Namespace packages (origin is None) are handled via submodule_search_locations.
    """
    spec = importlib.util.find_spec(root_name)
    if spec is None:
        return None

    # Namespace package: origin is None but submodule_search_locations is set
    if spec.origin is None:
        locs = spec.submodule_search_locations
        if locs:
            for loc in locs:
                p = Path(loc)
                if p.is_dir():
                    return p, True
        return None

    origin = Path(spec.origin)

    # Single-file module: origin points to e.g. site-packages/six.py
    # Its parent is site-packages — NOT the package directory.
    if origin.name != "__init__.py":
        return origin, False

    # Regular package: origin is __init__.py inside the package dir
    pkg_dir = origin.parent
    if pkg_dir.is_dir():
        return pkg_dir, True

    return None


def _inspect_pypi(ref: LibRef) -> LibContent:
    """Inspect an installed PyPI package."""
    # Metadata
    try:
        meta = importlib.metadata.metadata(ref.name)
        summary = meta.get("Summary", "") or ""
        version = meta.get("Version", ref.version or "") or ""
    except importlib.metadata.PackageNotFoundError:
        summary = ""
        version = ref.version or ""

    # README (best-effort via PyPI API)
    readme = ""
    try:
        from jaunt.pypi import fetch_readme

        readme_text, _ = fetch_readme(ref.name, version)
        readme = readme_text[:_README_MAX]
    except Exception:  # noqa: BLE001
        pass

    # Locate source files via import roots
    source_files: list[tuple[str, str]] = []  # (rel_path, content)
    for root_name in ref.import_roots:
        resolved = _resolve_import_root(root_name)
        if resolved is None:
            continue
        root_path, is_package = resolved
        if is_package:
            _collect_py_files(root_path, root_path, source_files, max_files=_MAX_MODULES)
        else:
            # Single-file module — read the file directly
            try:
                content = root_path.read_text(encoding="utf-8")
                source_files.append((root_path.name, content))
            except Exception:  # noqa: BLE001
                pass

    # Module tree
    tree_parts: list[str] = []
    for root_name in ref.import_roots:
        resolved = _resolve_import_root(root_name)
        if resolved is None:
            continue
        root_path, is_package = resolved
        if is_package:
            tree_parts.append(f"{root_name}/")
            tree_parts.append(_build_module_tree(root_path))
        else:
            tree_parts.append(root_path.name)
    module_structure = "\n".join(tree_parts)[:_TREE_MAX]

    # Public API
    api_lines: list[str] = []
    for rel, content in source_files:
        sigs = _extract_public_api(content, rel)
        if sigs:
            api_lines.append(f"# {rel}")
            api_lines.extend(sigs)
    public_api = "\n".join(api_lines)[:_API_MAX]

    return LibContent(
        ref=ref,
        summary=summary,
        readme=readme,
        module_structure=module_structure,
        public_api=public_api,
        version=version,
    )


def _inspect_local(ref: LibRef) -> LibContent:
    """Inspect a local directory as a library (supports src/ layout)."""
    assert ref.path is not None
    directory = Path(ref.path)

    # README — always at project root
    readme = ""
    for name in ("README.md", "README.rst", "README"):
        readme_path = directory / name
        if readme_path.is_file():
            try:
                readme = readme_path.read_text(encoding="utf-8")[:_README_MAX]
            except Exception:  # noqa: BLE001
                pass
            break

    # Version from pyproject.toml (at root) or __version__
    version = _extract_local_version(directory)

    # Determine source directories (handles src/ layout)
    source_dirs = _local_source_dirs(directory)

    # Source files — scan all source dirs
    source_files: list[tuple[str, str]] = []
    tree_parts: list[str] = []
    api_lines: list[str] = []

    for src_dir in source_dirs:
        _collect_py_files(src_dir, src_dir, source_files, max_files=_MAX_MODULES)
        tree_parts.append(_build_module_tree(src_dir))

    module_structure = "\n".join(t for t in tree_parts if t)[:_TREE_MAX]

    # Public API
    for rel, content in source_files:
        sigs = _extract_public_api(content, rel)
        if sigs:
            api_lines.append(f"# {rel}")
            api_lines.extend(sigs)
    public_api = "\n".join(api_lines)[:_API_MAX]

    # Summary from README first line or empty
    summary = ""
    if readme:
        for line in readme.splitlines():
            stripped = line.strip().lstrip("#").strip()
            if stripped:
                summary = stripped[:200]
                break

    return LibContent(
        ref=ref,
        summary=summary,
        readme=readme,
        module_structure=module_structure,
        public_api=public_api,
        version=version,
    )


def _collect_py_files(
    root: Path, base: Path, out: list[tuple[str, str]], *, max_files: int
) -> None:
    """Collect .py files from root, recursing into child packages."""
    if len(out) >= max_files:
        return

    # Prioritize __init__.py
    init = root / "__init__.py"
    if init.is_file():
        try:
            content = init.read_text(encoding="utf-8")
            rel = str(init.relative_to(base))
            out.append((rel, content))
        except Exception:  # noqa: BLE001
            pass

    # Collect .py files and child packages at this level
    py_files: list[tuple[int, Path]] = []
    child_packages: list[Path] = []
    try:
        for f in sorted(root.iterdir()):
            if f.is_file() and f.suffix == ".py" and f.name != "__init__.py":
                try:
                    py_files.append((f.stat().st_size, f))
                except OSError:
                    pass
            elif f.is_dir() and (f / "__init__.py").is_file():
                child_packages.append(f)
    except OSError:
        pass

    # Add .py files sorted by size (prefer smaller)
    py_files.sort(key=lambda t: t[0])
    for _, f in py_files:
        if len(out) >= max_files:
            break
        try:
            content = f.read_text(encoding="utf-8")
            rel = str(f.relative_to(base))
            out.append((rel, content))
        except Exception:  # noqa: BLE001
            pass

    # Recurse into child packages
    for child in child_packages:
        if len(out) >= max_files:
            break
        _collect_py_files(child, base, out, max_files=max_files)


def _extract_public_api(py_source: str, filename: str) -> list[str]:
    """AST-parse source and extract public function/class signatures."""
    try:
        tree = ast.parse(py_source, filename=filename)
    except SyntaxError:
        return []

    sigs: list[str] = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if node.name.startswith("_"):
                continue
            sig = _format_func_sig(node)
            sigs.append(sig)
        elif isinstance(node, ast.ClassDef):
            if node.name.startswith("_"):
                continue
            doc = _first_line_docstring(node)
            sig = f"class {node.name}"
            if doc:
                sig += f"  # {doc}"
            sigs.append(sig)
    return sigs


def _format_func_sig(node: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    """Format a function signature from AST node."""
    prefix = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
    args_parts: list[str] = []
    args = node.args

    for arg in args.args:
        name = arg.arg
        if arg.annotation:
            name += f": {ast.unparse(arg.annotation)}"
        args_parts.append(name)

    returns = ""
    if node.returns:
        returns = f" -> {ast.unparse(node.returns)}"

    sig = f"{prefix} {node.name}({', '.join(args_parts)}){returns}"
    doc = _first_line_docstring(node)
    if doc:
        sig += f"  # {doc}"
    return sig


def _first_line_docstring(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> str:
    """Extract first line of docstring from an AST node."""
    if not node.body:
        return ""
    first = node.body[0]
    if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
        val = first.value.value
        if isinstance(val, str):
            line = val.strip().split("\n")[0].strip()
            return line[:100]
    return ""


def _build_module_tree(root: Path, *, _depth: int = 0, _count: list[int] | None = None) -> str:
    """Build a tree listing of .py modules, max depth and entries."""
    if _count is None:
        _count = [0]
    if _depth >= _TREE_DEPTH or _count[0] >= _TREE_ENTRIES:
        return ""

    lines: list[str] = []
    indent = "  " * _depth

    try:
        entries = sorted(root.iterdir())
    except OSError:
        return ""

    for entry in entries:
        if _count[0] >= _TREE_ENTRIES:
            break
        if entry.name.startswith(".") or entry.name == "__pycache__":
            continue
        if entry.is_dir():
            if (entry / "__init__.py").is_file():
                lines.append(f"{indent}{entry.name}/")
                _count[0] += 1
                sub = _build_module_tree(entry, _depth=_depth + 1, _count=_count)
                if sub:
                    lines.append(sub)
        elif entry.is_file() and entry.suffix == ".py":
            lines.append(f"{indent}{entry.name}")
            _count[0] += 1

    return "\n".join(lines)


def _extract_local_version(directory: Path) -> str:
    """Try to extract version from pyproject.toml or __version__."""
    # pyproject.toml
    pyproject = directory / "pyproject.toml"
    if pyproject.is_file():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            v = data.get("project", {}).get("version")
            if isinstance(v, str) and v:
                return v
        except Exception:  # noqa: BLE001
            pass

    # __version__ in __init__.py
    init = directory / "__init__.py"
    if init.is_file():
        try:
            src = init.read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and target.id == "__version__":
                            if isinstance(node.value, ast.Constant) and isinstance(
                                node.value.value, str
                            ):
                                return node.value.value
        except Exception:  # noqa: BLE001
            pass

    return "unknown"
