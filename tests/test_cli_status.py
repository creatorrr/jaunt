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


def test_status_json_previews_generation_fanout_and_seeded_skills(tmp_path: Path, capsys) -> None:
    from jaunt.registry import clear_registries

    pkg = "status_plan_pkg"
    _make_spec_project(tmp_path, pkg=pkg)
    before_modules = set(sys.modules)
    orig_path = list(sys.path)
    try:
        rc = jaunt.cli.main(["status", "--json", "--root", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        plan = payload["generation_plan"]
        assert plan["candidate_modules"] == [f"{pkg}.specs"]
        assert plan["max_attempts_per_unit"] >= 2
        assert plan["max_generation_attempts"] >= 2
        module_plan = plan["modules"][f"{pkg}.specs"]
        assert module_plan["initial_generation_units"] == 1
        assert module_plan["strategy"] == "monolithic"
        assert module_plan["fallback_condition"] == "none"
        assert "not prompt tokens" in plan["skills_workspace_seeded"]["note"]
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


def test_status_explains_split_components_and_fallback_condition(tmp_path: Path, capsys) -> None:
    from jaunt.registry import clear_registries

    pkg = "status_split_plan_pkg"
    _make_spec_project(tmp_path, pkg=pkg)
    spec_path = tmp_path / "src" / pkg / "specs.py"
    spec_path.write_text(
        spec_path.read_text(encoding="utf-8")
        + "\n\n@jaunt.magic()\n"
        + "def farewell(name: str) -> str:\n"
        + '    """Say goodbye."""\n'
        + '    raise RuntimeError("stub")\n',
        encoding="utf-8",
    )
    before_modules = set(sys.modules)
    orig_path = list(sys.path)
    try:
        assert jaunt.cli.main(["status", "--json", "--root", str(tmp_path)]) == 0
        payload = json.loads(capsys.readouterr().out)
        module_plan = payload["generation_plan"]["modules"][f"{pkg}.specs"]
        assert module_plan["initial_generation_units"] == 2
        assert module_plan["split_fallback_units"] == 1
        assert module_plan["strategy"] == "split-components"
        assert (
            module_plan["fallback_condition"]
            == "any split component, merge, or whole-module validation fails"
        )

        assert jaunt.cli.main(["status", "--json", "--jobs", "1", "--root", str(tmp_path)]) == 0
        serial_payload = json.loads(capsys.readouterr().out)
        serial_plan = serial_payload["generation_plan"]["modules"][f"{pkg}.specs"]
        assert serial_plan["initial_generation_units"] == 1
        assert serial_plan["split_fallback_units"] == 0
        assert serial_plan["strategy"] == "monolithic"

        assert jaunt.cli.main(["status", "--root", str(tmp_path)]) == 0
        output = capsys.readouterr().out
        assert f"- {pkg}.specs: 2 split component unit(s)" in output
        assert "1 monolithic fallback unit(s)" in output
        assert "if any split component, merge, or whole-module validation fails" in output
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


def test_doctor_json_is_read_only_and_wraps_status(tmp_path: Path, monkeypatch, capsys) -> None:
    from jaunt.registry import clear_registries

    pkg = "doctor_pkg"
    _make_spec_project(tmp_path, pkg=pkg)
    monkeypatch.setattr(
        jaunt.cli,
        "_doctor_command_probe",
        lambda argv: {"available": True, "detail": f"{argv[0]} test"},
    )
    before_files = sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*"))
    before_modules = set(sys.modules)
    orig_path = list(sys.path)
    try:
        rc = jaunt.cli.main(["doctor", "--json", "--root", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["command"] == "doctor"
        assert payload["ok"] is True
        assert payload["read_only"] is True
        assert payload["model_calls"] == 0
        assert payload["status"]["stale"] == [f"{pkg}.specs"]
        assert payload["status"]["generation_plan"]["candidate_modules"] == [f"{pkg}.specs"]
        assert payload["findings"] == ["1 stale Jaunt module(s)"]
        assert sorted(path.relative_to(tmp_path) for path in tmp_path.rglob("*")) == before_files
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


def test_doctor_treats_successful_not_authenticated_probe_as_failure(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from jaunt.registry import clear_registries

    _make_spec_project(tmp_path, pkg="doctor_auth_pkg")

    def probe(argv: list[str]) -> dict[str, object]:
        if argv == ["codex", "login", "status"]:
            return {"available": True, "detail": "Not authenticated"}
        return {"available": True, "detail": f"{argv[0]} test"}

    monkeypatch.setattr(jaunt.cli, "_doctor_command_probe", probe)
    before_modules = set(sys.modules)
    orig_path = list(sys.path)
    try:
        rc = jaunt.cli.main(["doctor", "--json", "--root", str(tmp_path)])
        payload = json.loads(capsys.readouterr().out)

        assert rc == 0
        assert payload["environment"]["codex_auth"] == {
            "available": False,
            "detail": "Not authenticated",
        }
        assert "Codex is not authenticated; run codex login" in payload["findings"]
    finally:
        clear_registries()
        sys.path[:] = orig_path
        for name in list(sys.modules):
            if name not in before_modules:
                del sys.modules[name]


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_spec_project(tmp_path: Path, *, pkg: str = "statuspkg") -> None:
    # These fixtures hand-write generated files without going through stub
    # emission, so they opt out of `.pyi` freshness (covered in test_stub_freshness).
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
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


def test_status_json_exposes_module_digests_and_magic_only(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from jaunt.registry import clear_registries

    pkg = "statuspkg_digest"
    _make_spec_project(tmp_path, pkg=pkg)

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        rc = jaunt.cli.main(["status", "--json", "--magic-only"])
        assert rc == 0

        payload = json.loads(capsys.readouterr().out)
        assert payload["stale"]
        for module in payload["stale"]:
            assert payload["digests"][module]
        assert "contracts" not in payload
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
        assert f"- {pkg}.specs: 1 monolithic generation unit(s)" in captured.out
        assert "no split fallback" in captured.out
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
                    'engine = "codex"',
                    "",
                    "[codex]",
                    'reasoning_effort = "medium"',
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


def test_adding_sibling_spec_keeps_built_module_fresh(tmp_path: Path, monkeypatch, capsys) -> None:
    """Finding 14: a new sibling spec must not restale an already-built module.

    Build module A (generated file digested while A is the only spec), then add a
    brand-new sibling spec module B. A must stay Fresh (only B is stale/unbuilt).
    """
    from jaunt.builder import write_generated_module
    from jaunt.deps import build_spec_graph
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

    pkg = "statuspkg_sibling"
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "a_specs.py",
        (
            "import jaunt\n\n"
            "@jaunt.magic()\n"
            "def alpha(name: str) -> str:\n"
            '    """Alpha."""\n'
            '    raise RuntimeError("stub")\n'
        ),
    )

    monkeypatch.chdir(tmp_path)
    orig_path = list(sys.path)
    before_modules = set(sys.modules.keys())

    try:
        sys.path.insert(0, str(tmp_path / "src"))
        clear_registries()

        # Build A while it is the only spec module in the package.
        mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
        import_and_collect(mods, kind="magic")
        specs = dict(get_magic_registry())
        spec_graph = build_spec_graph(specs, infer_default=False)
        module_specs = get_specs_by_module("magic")
        entries = module_specs[f"{pkg}.a_specs"]
        digest = module_digest(f"{pkg}.a_specs", entries, specs, spec_graph)
        fingerprint = _build_generation_fingerprint(tmp_path)
        module_context_digest = _build_module_context_digest(
            module_name=f"{pkg}.a_specs",
            entries=entries,
            module_specs=module_specs,
            module_dag=collapse_to_module_dag(spec_graph),
            package_dir=tmp_path / "src",
        )
        module_api_digest = _build_module_api_digest(entries)

        write_generated_module(
            package_dir=tmp_path / "src",
            generated_dir="__generated__",
            module_name=f"{pkg}.a_specs",
            source="def alpha(name: str) -> str:\n    return name\n",
            header_fields={
                "tool_version": "0",
                "kind": "build",
                "source_module": f"{pkg}.a_specs",
                "module_digest": digest,
                "generation_fingerprint": fingerprint,
                "module_context_digest": module_context_digest,
                "module_api_digest": module_api_digest,
                "spec_refs": [str(e.spec_ref) for e in entries],
            },
        )

        # Now a new sibling spec module lands in the same package.
        _write(
            tmp_path / "src" / pkg / "b_specs.py",
            (
                "import jaunt\n\n"
                "@jaunt.magic()\n"
                "def beta(name: str) -> str:\n"
                '    """Beta."""\n'
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

        data = json.loads(capsys.readouterr().out)
        assert f"{pkg}.a_specs" in data["fresh"]
        assert f"{pkg}.a_specs" not in data["stale"]
        assert f"{pkg}.b_specs" in data["stale"]
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
