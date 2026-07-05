"""CLI surfacing of reconciliation: orphan gate, clean --orphans, newly-governed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli as cli
from jaunt.generate.base import GeneratorBackend, ModuleSpecContext
from jaunt.header import format_contract_battery_header, format_header


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
            # Evict freshly-imported PROJECT modules, but never framework modules:
            # dropping e.g. jaunt.contract.drift would split its enum identity for
            # later tests that call cmd_check directly (leaving is_blocking stale).
            if name not in before and not (name == "jaunt" or name.startswith("jaunt.")):
                del sys.modules[name]


def _orphan_project(tmp_path: Path, *, pkg: str = "orphpkg") -> tuple[str, Path]:
    """A project whose only magic artifact has no surviving spec (orphan)."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    gen = tmp_path / "src" / pkg / "__generated__"
    gen.mkdir(parents=True, exist_ok=True)
    (gen / "__init__.py").write_text("", encoding="utf-8")
    module = f"{pkg}.specs"
    header = format_header(
        tool_version="0",
        kind="build",
        source_module=module,
        module_digest="deadbeef",
        spec_refs=[f"{module}:greet"],
    )
    orphan = gen / "specs.py"
    orphan.write_text(header + "\n\ndef greet(name):\n    return name\n", encoding="utf-8")
    return module, orphan


def test_check_exit_4_and_names_fix_on_orphan(tmp_path: Path, monkeypatch, capsys) -> None:
    module, _orphan = _orphan_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["check", "--magic-only", "--root", str(tmp_path)])
    text = capsys.readouterr().out
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert "orphaned artifact" in text
    assert module in text
    assert "jaunt clean --orphans" in text


def test_check_json_lists_orphans_under_magic(tmp_path: Path, monkeypatch, capsys) -> None:
    module, orphan = _orphan_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["check", "--magic-only", "--json", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert out["ok"] is False
    rel = str(orphan.relative_to(tmp_path))
    assert rel in out["magic"]["orphans"]


def test_clean_orphans_removes_only_orphans_and_journals(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _module, orphan = _orphan_project(tmp_path)
    # A non-orphan handwritten sibling that must survive.
    keeper = tmp_path / "src" / "orphpkg" / "keep.py"
    keeper.write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "JAUNT_LOG").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    rc = _run(["clean", "--orphans", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    assert not orphan.exists()
    assert keeper.exists()
    log = (tmp_path / "JAUNT_LOG").read_text(encoding="utf-8")
    assert "orphan-removed" in log


def test_clean_orphans_dry_run_deletes_nothing(tmp_path: Path, monkeypatch, capsys) -> None:
    _module, orphan = _orphan_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["clean", "--orphans", "--dry-run", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    assert orphan.exists()


def test_plain_clean_behavior_unchanged(tmp_path: Path, monkeypatch) -> None:
    _module, orphan = _orphan_project(tmp_path)
    gen_dir = orphan.parent
    monkeypatch.chdir(tmp_path)
    rc = _run(["clean", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    # Plain clean removes the whole generated directory.
    assert not gen_dir.exists()


def _module_spec_project(tmp_path: Path, *, pkg: str = "modspec") -> str:
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "jaunt.magic_module(__name__)\n\n\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            "    ...\n"
        ),
    )
    return f"{pkg}.specs"


def _generated_test_project(tmp_path: Path, *, with_spec: bool = True) -> Path:
    """A project with a generated test under the test-root's __generated__ dir.

    Uses default roots (source_roots = ["src", "."], test_roots = ["tests"]), the
    configuration under which the test-root generated dir nests inside the "."
    source root — the case that used to misclassify valid generated tests.
    """
    _write(tmp_path / "jaunt.toml", "version = 1\n")
    test_module = "tests.test_greet"
    if with_spec:
        _write(
            tmp_path / "tests" / "test_greet.py",
            (
                "import jaunt\n\n\n"
                "@jaunt.test\n"
                "def test_greet_says_hello():\n"
                '    """Greeting works."""\n'
                "    ...\n"
            ),
        )
    gen = tmp_path / "tests" / "__generated__"
    gen.mkdir(parents=True, exist_ok=True)
    header = format_header(
        tool_version="0",
        kind="test",
        source_module=test_module,
        module_digest="deadbeef",
        spec_refs=[f"{test_module}:test_greet_says_hello"],
    )
    gen_test = gen / "test_greet.py"
    gen_test.write_text(header + "\n\ndef test_greet_says_hello():\n    assert True\n", "utf-8")
    return gen_test


def test_valid_generated_test_not_orphaned_default_roots(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    gen_test = _generated_test_project(tmp_path, with_spec=True)
    monkeypatch.chdir(tmp_path)
    rc = _run(["clean", "--orphans", "--dry-run", "--json", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    rel = str(gen_test.relative_to(tmp_path))
    assert rel not in out["would_remove"], out["would_remove"]


def test_orphaned_generated_test_detected_and_removed(tmp_path: Path, monkeypatch, capsys) -> None:
    gen_test = _generated_test_project(tmp_path, with_spec=False)
    monkeypatch.chdir(tmp_path)
    rc = _run(["clean", "--orphans", "--root", str(tmp_path)])
    assert rc == cli.EXIT_OK
    assert not gen_test.exists()


def _battery_orphan_project(tmp_path: Path) -> Path:
    """A contract battery whose derived-from spec no longer exists."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
    )
    _write(tmp_path / "src" / "app" / "__init__.py", "")
    battery = tmp_path / "tests" / "contract"
    battery.mkdir(parents=True, exist_ok=True)
    header = format_contract_battery_header(
        derived_from="app.gone:vanished",
        prose_digest="aa",
        signature="() -> None",
        body_digest="bb",
        strength="0",
        tool_version="0",
    )
    path = battery / "test_vanished.py"
    path.write_text(header + "\n\ndef test_x():\n    assert True\n", encoding="utf-8")
    return path


def test_check_contracts_only_gates_orphaned_battery(tmp_path: Path, monkeypatch, capsys) -> None:
    _battery_orphan_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["check", "--contracts-only", "--root", str(tmp_path)])
    text = capsys.readouterr().out
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert "orphaned artifact" in text


def test_specs_json_newly_governed_flag(tmp_path: Path, monkeypatch, capsys) -> None:
    module = _module_spec_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    rc = _run(["specs", "--json", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    entry = next(s for s in out["specs"] if s["ref"] == f"{module}:greet")
    assert entry["origin"] == "module"
    assert entry["newly_governed"] is True


class _GoodBackend(GeneratorBackend):
    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None, tuple[str, ...]]:
        lines = [f"def {name}(name: str) -> str:\n    return name\n" for name in ctx.expected_names]
        return "\n".join(lines).rstrip() + "\n", None, ()


def test_build_plan_prints_newly_governed_before_generation(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    module = _module_spec_project(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_build_backend", lambda cfg: _GoodBackend())
    rc = _run(["build", "--root", str(tmp_path)])
    text = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert f"newly governed by module scan: {module}.greet — first build" in text
