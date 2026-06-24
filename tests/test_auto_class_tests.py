from __future__ import annotations

import hashlib
from pathlib import Path

from jaunt.deps import build_spec_graph
from jaunt.digest import module_digest
from jaunt.header import format_header
from jaunt.module_contract import build_module_contract
from jaunt.module_contract import synthesize_auto_class_test_entries
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref
from jaunt.tester import detect_stale_test_modules


def _magic_class(module: str, name: str, *, test: object) -> SpecEntry:
    kwargs = {} if test is None else {"test": test}
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{name}"),
        module=module,
        qualname=name,
        source_file=f"/src/{module.replace('.', '/')}.py",
        obj=type(name, (), {}),
        decorator_kwargs=kwargs,
        class_name=None,
    )


def test_opt_in_via_kwarg() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=True)]}
    out = synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    )
    assert len(out) == 1
    entries = next(iter(out.values()))
    assert entries[0].kind == "test"
    assert entries[0].decorator_kwargs["public_api_only"] is True
    targets = {str(t) for t in entries[0].decorator_kwargs["targets"]}
    assert "pkg.mod:Cart" in targets


def test_default_on_applies_when_kwarg_absent() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=None)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=False, tests_package="tests", generated_dir="__generated__"
    ) == {}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) != {}


def test_kwarg_false_overrides_default_on() -> None:
    specs = {e.spec_ref: e for e in [_magic_class("pkg.mod", "Cart", test=False)]}
    assert synthesize_auto_class_test_entries(
        specs, default_on=True, tests_package="tests", generated_dir="__generated__"
    ) == {}


def test_target_api_digest_change_marks_stale(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    tests_dir = project / "tests"
    tests_dir.mkdir(parents=True)
    spec_path = tests_dir / "specs_mod.py"
    spec_path.write_text(
        """
def test_generated():
    raise AssertionError("generated at test time")
""".lstrip(),
        encoding="utf-8",
    )

    entry = SpecEntry(
        kind="test",
        spec_ref=normalize_spec_ref("tests.specs_mod:test_generated"),
        module="tests.specs_mod",
        qualname="test_generated",
        source_file=str(spec_path),
        obj=object(),
        decorator_kwargs={},
    )
    specs = {entry.spec_ref: entry}
    spec_graph = build_spec_graph(specs, infer_default=False)
    module_specs = {"tests.specs_mod": [entry]}
    expected_names = ["test_generated"]
    base_context_digest = build_module_contract(
        entries=[entry],
        expected_names=expected_names,
    ).digest
    target_api_digest = "target-api-v1"
    combined_context_digest = hashlib.sha256(
        f"{base_context_digest}\n{target_api_digest}".encode()
    ).hexdigest()

    generated_test_path = tests_dir / "__generated__" / "specs_mod.py"
    generated_test_path.parent.mkdir(parents=True)
    generated_test_path.write_text(
        format_header(
            tool_version="0",
            kind="test",
            source_module="tests.specs_mod",
            module_digest=module_digest("tests.specs_mod", [entry], specs, spec_graph),
            generation_fingerprint="test-fingerprint",
            module_context_digest=combined_context_digest,
            spec_refs=[str(entry.spec_ref)],
        )
        + "\ndef test_generated():\n    assert True\n",
        encoding="utf-8",
    )

    assert (
        detect_stale_test_modules(
            project_dir=project,
            generated_dir="__generated__",
            test_roots=[tests_dir],
            module_specs=module_specs,
            specs=specs,
            spec_graph=spec_graph,
            generation_fingerprint="test-fingerprint",
            module_context_digests={"tests.specs_mod": base_context_digest},
            target_api_digests={"tests.specs_mod": target_api_digest},
        )
        == set()
    )

    assert detect_stale_test_modules(
        project_dir=project,
        generated_dir="__generated__",
        test_roots=[tests_dir],
        module_specs=module_specs,
        specs=specs,
        spec_graph=spec_graph,
        generation_fingerprint="test-fingerprint",
        module_context_digests={"tests.specs_mod": base_context_digest},
        target_api_digests={"tests.specs_mod": "target-api-v2"},
    ) == {"tests.specs_mod"}
