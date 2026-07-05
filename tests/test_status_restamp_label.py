"""Status/check labeling of the free re-stamp (deterministic refreeze) case."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import jaunt.cli as cli


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _spec_project(tmp_path: Path, *, pkg: str, sig: str = "def greet(name: str) -> str:") -> None:
    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[build]\nemit_stubs = false\n',
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    _write(
        tmp_path / "src" / pkg / "specs.py",
        (f'import jaunt\n\n@jaunt.magic()\n{sig}\n    """Say hello."""\n    ...\n'),
    )


def _build_scheme2_module(tmp_path: Path, *, pkg: str, module_digest_override: str | None) -> None:
    """Write a generated module carrying scheme-2 spec_digests over the CURRENT specs.

    When ``module_digest_override`` is supplied, the header module_digest is set
    to a stale value so the module is detected stale while its per-spec
    structural/prose digests still match — the free re-stamp case.
    """
    from jaunt.builder import (
        _build_expected_names,
        _compute_spec_digests,
        build_module_context_artifacts,
        write_generated_module,
    )
    from jaunt.config import load_config
    from jaunt.deps import build_spec_graph, collapse_to_module_dag
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.generation_fingerprint import generation_fingerprint
    from jaunt.module_api import module_api_digest
    from jaunt.registry import clear_registries, get_magic_registry, get_specs_by_module

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
    real_digest = module_digest(module_name, entries, specs, spec_graph)
    write_generated_module(
        package_dir=tmp_path / "src",
        generated_dir="__generated__",
        module_name=module_name,
        source="def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": module_name,
            "module_digest": module_digest_override or real_digest,
            "generation_fingerprint": generation_fingerprint(
                load_config(root=tmp_path), kind="build"
            ),
            "module_context_digest": ctx_digest,
            "module_api_digest": module_api_digest(entries),
            "spec_refs": [str(e.spec_ref) for e in entries],
        },
        spec_digests=_compute_spec_digests(entries),
    )
    clear_registries()


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


def test_sig_alias_migration_labels_restamp(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "restamp_free"
    _spec_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(tmp_path / "src"))
    # Scheme-2 header whose per-spec digests match the current specs, but whose
    # module_digest is stale -> stale but refreeze-able without a model.
    _build_scheme2_module(tmp_path, pkg=pkg, module_digest_override="sha256:" + "0" * 64)

    rc = _run(["status", "--json", "--magic-only", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert f"{pkg}.specs" in out["stale"]
    assert out["stale_changes"][f"{pkg}.specs"] == "re-stamp"

    rc = _run(["status", "--magic-only", "--root", str(tmp_path)])
    text = capsys.readouterr().out
    assert rc == cli.EXIT_OK
    assert "re-stamp: free" in text


def test_structural_change_still_labels_structural(tmp_path: Path, monkeypatch, capsys) -> None:
    pkg = "restamp_structural"
    _spec_project(tmp_path, pkg=pkg)
    monkeypatch.chdir(tmp_path)
    sys.path.insert(0, str(tmp_path / "src"))
    _build_scheme2_module(tmp_path, pkg=pkg, module_digest_override=None)

    # Change the signature: structural digest diverges from the stored one.
    _spec_project(
        tmp_path,
        pkg=pkg,
        sig="def greet(name: str, greeting: str = 'Hi') -> str:",
    )

    rc = _run(["status", "--json", "--magic-only", "--root", str(tmp_path)])
    out = json.loads(capsys.readouterr().out)
    assert rc == cli.EXIT_OK
    assert f"{pkg}.specs" in out["stale"]
    assert out["stale_changes"][f"{pkg}.specs"] == "structural"
