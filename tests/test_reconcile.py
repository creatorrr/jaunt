"""Tests for the reconciliation core: orphan + newly-governed detection."""

from __future__ import annotations

from pathlib import Path

from jaunt.header import (
    format_contract_battery_header,
    format_header,
    format_stub_header,
)
from jaunt.reconcile import (
    OrphanArtifact,
    find_orphans,
    newly_governed_specs,
)
from jaunt.registry import SpecEntry
from jaunt.spec_ref import SpecRef


def _write_generated(
    package_dir: Path, generated_dir: str, module: str, source_module: str
) -> Path:
    header = format_header(
        tool_version="1",
        kind="build",
        source_module=source_module,
        module_digest="deadbeef",
        spec_refs=[f"{source_module}:f"],
    )
    gen_dir = package_dir / generated_dir
    gen_dir.mkdir(parents=True, exist_ok=True)
    path = gen_dir / f"{module}.py"
    path.write_text(header + "\n\ndef f():\n    return 1\n", encoding="utf-8")
    return path


def _write_sidecar(generated_path: Path) -> Path:
    sidecar = generated_path.with_name(generated_path.name + ".contract.json")
    sidecar.write_text("{}\n", encoding="utf-8")
    return sidecar


def _write_stub(source_dir: Path, name: str, source_module: str) -> Path:
    header = format_stub_header(
        tool_version="1",
        source_module=source_module,
        generated_digest="cafe",
        inputs_digest="babe",
    )
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.pyi"
    path.write_text(header + "\n\ndef f() -> int: ...\n", encoding="utf-8")
    return path


def _write_handwritten_stub(source_dir: Path, name: str) -> Path:
    source_dir.mkdir(parents=True, exist_ok=True)
    path = source_dir / f"{name}.pyi"
    path.write_text("# hand-written stub\ndef f() -> int: ...\n", encoding="utf-8")
    return path


def _write_battery(battery_dir: Path, name: str, derived_from: str) -> Path:
    header = format_contract_battery_header(
        derived_from=derived_from,
        prose_digest="aa",
        signature="() -> None",
        body_digest="bb",
        strength="0",
        tool_version="1",
    )
    battery_dir.mkdir(parents=True, exist_ok=True)
    path = battery_dir / f"{name}.py"
    path.write_text(header + "\n\ndef test_x():\n    assert True\n", encoding="utf-8")
    return path


def _entry(module: str, symbol: str, origin: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=SpecRef(f"{module}:{symbol}"),
        module=module,
        qualname=symbol,
        source_file=f"{module}.py",
        obj=None,
        decorator_kwargs={},
        origin=origin,  # type: ignore[arg-type]
    )


def test_generated_module_orphaned_when_spec_gone(tmp_path: Path):
    pkg = tmp_path / "pkg"
    gen = _write_generated(pkg, "__generated__", module="deleted", source_module="deleted")
    sidecar = _write_sidecar(gen)

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules={"kept"},
        source_dirs=[pkg],
        battery_dir=None,
        contract_refs=set(),
    )
    kinds = {(o.kind, o.source_module) for o in orphans}
    assert ("generated", "deleted") in kinds
    assert ("sidecar", "deleted") in kinds
    paths = {o.path for o in orphans}
    assert gen in paths
    assert sidecar in paths


def test_generated_module_kept_when_spec_exists(tmp_path: Path):
    pkg = tmp_path / "pkg"
    _write_generated(pkg, "__generated__", module="kept", source_module="kept")

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules={"kept"},
        source_dirs=[pkg],
        battery_dir=None,
        contract_refs=set(),
    )
    assert orphans == []


def test_stub_orphaned_via_parse_stub_header(tmp_path: Path):
    pkg = tmp_path / "pkg"
    src = tmp_path / "src"
    stub = _write_stub(src, name="gone", source_module="gone")

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules={"kept"},
        source_dirs=[src],
        battery_dir=None,
        contract_refs=set(),
    )
    assert [(o.kind, o.path) for o in orphans] == [("stub", stub)]


def test_handwritten_pyi_never_classified(tmp_path: Path):
    pkg = tmp_path / "pkg"
    src = tmp_path / "src"
    _write_handwritten_stub(src, name="hand")

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules={"kept"},
        source_dirs=[src],
        battery_dir=None,
        contract_refs=set(),
    )
    assert orphans == []


def test_battery_orphaned_when_contract_ref_gone(tmp_path: Path):
    pkg = tmp_path / "pkg"
    battery = tmp_path / "tests" / "contract"
    kept = _write_battery(battery, name="test_kept", derived_from="mod:kept")
    gone = _write_battery(battery, name="test_gone", derived_from="mod:gone")

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules=set(),
        source_dirs=[pkg],
        battery_dir=battery,
        contract_refs={"mod:kept"},
    )
    paths = {o.path for o in orphans}
    assert gone in paths
    assert kept not in paths
    assert all(o.kind == "contract_battery" for o in orphans)
    assert next(o for o in orphans if o.path == gone).source_module == "mod:gone"


def test_sidecar_follows_its_generated_module(tmp_path: Path):
    pkg = tmp_path / "pkg"
    # kept module + sidecar: neither orphaned.
    kept_gen = _write_generated(pkg, "__generated__", module="kept", source_module="kept")
    _write_sidecar(kept_gen)
    # deleted module + sidecar: both orphaned.
    del_gen = _write_generated(pkg, "__generated__", module="deleted", source_module="deleted")
    del_sidecar = _write_sidecar(del_gen)

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules={"kept"},
        source_dirs=[pkg],
        battery_dir=None,
        contract_refs=set(),
    )
    paths = {o.path for o in orphans}
    assert del_gen in paths
    assert del_sidecar in paths
    assert kept_gen not in paths
    # kept sidecar is never present.
    assert all("kept" not in str(o.path) for o in orphans)


def test_find_orphans_returns_sorted_by_path(tmp_path: Path):
    pkg = tmp_path / "pkg"
    b_gen = _write_generated(pkg, "__generated__", module="bbb", source_module="bbb")
    a_gen = _write_generated(pkg, "__generated__", module="aaa", source_module="aaa")

    orphans = find_orphans(
        package_dir=pkg,
        generated_dir="__generated__",
        governed_modules=set(),
        source_dirs=[pkg],
        battery_dir=None,
        contract_refs=set(),
    )
    generated = [o.path for o in orphans if o.kind == "generated"]
    assert generated == sorted([a_gen, b_gen])


def test_orphan_artifact_is_frozen():
    art = OrphanArtifact(path=Path("x"), kind="generated", source_module="m")
    try:
        art.path = Path("y")  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("OrphanArtifact should be frozen")


def test_newly_governed_only_module_origin_and_unbuilt(tmp_path: Path):
    pkg = tmp_path / "pkg"
    # built module-origin: has a generated artifact -> not reported.
    _write_generated(pkg, "__generated__", module="built", source_module="built")

    entries = [
        _entry("unbuiltmod", "make_thing", origin="module"),
        _entry("built", "already", origin="module"),
        _entry("decmod", "dec_symbol", origin="decorator"),
    ]

    result = newly_governed_specs(entries, package_dir=pkg, generated_dir="__generated__")
    assert result == {"unbuiltmod": ["make_thing"]}


def test_newly_governed_sorts_symbols(tmp_path: Path):
    pkg = tmp_path / "pkg"
    entries = [
        _entry("mod", "zeta", origin="module"),
        _entry("mod", "alpha", origin="module"),
    ]
    result = newly_governed_specs(entries, package_dir=pkg, generated_dir="__generated__")
    assert result == {"mod": ["alpha", "zeta"]}
