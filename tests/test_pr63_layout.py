"""Layout/targeting fixes from the PR #63 review.

- Finding 4: targeted builds emit `.pyi` stubs only for the target closure.
- Finding 5: the root-level `__generated__/` is a PEP 420 namespace package.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import jaunt.cli
from test_regressions_review_fixes import (
    GoodBackend,
    _restore_modules,
    _write,
    _write_package_init,
)


def _make_two_module_project(root: Path) -> Path:
    project = root / "proj"
    _write(
        project / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
    )
    _write_package_init(project, "src/app")
    for name in ("mod_a", "mod_b"):
        _write(
            project / "src" / "app" / f"{name}.py",
            "import jaunt\n\n\n"
            "@jaunt.magic()\n"
            f"def {name}() -> None:\n"
            f'    """Do {name}."""\n'
            '    raise RuntimeError("spec stub")\n',
        )
    return project


def test_targeted_build_emits_only_target_stub(tmp_path: Path, monkeypatch) -> None:
    project = _make_two_module_project(tmp_path)
    before = {"app": sys.modules.get("app")}
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--target", "app.mod_a"])
    finally:
        sys.path[:] = orig_path
        _restore_modules(["app"], before=before)

    assert rc == jaunt.cli.EXIT_OK
    # The target's stub is emitted next to its spec module.
    assert (project / "src" / "app" / "mod_a.pyi").exists()
    # The unrelated, out-of-target module gets no stub.
    assert not (project / "src" / "app" / "mod_b.pyi").exists()


def _make_top_level_project(root: Path) -> Path:
    project = root / "proj"
    _write(project / "jaunt.toml", 'version = 1\n\n[paths]\nsource_roots = ["src"]\n')
    (project / "src").mkdir(parents=True, exist_ok=True)
    _write(
        project / "src" / "toplevelmod.py",
        "import jaunt\n\n\n"
        "@jaunt.magic()\n"
        "def flag() -> bool:\n"
        '    """Return True."""\n'
        '    raise RuntimeError("spec stub")\n',
    )
    return project


def test_top_level_build_uses_namespace_generated_dir(tmp_path: Path, monkeypatch) -> None:
    project = _make_top_level_project(tmp_path)
    before = {"toplevelmod": sys.modules.get("toplevelmod")}
    orig_path = list(sys.path)
    monkeypatch.setattr(jaunt.cli, "_build_backend", lambda cfg: GoodBackend())
    try:
        rc = jaunt.cli.main(["build", "--root", str(project), "--target", "toplevelmod"])
        assert rc == jaunt.cli.EXIT_OK

        gen_dir = project / "src" / "__generated__"
        assert (gen_dir / "toplevelmod.py").exists()
        # Root-level generated dir stays a namespace package (no __init__.py) so two
        # installed dists shipping a top-level __generated__ merge instead of shadowing.
        assert not (gen_dir / "__init__.py").exists()

        # The generated module still imports through the standard loader.
        sys.path.insert(0, str(project / "src"))
        importlib.invalidate_caches()
        mod = importlib.import_module("__generated__.toplevelmod")
        assert callable(mod.flag)
        assert mod.flag() is None  # GoodBackend emits a no-op body
    finally:
        sys.path[:] = orig_path
        _restore_modules(["toplevelmod", "__generated__"], before=before)


def test_two_roots_top_level_generated_merge(tmp_path: Path) -> None:
    """Two sys.path roots each holding `__generated__/<module>.py` (no __init__.py)
    are both importable — the PEP 420 namespace merge that the layout fix enables."""
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    (root_a / "__generated__").mkdir(parents=True)
    (root_b / "__generated__").mkdir(parents=True)
    (root_a / "__generated__" / "alpha.py").write_text("VALUE = 'a'\n", encoding="utf-8")
    (root_b / "__generated__" / "beta.py").write_text("VALUE = 'b'\n", encoding="utf-8")

    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        sys.path.insert(0, str(root_b))
        sys.path.insert(0, str(root_a))
        importlib.invalidate_caches()
        alpha = importlib.import_module("__generated__.alpha")
        beta = importlib.import_module("__generated__.beta")
        assert alpha.VALUE == "a"
        assert beta.VALUE == "b"
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]
