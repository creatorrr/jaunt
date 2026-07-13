from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.builder import _compute_snapshots, _compute_spec_digests
from jaunt.config import SemanticGateConfig
from jaunt.deps import build_spec_graph
from jaunt.header import extract_digest_scheme
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref
from jaunt.tester import (
    _test_module_digest,
    _write_generated_test_module,
    detect_stale_test_modules,
    plan_test_refreeze_or_rebuild,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _entry(*, module: str, qualname: str, source_file: Path) -> SpecEntry:
    return SpecEntry(
        kind="test",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=str(source_file),
        obj=object(),
        decorator_kwargs={},
    )


def _header(module: str, entries: list[SpecEntry], digest: str) -> dict[str, object]:
    return {
        "tool_version": "0",
        "kind": "test",
        "source_module": module,
        "module_digest": digest,
        "generation_fingerprint": "same-fingerprint",
        "module_context_digest": "same-context",
        "spec_refs": [str(entry.spec_ref) for entry in entries],
    }


def test_removed_test_spec_rebuilds_module_and_dependents_before_restamp(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    a_source = tests_root / "a_specs.py"
    b_source = tests_root / "b_specs.py"
    _write(
        a_source,
        "def test_removed():\n    ...\n\ndef test_kept():\n    ...\n",
    )
    _write(b_source, "def test_dependent():\n    ...\n")

    removed = _entry(module="tests.a_specs", qualname="test_removed", source_file=a_source)
    old_kept = _entry(module="tests.a_specs", qualname="test_kept", source_file=a_source)
    dependent = _entry(module="tests.b_specs", qualname="test_dependent", source_file=b_source)
    old_a_entries = [removed, old_kept]

    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=tests_root / "__generated__" / "a_specs.py",
        generated_dir="__generated__",
        source=("def test_removed():\n    assert True\n\ndef test_kept():\n    assert True\n"),
        header_fields=_header("tests.a_specs", old_a_entries, "a" * 64),
        spec_digests=_compute_spec_digests(old_a_entries),
        snapshots=_compute_snapshots(old_a_entries),
    )
    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=tests_root / "__generated__" / "b_specs.py",
        generated_dir="__generated__",
        source="def test_dependent():\n    assert True\n",
        header_fields=_header("tests.b_specs", [dependent], "b" * 64),
        spec_digests=_compute_spec_digests([dependent]),
        snapshots=_compute_snapshots([dependent]),
    )

    _write(a_source, "def test_kept():\n    ...\n")
    current_kept = _entry(module="tests.a_specs", qualname="test_kept", source_file=a_source)
    specs = {entry.spec_ref: entry for entry in (current_kept, dependent)}

    plan = asyncio.run(
        plan_test_refreeze_or_rebuild(
            project_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={
                "tests.a_specs": [current_kept],
                "tests.b_specs": [dependent],
            },
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"tests.a_specs": set(), "tests.b_specs": {"tests.a_specs"}},
            stale_modules={"tests.a_specs", "tests.b_specs"},
            header_fields_by_module={
                "tests.a_specs": _header("tests.a_specs", [current_kept], "c" * 64),
                "tests.b_specs": _header("tests.b_specs", [dependent], "b" * 64),
            },
            cfg=SemanticGateConfig(),
            tests_package="tests",
            test_roots=[tests_root],
        )
    )

    assert plan.rebuild == {"tests.a_specs", "tests.b_specs"}
    assert plan.refrozen == set()
    generated_a = tests_root / "__generated__" / "a_specs.py"
    assert "def test_removed():" in generated_a.read_text(encoding="utf-8")


def test_vulnerable_test_restamp_version_forces_one_time_rebuild(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr("jaunt.builder._tool_version", lambda: "1.6.3")
    tests_root = tmp_path / "tests"
    source = tests_root / "specs.py"
    _write(source, "def test_kept():\n    ...\n")
    kept = _entry(module="tests.specs", qualname="test_kept", source_file=source)
    specs = {kept.spec_ref: kept}
    graph = build_spec_graph(specs, infer_default=False)
    digest = _test_module_digest("tests.specs", [kept], specs, graph)
    out_path = tests_root / "__generated__" / "specs.py"
    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=out_path,
        generated_dir="__generated__",
        source="def test_removed():\n    assert True\n\ndef test_kept():\n    assert True\n",
        header_fields={
            **_header("tests.specs", [kept], digest),
            "tool_version": "1.6.2",
        },
        spec_digests=_compute_spec_digests([kept]),
        snapshots=_compute_snapshots([kept]),
    )

    stale = detect_stale_test_modules(
        project_dir=tmp_path,
        generated_dir="__generated__",
        module_specs={"tests.specs": [kept]},
        specs=specs,
        spec_graph=graph,
        tests_package="tests",
        test_roots=[tests_root],
    )
    plan = asyncio.run(
        plan_test_refreeze_or_rebuild(
            project_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"tests.specs": [kept]},
            specs=specs,
            spec_graph=graph,
            module_dag={"tests.specs": set()},
            stale_modules=stale,
            header_fields_by_module={"tests.specs": _header("tests.specs", [kept], digest)},
            cfg=SemanticGateConfig(),
            tests_package="tests",
            test_roots=[tests_root],
        )
    )

    assert stale == {"tests.specs"}
    assert plan.rebuild == {"tests.specs"}
    assert plan.refrozen == set()
    assert "def test_removed():" in out_path.read_text(encoding="utf-8")


def test_test_context_only_rebuild_does_not_promote_stale_dependent(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    a_source = tests_root / "a.py"
    b_source = tests_root / "b.py"
    _write(a_source, "def test_a():\n    ...\n")
    _write(b_source, "def test_b():\n    ...\n")
    a = _entry(module="tests.a", qualname="test_a", source_file=a_source)
    b = _entry(module="tests.b", qualname="test_b", source_file=b_source)
    for module, entry, context in (
        ("tests.a", a, "old-context"),
        ("tests.b", b, "same-context"),
    ):
        _write_generated_test_module(
            project_dir=tmp_path,
            out_path=tests_root / "__generated__" / f"{module.rsplit('.', 1)[-1]}.py",
            generated_dir="__generated__",
            source=f"def {entry.qualname}():\n    assert True\n",
            header_fields={
                **_header(module, [entry], module[-1] * 64),
                "generation_fingerprint": "old-fingerprint",
                "module_context_digest": context,
            },
            spec_digests=_compute_spec_digests([entry]),
            snapshots=_compute_snapshots([entry]),
        )
    specs = {entry.spec_ref: entry for entry in (a, b)}

    plan = asyncio.run(
        plan_test_refreeze_or_rebuild(
            project_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"tests.a": [a], "tests.b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"tests.a": set(), "tests.b": {"tests.a"}},
            stale_modules={"tests.a", "tests.b"},
            header_fields_by_module={
                "tests.a": {
                    **_header("tests.a", [a], "a" * 64),
                    "generation_fingerprint": "old-fingerprint",
                    "module_context_digest": "new-context",
                },
                "tests.b": {
                    **_header("tests.b", [b], "b" * 64),
                    "generation_fingerprint": "new-fingerprint",
                    "module_context_digest": "same-context",
                },
            },
            cfg=SemanticGateConfig(),
            tests_package="tests",
            test_roots=[tests_root],
        )
    )

    assert plan.rebuild == {"tests.a"}
    assert plan.refrozen == {"tests.b"}


def test_legacy_test_restamp_is_deferred_until_after_rebuild_expansion(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    a_source = tests_root / "a.py"
    b_source = tests_root / "b.py"
    _write(a_source, "def test_removed():\n    ...\n\ndef test_kept():\n    ...\n")
    _write(b_source, "def test_b():\n    ...\n")
    removed = _entry(module="tests.a", qualname="test_removed", source_file=a_source)
    old_kept = _entry(module="tests.a", qualname="test_kept", source_file=a_source)
    b = _entry(module="tests.b", qualname="test_b", source_file=b_source)
    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=tests_root / "__generated__" / "a.py",
        generated_dir="__generated__",
        source="def test_removed():\n    assert True\n\ndef test_kept():\n    assert True\n",
        header_fields=_header("tests.a", [removed, old_kept], "old-a"),
        spec_digests=_compute_spec_digests([removed, old_kept]),
        snapshots=_compute_snapshots([removed, old_kept]),
    )
    b_path = tests_root / "__generated__" / "b.py"
    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=b_path,
        generated_dir="__generated__",
        source="def test_b():\n    assert True\n",
        header_fields=_header("tests.b", [b], "legacy-b"),
        spec_digests=None,
        snapshots=_compute_snapshots([b]),
    )
    before = b_path.read_text(encoding="utf-8")
    assert extract_digest_scheme(before) is None

    _write(a_source, "def test_kept():\n    ...\n")
    kept = _entry(module="tests.a", qualname="test_kept", source_file=a_source)
    specs = {entry.spec_ref: entry for entry in (kept, b)}
    plan = asyncio.run(
        plan_test_refreeze_or_rebuild(
            project_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"tests.a": [kept], "tests.b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"tests.a": set(), "tests.b": {"tests.a"}},
            stale_modules={"tests.a", "tests.b"},
            header_fields_by_module={
                "tests.a": _header("tests.a", [kept], "new-a"),
                "tests.b": {
                    **_header("tests.b", [b], "new-b"),
                    "legacy_module_digest": "legacy-b",
                },
            },
            cfg=SemanticGateConfig(),
            tests_package="tests",
            test_roots=[tests_root],
        )
    )

    assert plan.rebuild == {"tests.a", "tests.b"}
    assert plan.refrozen == set()
    assert b_path.read_text(encoding="utf-8") == before


def test_test_fingerprint_change_does_not_mask_context_change(tmp_path: Path) -> None:
    tests_root = tmp_path / "tests"
    source = tests_root / "specs.py"
    _write(source, "def test_kept():\n    ...\n")
    kept = _entry(module="tests.specs", qualname="test_kept", source_file=source)
    _write_generated_test_module(
        project_dir=tmp_path,
        out_path=tests_root / "__generated__" / "specs.py",
        generated_dir="__generated__",
        source="def test_kept():\n    assert old_helper()\n",
        header_fields={
            **_header("tests.specs", [kept], "old"),
            "generation_fingerprint": "old-fingerprint",
            "module_context_digest": "old-context",
        },
        spec_digests=_compute_spec_digests([kept]),
        snapshots=_compute_snapshots([kept]),
    )
    specs = {kept.spec_ref: kept}

    plan = asyncio.run(
        plan_test_refreeze_or_rebuild(
            project_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"tests.specs": [kept]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"tests.specs": set()},
            stale_modules={"tests.specs"},
            header_fields_by_module={
                "tests.specs": {
                    **_header("tests.specs", [kept], "new"),
                    "generation_fingerprint": "new-fingerprint",
                    "module_context_digest": "new-context",
                }
            },
            cfg=SemanticGateConfig(),
            tests_package="tests",
            test_roots=[tests_root],
        )
    )

    assert plan.rebuild == {"tests.specs"}
    assert plan.refrozen == set()
