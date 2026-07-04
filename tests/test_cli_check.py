from __future__ import annotations

import json
import sys

import pytest

from pathlib import Path

from jaunt import cli
from jaunt.contract.battery import render_battery
from jaunt.digest import contract_digests

SRC = '''
import jaunt


@jaunt.contract
def shout(text: str) -> str:
    """
    Uppercase a string.

    Examples:
    - "hi" -> "HI"

    Raises:
    - "" raises ValueError
    """
    if not text:
        raise ValueError("empty")
    return text.upper()
'''


def _project(tmp_path: Path, *, prose_digest_override: str | None = None) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    digs = contract_digests(str(tmp_path / "src" / "demo.py"), "shout")
    battery_dir = tmp_path / "tests" / "contract" / "demo"
    battery_dir.mkdir(parents=True)
    region_examples = (
        '@pytest.mark.parametrize("arg,want", [("hi", "HI")])\n'
        "def test_examples(arg, want):  # derived from: Examples\n"
        "    assert shout(arg) == want"
    )
    region_errors = (
        '@pytest.mark.parametrize("arg", [""])\n'
        "def test_raises_valueerror(arg):  # derived from: Raises\n"
        "    with pytest.raises(ValueError):\n"
        "        shout(arg)"
    )
    from jaunt.contract.battery import DerivedRegion

    text = render_battery(
        import_module="demo",
        func_name="shout",
        regions=[
            DerivedRegion("examples", region_examples),
            DerivedRegion("errors", region_errors),
        ],
        header_fields={
            "derived_from": "demo:shout",
            "prose_digest": prose_digest_override or digs.prose,
            "signature": digs.signature,
            "body_digest": digs.body,
            "strength": "3/3",
            "tool_version": "0.4.4",
        },
    )
    (battery_dir / "test_shout.py").write_text(text, encoding="utf-8")
    return tmp_path


def test_check_passes_when_in_sync(tmp_path: Path, capsys, monkeypatch) -> None:
    root = _project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_OK


def test_check_blocks_on_stale_prose(tmp_path: Path) -> None:
    root = _project(tmp_path, prose_digest_override="sha256:deadbeef")
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE


def test_check_blocks_when_unbuilt(tmp_path: Path) -> None:
    root = _project(tmp_path)
    # Remove the battery -> unbuilt.
    (root / "tests" / "contract" / "demo" / "test_shout.py").unlink()
    args = cli.parse_args(["check", "--root", str(root)])
    assert cli.cmd_check(args) == cli.EXIT_PYTEST_FAILURE


# --- Finding 11: `jaunt check` gates @jaunt.magic freshness -------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _magic_project(tmp_path: Path, *, pkg: str) -> None:
    """A magic-only project (no @jaunt.contract) with one unbuilt spec module."""
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "@jaunt.magic()\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )


def _build_fresh_magic(tmp_path: Path, *, pkg: str) -> None:
    """Write a fresh generated module with matching provenance digests."""
    from jaunt.builder import (
        _build_expected_names,
        build_module_context_artifacts,
        write_generated_module,
    )
    from jaunt.config import load_config
    from jaunt.deps import build_spec_graph, collapse_to_module_dag
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.generation_fingerprint import generation_fingerprint
    from jaunt.module_api import module_api_digest
    from jaunt.registry import (
        clear_registries,
        get_magic_registry,
        get_specs_by_module,
    )

    clear_registries()
    mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
    import_and_collect(mods, kind="magic")
    specs = dict(get_magic_registry())
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = get_specs_by_module("magic")
    module_name = f"{pkg}.specs"
    entries = module_specs[module_name]
    expected, _errs = _build_expected_names(entries)
    ctx_digest = build_module_context_artifacts(
        module_name=module_name,
        entries=entries,
        expected_names=expected,
        module_specs=module_specs,
        module_dag=collapse_to_module_dag(spec_graph),
        package_dir=tmp_path / "src",
        generated_dir="__generated__",
    ).digest
    write_generated_module(
        package_dir=tmp_path / "src",
        generated_dir="__generated__",
        module_name=module_name,
        source="def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": module_name,
            "module_digest": module_digest(module_name, entries, specs, spec_graph),
            "generation_fingerprint": generation_fingerprint(
                load_config(root=tmp_path), kind="build"
            ),
            "module_context_digest": ctx_digest,
            "module_api_digest": module_api_digest(entries),
            "spec_refs": [str(e.spec_ref) for e in entries],
        },
    )
    clear_registries()


def _run_check(argv: list[str]):
    from jaunt.registry import clear_registries

    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())
    try:
        return cli.main(argv)
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_check_contracts_only_and_magic_only_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(["check", "--contracts-only", "--magic-only"])


def test_check_default_gates_unbuilt_magic(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "checkmagic_unbuilt"
    _magic_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)

    rc = _run_check(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert out["ok"] is False
    assert f"{pkg}.specs" in out["magic"]["unbuilt"]
    assert out["magic"]["fresh"] == []


def test_check_contracts_only_ignores_stale_magic(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "checkmagic_contracts_only"
    _magic_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)

    rc = _run_check(["check", "--contracts-only", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert out["ok"] is True
    assert "magic" not in out


def test_check_magic_only_gates_stale_magic(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "checkmagic_magic_only"
    _magic_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)

    rc = _run_check(["check", "--magic-only", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_PYTEST_FAILURE
    assert out["ok"] is False
    assert f"{pkg}.specs" in out["magic"]["unbuilt"]
    # --magic-only suppresses the contract block.
    assert "checked" not in out


def test_check_default_passes_with_fresh_magic(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "checkmagic_fresh"
    _magic_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(tmp_path / "src"))
    _build_fresh_magic(tmp_path, pkg=pkg)

    rc = _run_check(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK, out
    assert out["ok"] is True
    assert f"{pkg}.specs" in out["magic"]["fresh"]
    assert out["magic"]["stale"] == {}
    assert out["magic"]["unbuilt"] == []


def test_check_default_gates_stale_magic_after_spec_edit(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """Build fresh provenance, then edit the spec signature; check must exit 4 and
    report the module under magic.stale (not merely unbuilt)."""
    pkg = "checkmagic_drift"
    _magic_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(tmp_path / "src"))
    _build_fresh_magic(tmp_path, pkg=pkg)

    # Edit the spec signature so the recomputed module digest diverges from the
    # digest baked into the (still-present) generated module header.
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n\n"
            "@jaunt.magic()\n"
            "def greet(name: str, greeting: str = 'Hello') -> str:\n"
            '    """Say hello with a configurable greeting."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )

    rc = _run_check(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_PYTEST_FAILURE, out
    assert out["ok"] is False
    assert f"{pkg}.specs" in out["magic"]["stale"]
    assert f"{pkg}.specs" not in out["magic"]["fresh"]
    assert f"{pkg}.specs" not in out["magic"]["unbuilt"]


def test_check_magic_free_project_exits_zero(tmp_path: Path, monkeypatch, capsys) -> None:
    """A contract-only, in-sync project stays exit 0 under the new default."""
    root = _project(tmp_path)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rc = _run_check(["check", "--root", str(root), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert out["ok"] is True
    # No magic specs -> empty magic block, never blocking.
    assert out["magic"] == {"fresh": [], "stale": {}, "unbuilt": []}
