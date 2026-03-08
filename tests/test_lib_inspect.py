from __future__ import annotations

from pathlib import Path

import pytest

from jaunt.lib_inspect import (
    LibRef,
    _build_module_tree,
    _extract_public_api,
    _resolve_import_root,
    inspect_lib,
    resolve_lib,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_resolve_lib_local_path(tmp_path: Path) -> None:
    (tmp_path / "mypkg" / "__init__.py").parent.mkdir(parents=True)
    (tmp_path / "mypkg" / "__init__.py").write_text("")
    ref = resolve_lib(str(tmp_path))
    assert ref.type == "path"
    assert ref.name == tmp_path.name
    assert ref.path == str(tmp_path)


def test_resolve_lib_invalid() -> None:
    with pytest.raises(ValueError, match="not an existing directory"):
        resolve_lib("definitely-not-a-real-package-xyz-9999")


def test_resolve_lib_empty() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        resolve_lib("")


def test_resolve_lib_pypi(monkeypatch) -> None:
    monkeypatch.setattr("jaunt.lib_inspect.importlib.metadata.version", lambda name: "1.2.3")
    monkeypatch.setattr("jaunt.lib_inspect._resolve_pypi_import_roots", lambda name: ["mypkg"])
    ref = resolve_lib("mypkg")
    assert ref.type == "pypi"
    assert ref.name == "mypkg"
    assert ref.version == "1.2.3"
    assert ref.import_roots == ["mypkg"]


def test_resolve_lib_pypi_dist_vs_import_mismatch(monkeypatch) -> None:
    """e.g. Pillow dist -> PIL import root."""
    monkeypatch.setattr("jaunt.lib_inspect.importlib.metadata.version", lambda name: "10.0.0")
    monkeypatch.setattr("jaunt.lib_inspect._resolve_pypi_import_roots", lambda name: ["PIL"])
    ref = resolve_lib("Pillow")
    assert ref.type == "pypi"
    assert ref.name == "Pillow"
    assert ref.import_roots == ["PIL"]


def test_inspect_local_lib(tmp_path: Path) -> None:
    _write(tmp_path / "README.md", "# My Lib\nA test library.\n")
    _write(tmp_path / "__init__.py", '__version__ = "0.1.0"\n')
    _write(
        tmp_path / "core.py",
        'def hello(name: str) -> str:\n    """Say hello."""\n    return f"Hello {name}"\n',
    )

    ref = LibRef(
        type="path",
        name=tmp_path.name,
        path=str(tmp_path),
        version=None,
        import_roots=[],
    )
    content = inspect_lib(ref)
    assert content.version == "0.1.0"
    assert "My Lib" in content.summary or "test library" in content.summary
    assert "My Lib" in content.readme
    assert "hello" in content.public_api


def test_extract_public_api() -> None:
    source = '''\
def public_func(x: int, y: str) -> bool:
    """Check something."""
    return True

def _private():
    pass

class MyClass:
    """A class."""
    pass

class _Internal:
    pass

async def async_op(data: list) -> None:
    """Process data."""
    pass
'''
    sigs = _extract_public_api(source, "test.py")
    assert len(sigs) == 3
    assert any("public_func" in s for s in sigs)
    assert any("MyClass" in s for s in sigs)
    assert any("async_op" in s for s in sigs)
    # Private should be excluded
    assert not any("_private" in s for s in sigs)
    assert not any("_Internal" in s for s in sigs)


def test_extract_public_api_syntax_error() -> None:
    sigs = _extract_public_api("def broken(", "bad.py")
    assert sigs == []


def test_build_module_tree(tmp_path: Path) -> None:
    _write(tmp_path / "pkg" / "__init__.py", "")
    _write(tmp_path / "pkg" / "core.py", "")
    _write(tmp_path / "pkg" / "sub" / "__init__.py", "")
    _write(tmp_path / "utils.py", "")

    tree = _build_module_tree(tmp_path)
    assert "pkg/" in tree
    assert "core.py" in tree
    assert "utils.py" in tree


def test_inspect_local_src_layout(tmp_path: Path) -> None:
    """src/<pkg> layout should be detected and scanned."""
    _write(tmp_path / "README.md", "# SrcLib\nA src-layout library.\n")
    _write(tmp_path / "src" / "mypkg" / "__init__.py", '__version__ = "2.0.0"\n')
    _write(
        tmp_path / "src" / "mypkg" / "api.py",
        'def do_stuff(x: int) -> str:\n    """Do stuff."""\n    return str(x)\n',
    )

    ref = LibRef(type="path", name=tmp_path.name, path=str(tmp_path), version=None, import_roots=[])
    content = inspect_lib(ref)
    # Should find the package under src/
    assert "do_stuff" in content.public_api
    assert "mypkg/" in content.module_structure


def test_resolve_import_root_single_file_module(tmp_path: Path, monkeypatch) -> None:
    """Single-file modules (e.g. six.py) should return the file, not its parent."""
    import importlib.util
    from unittest.mock import MagicMock

    single_file = tmp_path / "six.py"
    single_file.write_text("def add(a, b): return a + b\n")

    mock_spec = MagicMock()
    mock_spec.origin = str(single_file)
    mock_spec.submodule_search_locations = None

    def _find(name):
        return mock_spec if name == "six" else None

    monkeypatch.setattr(importlib.util, "find_spec", _find)

    result = _resolve_import_root("six")
    assert result is not None
    path, is_package = result
    assert not is_package
    assert path == single_file


def test_resolve_import_root_namespace_package(tmp_path: Path, monkeypatch) -> None:
    """Namespace packages (origin=None, search_locations set) should be handled."""
    import importlib.util
    from unittest.mock import MagicMock

    ns_dir = tmp_path / "google" / "cloud"
    ns_dir.mkdir(parents=True)
    (ns_dir / "__init__.py").write_text("")

    mock_spec = MagicMock()
    mock_spec.origin = None
    mock_spec.submodule_search_locations = [str(tmp_path / "google")]

    monkeypatch.setattr(
        importlib.util, "find_spec", lambda name: mock_spec if name == "google" else None
    )

    result = _resolve_import_root("google")
    assert result is not None
    path, is_package = result
    assert is_package
    assert path == tmp_path / "google"
