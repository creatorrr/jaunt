from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from jaunt.builder import (
    _strip_header,
    detect_stale_modules,
    plan_refreeze_or_rebuild,
    refreeze_module,
    write_generated_module,
)
from jaunt.change_detection import read_contract_sidecar, sidecar_path
from jaunt.config import SemanticGateConfig
from jaunt.deps import build_spec_graph
from jaunt.digest import contract_snapshot, module_digest, prose_digest, structural_digest
from jaunt.header import extract_digest_scheme, extract_module_digest, extract_spec_digests
from jaunt.paths import generated_module_to_relpath, spec_module_to_generated_module
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _entry(*, module: str, qualname: str, source_file: str, decorator_kwargs=None) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
    )


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _header(module: str, digest: str, spec_refs: list[str]) -> dict[str, object]:
    return {
        "tool_version": "0",
        "kind": "build",
        "source_module": module,
        "module_digest": digest,
        "spec_refs": spec_refs,
    }


def _spec_digests(entry: SpecEntry) -> dict[str, dict[str, str]]:
    return {str(entry.spec_ref): {"s": structural_digest(entry), "p": prose_digest(entry)}}


def _snapshots(entry: SpecEntry) -> dict[str, dict]:
    return {str(entry.spec_ref): contract_snapshot(entry)}


def test_refreeze_keeps_body_byte_identical_and_updates_header(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Foo():\n    return 1\n")
    e = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    specs = {e.spec_ref: e}
    build_spec_graph(specs, infer_default=False)
    seed_spec_digests = _spec_digests(e)
    seed_snapshots = _snapshots(e)

    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return 1\n",
        header_fields=_header("m", "a" * 64, [str(e.spec_ref)]),
        spec_digests=seed_spec_digests,
        snapshots=seed_snapshots,
    )
    seeded_text = out_path.read_text(encoding="utf-8")
    captured_body = _strip_header(seeded_text)
    new_spec_digests = _spec_digests(e)
    new_snapshots = _snapshots(e)
    refreeze_header = _header("m", "b" * 64, [str(e.spec_ref)])
    refreeze_header["spec_digests"] = new_spec_digests

    outcome = refreeze_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        header_fields=refreeze_header,
        snapshots=new_snapshots,
    )

    assert outcome.refrozen is True
    assert outcome.needs_rebuild is False
    new_text = out_path.read_text(encoding="utf-8")
    assert _strip_header(new_text) == captured_body
    assert extract_module_digest(new_text) == "sha256:" + "b" * 64
    assert extract_digest_scheme(new_text) == 2
    assert extract_spec_digests(new_text) == new_spec_digests
    assert read_contract_sidecar(sidecar_path(out_path)) == new_snapshots


def test_refreeze_updates_sidecar_to_new_snapshots(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, 'def Foo():\n    """Old prose."""\n    return 1\n')
    e_v1 = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return 1\n",
        header_fields=_header("m", "a" * 64, [str(e_v1.spec_ref)]),
        spec_digests=_spec_digests(e_v1),
        snapshots=_snapshots(e_v1),
    )
    _write(spec_path, 'def Foo():\n    """New prose, same behavior."""\n    return 1\n')
    e_v2 = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    snapshots = _snapshots(e_v2)
    header_fields = _header("m", "b" * 64, [str(e_v2.spec_ref)])
    header_fields["spec_digests"] = _spec_digests(e_v2)

    outcome = refreeze_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        header_fields=header_fields,
        snapshots=snapshots,
    )

    assert outcome.refrozen is True
    assert read_contract_sidecar(sidecar_path(out_path))[str(e_v2.spec_ref)]["prose"] == (
        "New prose, same behavior."
    )


def test_removed_spec_forces_rebuild_instead_of_restamping_dead_body(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Foo():\n    return 1\n\ndef Bar():\n    return Foo()\n")
    foo = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    bar = _entry(module="m", qualname="Bar", source_file=str(spec_path))
    old_spec_digests = {**_spec_digests(foo), **_spec_digests(bar)}
    old_snapshots = {**_snapshots(foo), **_snapshots(bar)}
    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return 1\n\ndef Bar():\n    return Foo()\n",
        header_fields=_header("m", "a" * 64, [str(foo.spec_ref), str(bar.spec_ref)]),
        spec_digests=old_spec_digests,
        snapshots=old_snapshots,
    )
    before = out_path.read_text(encoding="utf-8")

    _write(spec_path, "def Bar():\n    return 2\n")
    current_bar = _entry(module="m", qualname="Bar", source_file=str(spec_path))
    specs = {current_bar.spec_ref: current_bar}

    async def unexpected_gate(**kwargs: object) -> SimpleNamespace:
        raise AssertionError(f"removed specs must not reach the semantic gate: {kwargs}")

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"m": [current_bar]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"m": set()},
            stale_modules={"m"},
            header_fields_by_module={"m": _header("m", "b" * 64, [str(current_bar.spec_ref)])},
            cfg=SemanticGateConfig(),
            run_exec=unexpected_gate,
        )
    )

    assert plan.rebuild == {"m"}
    assert plan.refrozen == set()
    assert out_path.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("artifact_version", ["1.0.0rc7", "1.0.0", "1.6.2"])
def test_vulnerable_restamp_version_forces_one_time_rebuild(
    tmp_path: Path, monkeypatch, artifact_version: str
) -> None:
    monkeypatch.setattr("jaunt.builder._tool_version", lambda: "1.6.3")
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Bar():\n    return 2\n")
    bar = _entry(module="m", qualname="Bar", source_file=str(spec_path))
    specs = {bar.spec_ref: bar}
    graph = build_spec_graph(specs, infer_default=False)
    digest = module_digest("m", [bar], specs, graph)
    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Removed():\n    return 1\n\ndef Bar():\n    return 2\n",
        header_fields={
            **_header("m", digest, [str(bar.spec_ref)]),
            "tool_version": artifact_version,
        },
        spec_digests=_spec_digests(bar),
        snapshots=_snapshots(bar),
    )

    stale = detect_stale_modules(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_specs={"m": [bar]},
        specs=specs,
        spec_graph=graph,
    )
    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"m": [bar]},
            specs=specs,
            spec_graph=graph,
            module_dag={"m": set()},
            stale_modules=stale,
            header_fields_by_module={"m": _header("m", digest, [str(bar.spec_ref)])},
            cfg=SemanticGateConfig(),
        )
    )

    assert stale == {"m"}
    assert plan.rebuild == {"m"}
    assert plan.refrozen == set()
    assert "def Removed():" in out_path.read_text(encoding="utf-8")


def test_removed_spec_rebuild_expands_before_dependents_can_restamp(tmp_path: Path) -> None:
    old_a_path = tmp_path / "a_old.py"
    current_a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    c_path = tmp_path / "c.py"
    _write(old_a_path, "def Removed():\n    return 1\n\ndef A():\n    return 2\n")
    _write(current_a_path, "def A():\n    return 2\n")
    _write(b_path, "def B():\n    return 3\n")
    _write(c_path, "def C():\n    return 4\n")

    removed = _entry(module="a", qualname="Removed", source_file=str(old_a_path))
    old_a = _entry(module="a", qualname="A", source_file=str(old_a_path))
    current_a = _entry(module="a", qualname="A", source_file=str(current_a_path))
    b = _entry(module="b", qualname="B", source_file=str(b_path))
    c = _entry(module="c", qualname="C", source_file=str(c_path))

    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="a",
        source="def Removed():\n    return 1\n\ndef A():\n    return 2\n",
        header_fields=_header("a", "a" * 64, [str(removed.spec_ref), str(old_a.spec_ref)]),
        spec_digests={**_spec_digests(removed), **_spec_digests(old_a)},
        snapshots={**_snapshots(removed), **_snapshots(old_a)},
    )
    for module, entry, digest in (("b", b, "b" * 64), ("c", c, "c" * 64)):
        write_generated_module(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_name=module,
            source=f"def {entry.qualname}():\n    return 1\n",
            header_fields=_header(module, digest, [str(entry.spec_ref)]),
            spec_digests=_spec_digests(entry),
            snapshots=_snapshots(entry),
        )

    specs = {entry.spec_ref: entry for entry in (current_a, b, c)}
    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"a": [current_a], "b": [b], "c": [c]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"a": set(), "b": {"a"}, "c": {"b"}},
            stale_modules={"a", "b", "c"},
            header_fields_by_module={
                "a": _header("a", "d" * 64, [str(current_a.spec_ref)]),
                "b": _header("b", "b" * 64, [str(b.spec_ref)]),
                "c": _header("c", "c" * 64, [str(c.spec_ref)]),
            },
            cfg=SemanticGateConfig(),
        )
    )

    assert plan.rebuild == {"a", "b", "c"}
    assert plan.refrozen == set()


def test_changed_handwritten_context_forces_rebuild_when_specs_unchanged(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Foo():\n    return 1\n")
    entry = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return 1\n",
        header_fields={
            **_header("m", "a" * 64, [str(entry.spec_ref)]),
            "generation_fingerprint": "same-fingerprint",
            "module_context_digest": "old-context",
        },
        spec_digests=_spec_digests(entry),
        snapshots=_snapshots(entry),
    )
    before = out_path.read_text(encoding="utf-8")
    specs = {entry.spec_ref: entry}

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"m": [entry]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"m": set()},
            stale_modules={"m"},
            header_fields_by_module={
                "m": {
                    **_header("m", "a" * 64, [str(entry.spec_ref)]),
                    "generation_fingerprint": "same-fingerprint",
                    "module_context_digest": "new-context",
                }
            },
            cfg=SemanticGateConfig(),
        )
    )

    assert plan.rebuild == {"m"}
    assert plan.refrozen == set()
    assert out_path.read_text(encoding="utf-8") == before


def test_context_only_rebuild_does_not_promote_independently_stale_dependent(
    tmp_path: Path,
) -> None:
    a_path = tmp_path / "a.py"
    b_path = tmp_path / "b.py"
    _write(a_path, "def A():\n    return 1\n")
    _write(b_path, "def B():\n    return 2\n")
    a = _entry(module="a", qualname="A", source_file=str(a_path))
    b = _entry(module="b", qualname="B", source_file=str(b_path))
    for module, entry, context in (("a", a, "old-context"), ("b", b, "same-context")):
        write_generated_module(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_name=module,
            source=f"def {entry.qualname}():\n    return 1\n",
            header_fields={
                **_header(module, module * 64, [str(entry.spec_ref)]),
                "generation_fingerprint": "old-fingerprint",
                "module_context_digest": context,
            },
            spec_digests=_spec_digests(entry),
            snapshots=_snapshots(entry),
        )
    specs = {entry.spec_ref: entry for entry in (a, b)}

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"a": [a], "b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"a": set(), "b": {"a"}},
            stale_modules={"a", "b"},
            header_fields_by_module={
                "a": {
                    **_header("a", "a" * 64, [str(a.spec_ref)]),
                    "generation_fingerprint": "old-fingerprint",
                    "module_context_digest": "new-context",
                },
                "b": {
                    **_header("b", "b" * 64, [str(b.spec_ref)]),
                    "generation_fingerprint": "new-fingerprint",
                    "module_context_digest": "same-context",
                },
            },
            cfg=SemanticGateConfig(),
        )
    )

    assert plan.rebuild == {"a"}
    assert plan.refrozen == {"b"}


def test_fingerprint_change_does_not_mask_context_change(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Foo():\n    return 1\n")
    entry = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return old_helper()\n",
        header_fields={
            **_header("m", "a" * 64, [str(entry.spec_ref)]),
            "generation_fingerprint": "old-fingerprint",
            "module_context_digest": "old-context",
        },
        spec_digests=_spec_digests(entry),
        snapshots=_snapshots(entry),
    )
    specs = {entry.spec_ref: entry}

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"m": [entry]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"m": set()},
            stale_modules={"m"},
            header_fields_by_module={
                "m": {
                    **_header("m", "a" * 64, [str(entry.spec_ref)]),
                    "generation_fingerprint": "new-fingerprint",
                    "module_context_digest": "new-context",
                }
            },
            cfg=SemanticGateConfig(),
        )
    )

    assert plan.rebuild == {"m"}
    assert plan.refrozen == set()


def test_refreeze_validation_failure_forces_rebuild(tmp_path: Path) -> None:
    spec_path = tmp_path / "m.py"
    _write(spec_path, "def Foo():\n    return 1\n")
    e = _entry(module="m", qualname="Foo", source_file=str(spec_path))
    out_path = write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        source="def Foo():\n    return 1\n",
        header_fields=_header("m", "a" * 64, [str(e.spec_ref)]),
        spec_digests=_spec_digests(e),
        snapshots=_snapshots(e),
    )
    original_text = out_path.read_text(encoding="utf-8")
    header_fields = _header("m", "b" * 64, [str(e.spec_ref)])
    header_fields["spec_digests"] = _spec_digests(e)

    outcome = refreeze_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="m",
        header_fields=header_fields,
        snapshots=_snapshots(e),
        validate_body=lambda body: ["body failed validation"],
    )

    assert outcome.refrozen is False
    assert outcome.needs_rebuild is True
    assert "body failed validation" in outcome.errors
    assert out_path.read_text(encoding="utf-8") == original_text


def test_refreeze_missing_header_forces_rebuild(tmp_path: Path) -> None:
    module_name = "m"
    generated_module = spec_module_to_generated_module(module_name, generated_dir="__generated__")
    relpath = generated_module_to_relpath(generated_module, generated_dir="__generated__")
    out_path = tmp_path / relpath
    _write(out_path, "def Foo():\n    return 1\n")

    outcome = refreeze_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name=module_name,
        header_fields=_header(module_name, "b" * 64, ["m:Foo"]),
        snapshots={},
    )

    assert outcome.needs_rebuild is True
    assert outcome.refrozen is False


def test_rollup_dependent_refreezes_when_dependency_equivalent(tmp_path: Path) -> None:
    spec_a_path = tmp_path / "mod_a.py"
    spec_b_path = tmp_path / "mod_b.py"
    _write(spec_a_path, 'def A():\n    """Old A docstring."""\n    return 1\n')
    _write(spec_b_path, 'def B():\n    """B docstring."""\n    return 2\n')
    a_v1 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    b = _entry(module="mod_b", qualname="B", source_file=str(spec_b_path))
    specs_v1 = {a_v1.spec_ref: a_v1, b.spec_ref: b}
    build_spec_graph(specs_v1, infer_default=False)
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_a",
        source="def A():\n    return 1\n",
        header_fields=_header("mod_a", "a" * 64, [str(a_v1.spec_ref)]),
        spec_digests=_spec_digests(a_v1),
        snapshots=_snapshots(a_v1),
    )
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_b",
        source="def B():\n    return 2\n",
        header_fields=_header("mod_b", "b" * 64, [str(b.spec_ref)]),
        spec_digests=_spec_digests(b),
        snapshots=_snapshots(b),
    )
    _write(spec_a_path, 'def A():\n    """New A docstring, same behavior."""\n    return 1\n')
    a_v2 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    specs = {a_v2.spec_ref: a_v2, b.spec_ref: b}

    async def mock_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="EQUIVALENT")

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"mod_a": [a_v2], "mod_b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"mod_b": {"mod_a"}, "mod_a": set()},
            stale_modules={"mod_a", "mod_b"},
            header_fields_by_module={
                "mod_a": _header("mod_a", "c" * 64, [str(a_v2.spec_ref)]),
                "mod_b": _header("mod_b", "d" * 64, [str(b.spec_ref)]),
            },
            cfg=SemanticGateConfig(),
            run_exec=mock_run_exec,
        )
    )

    assert plan.rebuild == set()
    assert plan.refrozen == {"mod_a", "mod_b"}
    assert plan.failed_refreeze == set()


def test_rollup_dependent_rebuilds_when_dependency_meaningful(tmp_path: Path) -> None:
    spec_a_path = tmp_path / "mod_a.py"
    spec_b_path = tmp_path / "mod_b.py"
    _write(spec_a_path, 'def A():\n    """Old A docstring."""\n    return 1\n')
    _write(spec_b_path, 'def B():\n    """B docstring."""\n    return 2\n')
    a_v1 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    b = _entry(module="mod_b", qualname="B", source_file=str(spec_b_path))
    specs_v1 = {a_v1.spec_ref: a_v1, b.spec_ref: b}
    build_spec_graph(specs_v1, infer_default=False)
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_a",
        source="def A():\n    return 1\n",
        header_fields=_header("mod_a", "a" * 64, [str(a_v1.spec_ref)]),
        spec_digests=_spec_digests(a_v1),
        snapshots=_snapshots(a_v1),
    )
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_b",
        source="def B():\n    return 2\n",
        header_fields=_header("mod_b", "b" * 64, [str(b.spec_ref)]),
        spec_digests=_spec_digests(b),
        snapshots=_snapshots(b),
    )
    _write(spec_a_path, 'def A():\n    """New A docstring, same behavior."""\n    return 1\n')
    a_v2 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    specs = {a_v2.spec_ref: a_v2, b.spec_ref: b}

    async def mock_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="MEANINGFUL")

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"mod_a": [a_v2], "mod_b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"mod_b": {"mod_a"}, "mod_a": set()},
            stale_modules={"mod_a", "mod_b"},
            header_fields_by_module={
                "mod_a": _header("mod_a", "c" * 64, [str(a_v2.spec_ref)]),
                "mod_b": _header("mod_b", "d" * 64, [str(b.spec_ref)]),
            },
            cfg=SemanticGateConfig(),
            run_exec=mock_run_exec,
        )
    )

    assert plan.rebuild == {"mod_a", "mod_b"}
    assert plan.refrozen == set()


def test_refreeze_refused_when_base_api_moved(tmp_path: Path) -> None:
    # mod_b subclasses mod_a. A docstring-only edit to mod_a would normally let
    # the semantic gate re-freeze both modules (EQUIVALENT). But when mod_b's
    # spec'd-base generated API digest moved, refreeze must be refused for mod_b
    # and it must rebuild instead. mod_a (whose base API did not move) still
    # re-freezes, proving the guard is selective.
    spec_a_path = tmp_path / "mod_a.py"
    spec_b_path = tmp_path / "mod_b.py"
    _write(spec_a_path, 'def A():\n    """Old A docstring."""\n    return 1\n')
    _write(spec_b_path, 'def B():\n    """B docstring."""\n    return 2\n')
    a_v1 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    b = _entry(module="mod_b", qualname="B", source_file=str(spec_b_path))
    specs_v1 = {a_v1.spec_ref: a_v1, b.spec_ref: b}
    build_spec_graph(specs_v1, infer_default=False)
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_a",
        source="def A():\n    return 1\n",
        header_fields=_header("mod_a", "a" * 64, [str(a_v1.spec_ref)]),
        spec_digests=_spec_digests(a_v1),
        snapshots=_snapshots(a_v1),
    )
    write_generated_module(
        package_dir=tmp_path,
        generated_dir="__generated__",
        module_name="mod_b",
        source="def B():\n    return 2\n",
        header_fields=_header("mod_b", "b" * 64, [str(b.spec_ref)]),
        spec_digests=_spec_digests(b),
        snapshots=_snapshots(b),
    )
    _write(spec_a_path, 'def A():\n    """New A docstring, same behavior."""\n    return 1\n')
    a_v2 = _entry(module="mod_a", qualname="A", source_file=str(spec_a_path))
    specs = {a_v2.spec_ref: a_v2, b.spec_ref: b}

    async def mock_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="EQUIVALENT")

    plan = asyncio.run(
        plan_refreeze_or_rebuild(
            package_dir=tmp_path,
            generated_dir="__generated__",
            module_specs={"mod_a": [a_v2], "mod_b": [b]},
            specs=specs,
            spec_graph=build_spec_graph(specs, infer_default=False),
            module_dag={"mod_b": {"mod_a"}, "mod_a": set()},
            stale_modules={"mod_a", "mod_b"},
            header_fields_by_module={
                "mod_a": _header("mod_a", "c" * 64, [str(a_v2.spec_ref)]),
                "mod_b": _header("mod_b", "d" * 64, [str(b.spec_ref)]),
            },
            cfg=SemanticGateConfig(),
            base_api_changed={"mod_b"},
            run_exec=mock_run_exec,
        )
    )

    assert "mod_b" in plan.rebuild
    assert "mod_b" not in plan.refrozen
    assert "mod_a" in plan.refrozen
    assert "mod_a" not in plan.rebuild
