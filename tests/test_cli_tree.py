import argparse
from pathlib import Path

from jaunt.cli import cmd_tree


def _project(tmp_path: Path) -> Path:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    src = tmp_path / "src" / "pkg"
    src.mkdir(parents=True)
    (src / "__init__.py").write_text('"""Pkg."""\n', encoding="utf-8")
    (src / "a.py").write_text('"""Module A."""\n', encoding="utf-8")
    return tmp_path


def _args(root: Path, **kw) -> argparse.Namespace:
    ns = argparse.Namespace(
        root=str(root),
        config=None,
        json_output=False,
        force=False,
        enrich=False,
        no_enrich=False,
        check=False,
    )
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_tree_creates_treedocs_yaml(tmp_path: Path) -> None:
    root = _project(tmp_path)
    rc = cmd_tree(_args(root))
    assert rc == 0
    assert (root / "treedocs.yaml").exists()
    text = (root / "treedocs.yaml").read_text(encoding="utf-8")
    assert "schema_version" in text and "src" in text


def test_tree_check_detects_drift(tmp_path: Path) -> None:
    root = _project(tmp_path)
    assert cmd_tree(_args(root)) == 0  # build the tree
    assert cmd_tree(_args(root, check=True)) == 0  # clean
    (root / "src" / "pkg" / "b.py").write_text('"""B."""\n', encoding="utf-8")
    assert cmd_tree(_args(root, check=True)) == 4  # new path -> drift, exit 4


def test_status_reports_tree_drift(tmp_path: Path, capsys) -> None:
    import argparse
    import sys

    from jaunt.cli import cmd_status, cmd_tree
    from jaunt.registry import clear_registries

    root = _project(tmp_path)
    cmd_tree(_args(root))
    (root / "src" / "pkg" / "c.py").write_text('"""C."""\n', encoding="utf-8")
    ns = argparse.Namespace(
        root=str(root),
        config=None,
        json_output=True,
        jobs=None,
        force=False,
        target=[],
        no_infer_deps=False,
        no_progress=True,
        no_cache=True,
    )
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())
    try:
        cmd_status(ns)
        out = capsys.readouterr().out
        assert "tree" in out.lower()
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]
