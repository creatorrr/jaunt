"""Tests for `jaunt status` command."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli
from jaunt.config import load_config
from jaunt.deps import collapse_to_module_dag
from jaunt.generation_fingerprint import generation_fingerprint


def test_parse_status_defaults() -> None:
    ns = jaunt.cli.parse_args(["status"])
    assert ns.command == "status"
    assert ns.json_output is False
    assert ns.force is False


def test_parse_status_flags() -> None:
    ns = jaunt.cli.parse_args(["status", "--json", "--root", "/tmp"])
    assert ns.json_output is True
    assert ns.root == "/tmp"


def test_main_dispatches_status(monkeypatch) -> None:
    monkeypatch.setattr(jaunt.cli, "cmd_status", lambda args: 0)
    assert jaunt.cli.main(["status"]) == 0


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_spec_project(tmp_path: Path, *, pkg: str = "statuspkg") -> None:
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (
            "import jaunt\n"
            "\n"
            "@jaunt.magic()\n"
            "def greet(name: str) -> str:\n"
            '    """Say hello."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )


def _build_generation_fingerprint(project_root: Path) -> str:
    return generation_fingerprint(load_config(root=project_root), kind="build")


def _build_module_context_digest(
    *,
    module_name: str,
    entries,
    module_specs,
    module_dag,
    package_dir: Path,
) -> str:
    from jaunt.builder import _build_expected_names, build_module_context_artifacts

    expected, _errs = _build_expected_names(entries)
    return build_module_context_artifacts(
        module_name=module_name,
        entries=entries,
        expected_names=expected,
        module_specs=module_specs,
        module_dag=module_dag,
        package_dir=package_dir,
        generated_dir="__generated__",
        targeted_test_entries={},
    ).digest


def _build_module_api_digest(entries) -> str:
    from jaunt.module_api import module_api_digest

    return module_api_digest(entries)


def test_cmd_status_no_specs(tmp_path: Path, monkeypatch, capsys) -> None:
    """Status on a project with no specs should succeed with empty modules."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "jaunt.toml", "version = 1\n")
    (tmp_path / "src").mkdir()

    ns = jaunt.cli.parse_args(["status", "--json"])
    rc = jaunt.cli.cmd_status(ns)
    assert rc == 0

    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["command"] == "status"
    assert data["ok"] is True
    assert data["stale"] == []
    assert data["fresh"] == []


def test_cmd_status_no_specs_non_json(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "jaunt.toml", "version = 1\n")
    (tmp_path / "src").mkdir()

    ns = jaunt.cli.parse_args(["status"])
    rc = jaunt.cli.cmd_status(ns)
    assert rc == 0

    captured = capsys.readouterr()
    assert "Status: 0 module(s) total" in captured.out
    assert "No magic specs discovered." in captured.out


def test_cmd_status_with_stale_specs(tmp_path: Path, monkeypatch, capsys) -> None:
    """Status should report stale modules when no generated files exist."""
    from jaunt.registry import clear_registries

    pkg = "statuspkg_stale"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        ns = jaunt.cli.parse_args(["status", "--json"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["command"] == "status"
        assert data["ok"] is True
        assert f"{pkg}.specs" in data["stale"]
        assert data["fresh"] == []
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_with_stale_specs_non_json(tmp_path: Path, monkeypatch, capsys) -> None:
    from jaunt.registry import clear_registries

    pkg = "statuspkg_stale_text"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        ns = jaunt.cli.parse_args(["status"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        assert "Status: 1 module(s) total" in captured.out
        assert "Stale (1):" in captured.out
        assert f"- {pkg}.specs" in captured.out
        assert "Fresh (0):" in captured.out
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_with_fresh_specs(tmp_path: Path, monkeypatch, capsys) -> None:
    """Status should report fresh modules when generated files have matching digests."""
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_fresh"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        # Discover and register specs to compute the digest
        mods = discover_modules(
            roots=[tmp_path / "src"],
            exclude=[],
            generated_dir="__generated__",
        )
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        entries = module_specs[f"{pkg}.specs"]
        digest = module_digest(f"{pkg}.specs", entries, specs, spec_graph)
        fingerprint = _build_generation_fingerprint(tmp_path)
        module_context_digest = _build_module_context_digest(
            module_name=f"{pkg}.specs",
            entries=entries,
            module_specs=module_specs,
            module_dag=collapse_to_module_dag(spec_graph),
            package_dir=tmp_path / "src",
        )
        module_api_digest = _build_module_api_digest(entries)

        # Write a generated file with matching digest
        write_generated_module(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_name=f"{pkg}.specs",
            source="def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
            header_fields={
                "tool_version": "0",
                "kind": "build",
                "source_module": f"{pkg}.specs",
                "module_digest": digest,
                "generation_fingerprint": fingerprint,
                "module_context_digest": module_context_digest,
                "module_api_digest": module_api_digest,
                "spec_refs": [str(e.spec_ref) for e in entries],
            },
        )

        clear_registries()
        # Remove cached modules so cmd_status can re-import and re-register
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]

        ns = jaunt.cli.parse_args(["status", "--json"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["command"] == "status"
        assert data["ok"] is True
        assert data["stale"] == []
        assert f"{pkg}.specs" in data["fresh"]
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_with_fresh_specs_non_json(tmp_path: Path, monkeypatch, capsys) -> None:
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_fresh_text"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        entries = module_specs[f"{pkg}.specs"]
        digest = module_digest(f"{pkg}.specs", entries, specs, spec_graph)
        fingerprint = _build_generation_fingerprint(tmp_path)
        module_context_digest = _build_module_context_digest(
            module_name=f"{pkg}.specs",
            entries=entries,
            module_specs=module_specs,
            module_dag=collapse_to_module_dag(spec_graph),
            package_dir=tmp_path / "src",
        )
        module_api_digest = _build_module_api_digest(entries)

        write_generated_module(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_name=f"{pkg}.specs",
            source="def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
            header_fields={
                "tool_version": "0",
                "kind": "build",
                "source_module": f"{pkg}.specs",
                "module_digest": digest,
                "generation_fingerprint": fingerprint,
                "module_context_digest": module_context_digest,
                "module_api_digest": module_api_digest,
                "spec_refs": [str(e.spec_ref) for e in entries],
            },
        )

        clear_registries()
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]

        ns = jaunt.cli.parse_args(["status"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        assert "Status: 1 module(s) total" in captured.out
        assert "Stale (0):" in captured.out
        assert "Fresh (1):" in captured.out
        assert f"- {pkg}.specs" in captured.out
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_missing_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    ns = jaunt.cli.parse_args(["status"])
    rc = jaunt.cli.cmd_status(ns)
    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY


def test_cmd_status_force_marks_all_stale(tmp_path: Path, monkeypatch, capsys) -> None:
    """The --force flag should mark all modules as stale regardless of digest."""
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_force"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        entries = module_specs[f"{pkg}.specs"]
        digest = module_digest(f"{pkg}.specs", entries, specs, spec_graph)
        fingerprint = _build_generation_fingerprint(tmp_path)

        write_generated_module(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_name=f"{pkg}.specs",
            source="def greet(name: str) -> str:\n    return 'hi'\n",
            header_fields={
                "tool_version": "0",
                "kind": "build",
                "source_module": f"{pkg}.specs",
                "module_digest": digest,
                "generation_fingerprint": fingerprint,
                "spec_refs": [str(e.spec_ref) for e in entries],
            },
        )

        clear_registries()
        # Remove cached modules so cmd_status can re-import and re-register
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]

        ns = jaunt.cli.parse_args(["status", "--json", "--force"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert f"{pkg}.specs" in data["stale"]
        assert data["fresh"] == []
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_marks_engine_switch_as_stale(tmp_path: Path, monkeypatch, capsys) -> None:
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_engine"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        entries = module_specs[f"{pkg}.specs"]
        digest = module_digest(f"{pkg}.specs", entries, specs, spec_graph)
        fingerprint = _build_generation_fingerprint(tmp_path)

        write_generated_module(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_name=f"{pkg}.specs",
            source="def greet(name: str) -> str:\n    return 'hi'\n",
            header_fields={
                "tool_version": "0",
                "kind": "build",
                "source_module": f"{pkg}.specs",
                "module_digest": digest,
                "generation_fingerprint": fingerprint,
                "spec_refs": [str(e.spec_ref) for e in entries],
            },
        )

        (tmp_path / "jaunt.toml").write_text(
            "\n".join(
                [
                    "version = 1",
                    "",
                    "[paths]",
                    'source_roots = ["src"]',
                    "",
                    "[agent]",
                    'engine = "aider"',
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        clear_registries()
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]
        ns = jaunt.cli.parse_args(["status", "--json"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert f"{pkg}.specs" in data["stale"]
        assert data["fresh"] == []
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]


def test_cmd_status_marks_api_changed_dependents_as_stale(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_api_dependents"
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "a_specs.py",
        (
            "import jaunt\n"
            "\n"
            "@jaunt.magic()\n"
            "def parse_name(raw: str) -> str:\n"
            '    """Parse a raw name."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )
    _write(
        tmp_path / "src" / pkg / "b_specs.py",
        (
            "import jaunt\n"
            "\n"
            f'@jaunt.magic(deps="{pkg}.a_specs:parse_name")\n'
            "def format_name(raw: str) -> str:\n"
            '    """Format a parsed name."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        fingerprint = _build_generation_fingerprint(tmp_path)

        for module_name, entries in module_specs.items():
            digest = module_digest(module_name, entries, specs, spec_graph)
            module_context_digest = _build_module_context_digest(
                module_name=module_name,
                entries=entries,
                module_specs=module_specs,
                module_dag=collapse_to_module_dag(spec_graph),
                package_dir=tmp_path / "src",
            )
            module_api_digest = _build_module_api_digest(entries)
            source = (
                "def parse_name(raw: str) -> str:\n    return raw.strip()\n"
                if module_name.endswith("a_specs")
                else "def format_name(raw: str) -> str:\n    return raw.title()\n"
            )
            write_generated_module(
                package_dir=tmp_path / "src",
                generated_dir="__generated__",
                module_name=module_name,
                source=source,
                header_fields={
                    "tool_version": "0",
                    "kind": "build",
                    "source_module": module_name,
                    "module_digest": digest,
                    "generation_fingerprint": fingerprint,
                    "module_context_digest": module_context_digest,
                    "module_api_digest": module_api_digest,
                    "spec_refs": [str(entry.spec_ref) for entry in entries],
                },
            )

        _write(
            tmp_path / "src" / pkg / "a_specs.py",
            (
                "import jaunt\n"
                "\n"
                "@jaunt.magic()\n"
                "def parse_name(raw: bytes) -> str:\n"
                '    """Parse a raw name."""\n'
                '    raise RuntimeError("stub")\n'
            ),
        )

        clear_registries()
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]

        ns = jaunt.cli.parse_args(["status", "--json"])
        rc = jaunt.cli.cmd_status(ns)
        assert rc == 0

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["stale"] == [f"{pkg}.a_specs", f"{pkg}.b_specs"]
        assert data["fresh"] == []
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for mod_name in list(sys.modules.keys()):
            if mod_name not in before_modules:
                del sys.modules[mod_name]
