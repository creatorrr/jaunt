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


def _make_fresh_built_module(
    tmp_path: Path, pkg: str, *, emit_stubs: bool, extra_spec: str = ""
) -> tuple[Path, Path]:
    """Create a project with one hand-built, digest-fresh generated module.

    ``extra_spec`` is prepended handwritten source (e.g. a helper) — default "" keeps
    the spec byte-identical for existing callers.

    Returns (spec_source_file, generated_source_content is written to disk).
    """
    from jaunt.builder import (
        _build_expected_names,
        build_module_context_artifacts,
        write_generated_module,
    )
    from jaunt.digest import module_digest, prose_digest, structural_digest
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
        f"{extra_spec}"
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
            "spec_digests": {
                str(e.spec_ref): {"s": structural_digest(e), "p": prose_digest(e)} for e in entries
            },
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
        format_stub_best_effort,
        generated_content_digest,
        stub_inputs_digest,
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
            inputs_digest=stub_inputs_digest(spec_file.read_text(encoding="utf-8"), gen_source),
        )
        stub = build_stub_source(spec_file.read_text(encoding="utf-8"), gen_source, set(), header)
        stub_path = stub_path_for_source(spec_file)
        stub_path.write_text(format_stub_best_effort(stub, filename=stub_path), encoding="utf-8")

        st = _status(tmp_path)
        assert "stubpkg_fresh.specs" in st.fresh
        assert "stubpkg_fresh.specs" not in st.stale
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]


def test_stale_stub_marks_module_stale(tmp_path: Path, monkeypatch) -> None:
    from jaunt.header import format_stub_header
    from jaunt.stub_emitter import (
        build_stub_source,
        format_stub_best_effort,
        generated_content_digest,
        stub_inputs_digest,
        stub_path_for_source,
    )

    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        spec_file, gen_file = _make_fresh_built_module(tmp_path, "stubpkg_stale", emit_stubs=True)
        gen_source = gen_file.read_text(encoding="utf-8")
        # Record a digest for content that no longer matches the generated module,
        # so the stub is present but stale (recorded != current). The drift must be a
        # real code change (not a comment), since the inputs digest is AST-normalized.
        drifted = gen_source + "\n\ndef _drift() -> int:\n    return 0\n"
        header = format_stub_header(
            tool_version="0",
            source_module="stubpkg_stale.specs",
            generated_digest=generated_content_digest(drifted),
            inputs_digest=stub_inputs_digest(spec_file.read_text(encoding="utf-8"), drifted),
        )
        stub = build_stub_source(spec_file.read_text(encoding="utf-8"), gen_source, set(), header)
        stub_path = stub_path_for_source(spec_file)
        stub_path.write_text(format_stub_best_effort(stub, filename=stub_path), encoding="utf-8")

        st = _status(tmp_path)
        assert "stubpkg_stale.specs" in st.stale
        assert st.stale_changes.get("stubpkg_stale.specs") == "stub"
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]


def _emit_matching_stub(spec_file: Path, gen_file: Path, source_module: str) -> None:
    from jaunt.header import format_stub_header
    from jaunt.stub_emitter import (
        build_stub_source,
        format_stub_best_effort,
        generated_content_digest,
        stub_inputs_digest,
        stub_path_for_source,
    )

    gen_source = gen_file.read_text(encoding="utf-8")
    spec_source = spec_file.read_text(encoding="utf-8")
    header = format_stub_header(
        tool_version="0",
        source_module=source_module,
        generated_digest=generated_content_digest(gen_source),
        inputs_digest=stub_inputs_digest(spec_source, gen_source),
    )
    stub_path = stub_path_for_source(spec_file)
    stub_path.write_text(
        format_stub_best_effort(
            build_stub_source(spec_source, gen_source, set(), header), filename=stub_path
        ),
        encoding="utf-8",
    )


def test_comment_only_spec_edit_keeps_module_fresh(tmp_path: Path, monkeypatch) -> None:
    """A comment-only spec edit must NOT restale the stub (finding 4, PR #63): the
    inputs digest is AST-normalized, preserving the Layer-A freshness guarantee."""
    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        spec_file, gen_file = _make_fresh_built_module(tmp_path, "stubpkg_comment", emit_stubs=True)
        _emit_matching_stub(spec_file, gen_file, "stubpkg_comment.specs")
        assert "stubpkg_comment.specs" in _status(tmp_path).fresh

        spec_file.write_text(
            spec_file.read_text(encoding="utf-8") + "\n# a purely cosmetic note\n",
            encoding="utf-8",
        )
        assert "stubpkg_comment.specs" in _status(tmp_path).fresh
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]


def test_handwritten_helper_edit_marks_module_stale(tmp_path: Path, monkeypatch) -> None:
    """A real edit to the spec's handwritten source restales the module (finding 4/6,
    PR #63) — the stub derives from spec source, not only the generated file."""
    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        spec_file, gen_file = _make_fresh_built_module(
            tmp_path,
            "stubpkg_helper",
            emit_stubs=True,
            extra_spec="def _helper() -> int:\n    return 1\n\n\n",
        )
        _emit_matching_stub(spec_file, gen_file, "stubpkg_helper.specs")
        assert "stubpkg_helper.specs" in _status(tmp_path).fresh

        # Change the handwritten helper's body (generated file untouched).
        spec_file.write_text(
            spec_file.read_text(encoding="utf-8").replace("return 1", "return 2"),
            encoding="utf-8",
        )
        assert "stubpkg_helper.specs" in _status(tmp_path).stale
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


def test_fingerprint_only_mismatch_labeled_fingerprint(tmp_path: Path) -> None:
    """Byte-identical specs + changed generation fingerprint → "fingerprint".

    Previously this surfaced as "stale (structural)", which sent adopters
    bisecting spec digests when the actual cause was an environment-dependent
    fingerprint input (mem-mcp-b adoption feedback, finding 21).
    """
    orig_path = list(sys.path)
    before = set(sys.modules.keys())
    try:
        _make_fresh_built_module(tmp_path, "fppkg", emit_stubs=False)
        st = _status(tmp_path)
        assert "fppkg.specs" in st.fresh

        # Change a fingerprint input (the codex model) without touching specs.
        toml = tmp_path / "jaunt.toml"
        toml.write_text(
            toml.read_text(encoding="utf-8") + '\n[codex]\nmodel = "gpt-6-test"\n',
            encoding="utf-8",
        )
        st2 = _status(tmp_path)
        assert "fppkg.specs" in st2.stale
        assert st2.stale_changes.get("fppkg.specs") == "fingerprint"
    finally:
        sys.path[:] = orig_path
        for m in list(sys.modules.keys()):
            if m not in before:
                del sys.modules[m]
