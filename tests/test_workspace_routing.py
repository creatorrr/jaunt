from __future__ import annotations

import sys
from pathlib import Path

import pytest

from jaunt.config import load_config
from jaunt.errors import JauntConfigError
from jaunt.workspace import resolve_workspace


def _write(path: Path, text: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _config(root: Path, *, sources: str, tests: str = "[]"):
    _write(
        root / "jaunt.toml",
        f"version = 1\n[paths]\nsource_roots = {sources}\ntest_roots = {tests}\n",
    )
    return load_config(root=root)


def test_mixed_src_and_flat_packages_route_to_own_import_roots(tmp_path: Path) -> None:
    _write(tmp_path / "packages/a/pyproject.toml", "[project]\nname='a'\nversion='1'\n")
    _write(tmp_path / "packages/b/pyproject.toml", "[project]\nname='b'\nversion='1'\n")
    _write(tmp_path / "packages/a/src/a/spec.py", "import jaunt\n@jaunt.magic()\ndef f(): ...\n")
    _write(tmp_path / "packages/b/b/spec.py", "import jaunt\n@jaunt.magic()\ndef g(): ...\n")
    _write(tmp_path / "packages/a/tests/test_a.py")
    _write(tmp_path / "packages/b/tests/unit/test_b.py")
    cfg = _config(
        tmp_path,
        sources='["packages/*/src", "packages/b"]',
        tests='["packages/*/tests"]',
    )

    workspace = resolve_workspace(tmp_path, cfg)

    assert workspace.route_for("a.spec").output_base == (tmp_path / "packages/a/src").resolve()
    assert workspace.route_for("b.spec").output_base == (tmp_path / "packages/b").resolve()
    assert workspace.route_for("a.spec").owner_dir == (tmp_path / "packages/a").resolve()
    assert [route.module_prefix for route in workspace.test_roots] == ["tests", "tests"]


def test_overlapping_roots_use_longest_containment(tmp_path: Path) -> None:
    _write(tmp_path / "src/pkg/spec.py", "import jaunt\n@jaunt.magic()\ndef f(): ...\n")
    cfg = _config(tmp_path, sources='[".", "src"]')

    route = resolve_workspace(tmp_path, cfg).route_for("pkg.spec")

    assert route.import_root == (tmp_path / "src").resolve()


def test_duplicate_module_names_are_rejected_before_import(tmp_path: Path) -> None:
    body = "import jaunt\nraise RuntimeError('must not import')\n@jaunt.magic()\ndef f(): ...\n"
    _write(tmp_path / "one/pkg/spec.py", body)
    _write(tmp_path / "two/pkg/spec.py", body)
    cfg = _config(tmp_path, sources='["one", "two"]')

    with pytest.raises(JauntConfigError, match="Duplicate module names"):
        resolve_workspace(tmp_path, cfg)


def test_unmatched_glob_is_a_config_error(tmp_path: Path) -> None:
    _write(tmp_path / "src/keep.py")
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n[paths]\nsource_roots = ["src", "packages/*/src"]\n',
    )

    with pytest.raises(JauntConfigError, match="matched no directories"):
        load_config(root=tmp_path)


def test_cli_build_writes_each_module_under_its_import_root(tmp_path: Path, monkeypatch) -> None:
    import jaunt.cli
    from test_regressions_review_fixes import GoodBackend

    spec = (
        "import jaunt\n"
        "@jaunt.magic()\n"
        "def generated_smoke() -> None:\n"
        '    """Return without side effects."""\n'
        "    ...\n"
    )
    _write(tmp_path / "one/route_a/__init__.py")
    _write(tmp_path / "one/route_a/spec.py", spec)
    _write(tmp_path / "two/route_b/__init__.py")
    _write(tmp_path / "two/route_b/spec.py", spec)
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n[paths]\nsource_roots=["one", "two"]\ntest_roots=[]\n'
        "[skills]\nauto=false\nbuiltin=false\n[context]\nrepo_map=false\n"
        "[build]\nemit_stubs=false\n",
    )
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda _cfg: GoodBackend())

    rc = jaunt.cli.main(["build", "--root", str(tmp_path), "--no-progress"])

    assert rc == 0
    assert (tmp_path / "one/route_a/__generated__/spec.py").is_file()
    assert (tmp_path / "two/route_b/__generated__/spec.py").is_file()
    assert not (tmp_path / "one/route_b/__generated__/spec.py").exists()


def test_cli_test_generates_identical_test_modules_per_owner(tmp_path: Path, monkeypatch) -> None:
    import jaunt.cli
    from test_regressions_review_fixes import GoodBackend

    test_spec = (
        "import jaunt\n"
        "@jaunt.test()\n"
        "def test_smoke() -> None:\n"
        '    """Assert that True is true."""\n'
        "    ...\n"
    )
    for package in ("a", "b"):
        owner = tmp_path / f"packages/{package}"
        _write(
            owner / "pyproject.toml",
            f"[project]\nname='owner-{package}'\nversion='1'\n",
        )
        _write(owner / f"src/owner_{package}/__init__.py")
        _write(owner / "tests/__init__.py")
        _write(owner / "tests/test_spec.py", test_spec)
    _write(
        tmp_path / "jaunt.toml",
        'version=1\n[paths]\nsource_roots=["packages/*/src"]\n'
        'test_roots=["packages/*/tests"]\n'
        "[skills]\nauto=false\nbuiltin=false\n[context]\nrepo_map=false\n",
    )
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda _cfg: GoodBackend())

    original_path = list(sys.path)
    original_modules = dict(sys.modules)
    try:
        rc = jaunt.cli.main(
            ["test", "--root", str(tmp_path), "--no-build", "--no-run", "--no-progress"]
        )
    finally:
        sys.path[:] = original_path
        for name in list(sys.modules):
            if name not in original_modules and not name.startswith("jaunt"):
                del sys.modules[name]
        for name, module in original_modules.items():
            if not name.startswith("jaunt"):
                sys.modules[name] = module

    assert rc == 0
    assert (tmp_path / "packages/a/tests/__generated__/test_spec.py").is_file()
    assert (tmp_path / "packages/b/tests/__generated__/test_spec.py").is_file()


def test_targeted_test_discovery_expands_globbed_test_roots(tmp_path: Path) -> None:
    from jaunt.status_core import discover_targeted_test_entries

    _write(tmp_path / "packages/a/pyproject.toml", "[project]\nname='a'\nversion='1'\n")
    _write(tmp_path / "packages/a/src/app/__init__.py")
    _write(tmp_path / "packages/a/src/app/api.py", "def value(): return 1\n")
    _write(tmp_path / "packages/a/tests/__init__.py")
    _write(
        tmp_path / "packages/a/tests/test_api.py",
        "import jaunt\n"
        "from app.api import value\n"
        "@jaunt.test(targets=[value])\n"
        "def test_value(): ...\n",
    )
    cfg = _config(
        tmp_path,
        sources='["packages/*/src"]',
        tests='["packages/*/tests"]',
    )

    entries = discover_targeted_test_entries(root=tmp_path, cfg=cfg)

    assert [entry.module for entry in entries] == ["tests.test_api"]
    assert [str(ref) for ref in entries[0].decorator_kwargs["targets"]] == ["app.api:value"]
