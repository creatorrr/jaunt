"""CLI surface for `jaunt migrate` — plan-by-default mechanical migrations."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import jaunt.cli as cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _run(argv: list[str]):
    from jaunt.registry import clear_registries

    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        return cli.main(argv)
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for name in list(sys.modules.keys()):
            if name not in before:
                del sys.modules[name]


class _GoodBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None, tuple[str, ...]]:
        lines = [f"def {name}(name: str) -> str:\n    return name\n" for name in ctx.expected_names]
        return "\n".join(lines).rstrip() + "\n", None, ()


class _ExplodingBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None, tuple[str, ...]]:
        raise AssertionError("migrate must never call the generation backend")


_JAUNT_TOML = 'version = 1\n\n[paths]\nsource_roots = ["src"]\n'


def _legacy_decorator_project(tmp_path: Path, *, pkg: str = "legpkg") -> str:
    """A decorator-governed spec with a legacy `raise RuntimeError("spec stub")` body."""
    _write(tmp_path / "jaunt.toml", _JAUNT_TOML)
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n\n"
            "@jaunt.magic()\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            '    raise RuntimeError("spec stub")\n'
        ),
    )
    return f"{pkg}.specs"


def _build(tmp_path: Path, monkeypatch, backend=None) -> None:
    monkeypatch.setattr(cli, "_build_backend", lambda cfg: backend or _GoodBackend())
    rc = _run(["build", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK


def _module_mode_project(tmp_path: Path, *, pkg: str = "modpkg") -> str:
    """A module-mode file with a governed `...` spec and an ungoverned legacy helper."""
    _write(tmp_path / "jaunt.toml", _JAUNT_TOML)
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "jaunt.magic_module(__name__)\n\n\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            "    ...\n\n\n"
            "def helper(x: int) -> int:\n"
            '    """Legacy helper."""\n'
            '    raise RuntimeError("spec stub")\n'
        ),
    )
    return f"{pkg}.specs"


def _pure_legacy_module_project(tmp_path: Path, *, pkg: str = "purepkg") -> str:
    """A module-mode file where EVERY candidate is a legacy `raise RuntimeError`
    body — so it has zero currently-governed specs (module mode does not treat
    that body as a stub)."""
    _write(tmp_path / "jaunt.toml", _JAUNT_TOML)
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "jaunt.magic_module(__name__)\n\n\n"
            "def alpha(x: int) -> int:\n"
            '    """Alpha."""\n'
            '    raise RuntimeError("spec stub")\n\n\n'
            "def beta(x: int) -> int:\n"
            '    """Beta."""\n'
            '    raise RuntimeError("spec stub")\n'
        ),
    )
    return f"{pkg}.specs"


def test_plan_covers_pure_legacy_module_file(tmp_path: Path, monkeypatch, capsys) -> None:
    # A module-mode file whose every symbol is a legacy body has no governed
    # specs yet, but migrate must still plan it (newly-governs entries) instead
    # of reporting "No pending migrations".
    module = _pure_legacy_module_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["migrate", "--json", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    symbols = {a["symbol"]: a for a in out["actions"] if a["module"] == module}
    assert symbols, "pure-legacy module file should produce plan entries"
    assert symbols["alpha"]["classification"] == "newly-governs"
    assert symbols["beta"]["classification"] == "newly-governs"


def test_plan_mode_lists_actions_and_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _legacy_decorator_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    rc = _run(["migrate", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert f"{module}.greet" in out
    assert "re-stamp" in out
    # Plan mode never edits the source.
    src = (tmp_path / "src" / "legpkg" / "specs.py").read_text(encoding="utf-8")
    assert 'raise RuntimeError("spec stub")' in src


def test_plan_json_shape(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _legacy_decorator_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    rc = _run(["migrate", "--json", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert out["command"] == "migrate"
    assert out["ok"] is True
    assert out["applied"] is False
    action = next(a for a in out["actions"] if a["symbol"] == "greet")
    assert action["module"] == module
    assert action["classification"] == "re-stamp"
    assert set(action) >= {
        "migration",
        "path",
        "module",
        "symbol",
        "kind",
        "classification",
        "description",
    }


def test_apply_refuses_dirty_tree_without_force(tmp_path: Path, monkeypatch, capsys) -> None:
    _legacy_decorator_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    # Untracked files (the generated tree, sources) make the tree dirty.
    monkeypatch.chdir(tmp_path)
    rc = _run(["migrate", "--apply", "--root", str(tmp_path)])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_CONFIG_OR_DISCOVERY
    assert "dirty" in err.lower() or "working tree" in err.lower()


def test_apply_rewrites_restamps_and_reemits_stub(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _legacy_decorator_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    src_path = tmp_path / "src" / "legpkg" / "specs.py"
    stub_path = tmp_path / "src" / "legpkg" / "specs.pyi"
    assert stub_path.exists()
    monkeypatch.chdir(tmp_path)

    # Apply with an exploding backend proves no model/generation call happens.
    monkeypatch.setattr(cli, "_build_backend", lambda cfg: _ExplodingBackend())
    rc = _run(["migrate", "--apply", "--force", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    capsys.readouterr()

    body = src_path.read_text(encoding="utf-8")
    assert 'raise RuntimeError("spec stub")' not in body
    assert '"""Say hello."""' in body
    assert "..." in body

    # After migration the module is FRESH with zero model calls.
    rc = _run(["status", "--json", "--magic-only", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert module in out["fresh"]
    assert out["stale"] == []


def test_apply_skips_newly_governs_without_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    _module_mode_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    src_path = tmp_path / "src" / "modpkg" / "specs.py"
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli, "_build_backend", lambda cfg: _ExplodingBackend())
    rc = _run(["migrate", "--apply", "--force", "--root", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "SKIPPED" in out
    # The ungoverned helper keeps its legacy body (rewriting would newly govern it).
    body = src_path.read_text(encoding="utf-8")
    assert 'raise RuntimeError("spec stub")' in body


def test_apply_allow_newly_governed_rewrites(tmp_path: Path, monkeypatch, capsys) -> None:
    _module_mode_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    src_path = tmp_path / "src" / "modpkg" / "specs.py"
    monkeypatch.chdir(tmp_path)

    monkeypatch.setattr(cli, "_build_backend", lambda cfg: _ExplodingBackend())
    rc = _run(["migrate", "--apply", "--force", "--allow-newly-governed", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    body = src_path.read_text(encoding="utf-8")
    assert 'raise RuntimeError("spec stub")' not in body


def test_stub_reemit_migration_clears_stub_staleness(tmp_path: Path, monkeypatch, capsys) -> None:
    _legacy_decorator_project(tmp_path)
    _build(tmp_path, monkeypatch)
    capsys.readouterr()
    from jaunt import stub_emitter

    src_file = tmp_path / "src" / "legpkg" / "specs.py"
    stub_path = tmp_path / "src" / "legpkg" / "specs.pyi"
    gen_source = (tmp_path / "src" / "legpkg" / "__generated__" / "specs.py").read_text(
        encoding="utf-8"
    )

    # Corrupt the stub's inputs digest so it reads as stale (simulating a format bump).
    text = stub_path.read_text(encoding="utf-8")
    text = text.replace("# jaunt:inputs_digest=sha256:", "# jaunt:inputs_digest=sha256:deadbeef", 1)
    stub_path.write_text(text, encoding="utf-8")
    assert (
        stub_emitter.stub_staleness(source_file=str(src_file), generated_source=gen_source)
        is not None
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_build_backend", lambda cfg: _ExplodingBackend())
    rc = _run(["migrate", "--apply", "--force", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK

    gen_source = (tmp_path / "src" / "legpkg" / "__generated__" / "specs.py").read_text(
        encoding="utf-8"
    )
    assert (
        stub_emitter.stub_staleness(source_file=str(src_file), generated_source=gen_source) is None
    )
