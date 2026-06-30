from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jaunt.builder import (
    _strip_header,
    plan_refreeze_or_rebuild,
    refreeze_module,
    write_generated_module,
)
from jaunt.change_detection import read_contract_sidecar, sidecar_path
from jaunt.config import SemanticGateConfig
from jaunt.deps import build_spec_graph
from jaunt.digest import contract_snapshot, prose_digest, structural_digest
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
