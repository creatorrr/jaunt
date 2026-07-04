"""`jaunt status` flags a missing/stale `.pyi` when `[build] emit_stubs = true`."""

from __future__ import annotations

import sys
from pathlib import Path

from jaunt.config import load_config
from jaunt.deps import build_spec_graph, collapse_to_module_dag
from jaunt.generation_fingerprint import generation_fingerprint
from jaunt.status_core import compute_magic_status


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_fresh_built_module(tmp_path: Path, pkg: str, *, emit_stubs: bool) -> tuple[Path, Path]:
    """Create a project with one hand-built, digest-fresh generated module.

    Returns (spec_source_file, generated_source_content is written to disk).
    """
    from jaunt.builder import (
        _build_expected_names,
        build_module_context_artifacts,
        write_generated_module,
    )
    from jaunt.digest import module_digest
    from jaunt.discovery import discover_modules, import_and_collect
    from jaunt.module_api import module_api_digest
    from jaunt.registry import (
        clear_registries,
        get_magic_registry,
        get_specs_by_module,
    )

    _write(
        tmp_path / "jaunt.toml",
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n'
        f"[build]\nemit_stubs = {'true' if emit_stubs else 'false'}\n",
    )
    _write(tmp_path / "src" / pkg / "__init__.py", "")
    spec_file = tmp_path / "src" / pkg / "specs.py"
    _write(
        spec_file,
        "import jaunt\n\n"
        "@jaunt.magic()\n"
        "def greet(name: str) -> str:\n"
        '    """Say hello."""\n'
        '    raise RuntimeError("stub")\n',
    )

    sys.path.insert(0, str(tmp_path / "src"))
    clear_registries()
    mods = discover_modules(roots=[tmp_path / "src"], exclude=[], generated_dir="__generated__")
    import_and_collect(mods, kind="magic")
    specs = dict(get_magic_registry())
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = get_specs_by_module("magic")
    module_name = f"{pkg}.specs"
    entries = module_specs[module_name]
    expected, _ = _build_expected_names(entries)
    digest = module_digest(module_name, entries, specs, spec_graph)
    fingerprint = generation_fingerprint(load_config(root=tmp_path), kind="build")
    ctx_digest = build_module_context_artifacts(
        module_name=module_name,
        entries=entries,
        expected_names=expected,
        module_specs=module_specs,
        module_dag=collapse_to_module_dag(spec_graph),
        package_dir=tmp_path / "src",
        generated_dir="__generated__",
        targeted_test_entries={},
    ).digest

    generated = "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n"
    write_generated_module(
        package_dir=tmp_path / "src",
        generated_dir="__generated__",
        module_name=module_name,
        source=generated,
        header_fields={
            "tool_version": "0",
            "kind": "build",
            "source_module": module_name,
            "module_digest": digest,
            "generation_fingerprint": fingerprint,
            "module_context_digest": ctx_digest,
            "module_api_digest": module_api_digest(entries),
            "spec_refs": [str(e.spec_ref) for e in entries],
        },
    )
    clear_registries()
    return spec_file, tmp_path / "src" / pkg / "__generated__" / "specs.py"


def _status(tmp_path: Path):
    cfg = load_config(root=tmp_path)
    return compute_magic_status(
        root=tmp_path,
        cfg=cfg,
        source_dirs=[tmp_path / "src"],
        build_instructions=[],
        include_target_tests=False,
        infer_deps=False,
    )


def test_missing_stub_marks_module_stale(tmp_path: Path, monkeypatch) -> None:
    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        _make_fresh_built_module(tmp_path, "stubpkg_missing", emit_stubs=True)
        st = _status(tmp_path)
        assert "stubpkg_missing.specs" in st.stale
        assert st.stale_changes.get("stubpkg_missing.specs") == "stub"
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]


def test_matching_stub_keeps_module_fresh(tmp_path: Path, monkeypatch) -> None:
    from jaunt.header import format_stub_header
    from jaunt.stub_emitter import (
        build_stub_source,
        generated_content_digest,
        stub_path_for_source,
    )

    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        spec_file, gen_file = _make_fresh_built_module(tmp_path, "stubpkg_fresh", emit_stubs=True)
        gen_source = gen_file.read_text(encoding="utf-8")
        header = format_stub_header(
            tool_version="0",
            source_module="stubpkg_fresh.specs",
            generated_digest=generated_content_digest(gen_source),
        )
        stub = build_stub_source(spec_file.read_text(encoding="utf-8"), gen_source, set(), header)
        stub_path_for_source(spec_file).write_text(stub, encoding="utf-8")

        st = _status(tmp_path)
        assert "stubpkg_fresh.specs" in st.fresh
        assert "stubpkg_fresh.specs" not in st.stale
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]


def test_missing_stub_ignored_when_emit_stubs_false(tmp_path: Path, monkeypatch) -> None:
    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        _make_fresh_built_module(tmp_path, "stubpkg_off", emit_stubs=False)
        st = _status(tmp_path)
        assert "stubpkg_off.specs" in st.fresh
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]
