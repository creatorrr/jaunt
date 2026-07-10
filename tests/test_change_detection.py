from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from types import SimpleNamespace

from jaunt.change_detection import (
    assess_specs,
    classify_change,
    gate_prose,
    read_contract_sidecar,
    sidecar_path,
    write_contract_sidecar,
)
from jaunt.config import SemanticGateConfig
from jaunt.digest import contract_snapshot
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.write_text(textwrap.dedent(text).strip() + "\n", encoding="utf-8")


def _entry(
    *,
    spec_ref: str,
    module: str,
    qualname: str,
    source_file: str,
    decorator_kwargs: dict[str, object] | None = None,
) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(spec_ref),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs=decorator_kwargs or {},
    )


def test_classify_change_detects_structural_change(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        '''
        def Foo(value: int) -> int:
            """Return the value."""
            ...
        ''',
    )
    old_entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))
    old_snapshot = contract_snapshot(old_entry)

    _write(
        p,
        '''
        def Foo(value: int, fallback: int = 0) -> int:
            """Return the value."""
            ...
        ''',
    )
    new_entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))

    assert classify_change(old_snapshot, new_entry) == "structural"


def test_classify_change_detects_prose_change(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        '''
        def Foo(value: int) -> int:
            """Return the input value."""
            ...
        ''',
    )
    old_entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))
    old_snapshot = contract_snapshot(old_entry)

    _write(
        p,
        '''
        def Foo(value: int) -> int:
            """Return the input value unchanged."""
            ...
        ''',
    )
    new_entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))

    assert classify_change(old_snapshot, new_entry) == "prose"


def test_classify_change_detects_no_change(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        '''
        def Foo(value: int) -> int:
            """Return the input value."""
            ...
        ''',
    )
    entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))
    old_snapshot = contract_snapshot(entry)

    assert classify_change(old_snapshot, entry) == "none"


def test_classify_change_treats_missing_old_snapshot_as_structural(tmp_path: Path) -> None:
    p = tmp_path / "m.py"
    _write(
        p,
        '''
        def Foo(value: int) -> int:
            """Return the input value."""
            ...
        ''',
    )
    entry = _entry(spec_ref="m:Foo", module="m", qualname="Foo", source_file=str(p))

    assert classify_change(None, entry) == "structural"
    assert classify_change({}, entry) == "structural"


def test_gate_prose_accepts_equivalent_token() -> None:
    seen: dict[str, object] = {}

    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(final_message="EQUIVALENT")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "EQUIVALENT"
    assert seen["model"] == "gpt-5.6-luna"
    assert seen["reasoning_effort"] == "medium"


def test_gate_prose_strips_equivalent_token() -> None:
    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="  EQUIVALENT\n")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "EQUIVALENT"


def test_gate_prose_treats_rebuild_as_meaningful() -> None:
    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="REBUILD")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "MEANINGFUL"


def test_gate_prose_treats_garbage_as_meaningful() -> None:
    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="some garbage text")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "MEANINGFUL"


def test_gate_prose_treats_empty_message_as_meaningful() -> None:
    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(final_message="")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "MEANINGFUL"


def test_gate_prose_treats_exec_error_as_meaningful() -> None:
    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        raise RuntimeError("codex failed")

    verdict = asyncio.run(
        gate_prose(
            old_prose="old",
            new_prose="new",
            signature="def Foo() -> None",
            cfg=SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdict == "MEANINGFUL"


def test_assess_specs_rolls_up_changes_and_only_gates_prose(tmp_path: Path) -> None:
    structural_path = tmp_path / "structural.py"
    unchanged_path = tmp_path / "unchanged.py"
    prose_equiv_path = tmp_path / "prose_equiv.py"
    prose_meaningful_path = tmp_path / "prose_meaningful.py"
    new_path = tmp_path / "new.py"

    _write(
        structural_path,
        '''
        def Structural(value: int) -> int:
            """Return the value."""
            ...
        ''',
    )
    structural_old = _entry(
        spec_ref="m:Structural",
        module="m",
        qualname="Structural",
        source_file=str(structural_path),
    )
    structural_snapshot = contract_snapshot(structural_old)
    _write(
        structural_path,
        '''
        def Structural(value: int, fallback: int = 0) -> int:
            """Return the value."""
            ...
        ''',
    )
    structural_entry = _entry(
        spec_ref="m:Structural",
        module="m",
        qualname="Structural",
        source_file=str(structural_path),
    )

    _write(
        unchanged_path,
        '''
        def Unchanged(value: int) -> int:
            """Return the value."""
            ...
        ''',
    )
    unchanged_entry = _entry(
        spec_ref="m:Unchanged",
        module="m",
        qualname="Unchanged",
        source_file=str(unchanged_path),
    )
    unchanged_snapshot = contract_snapshot(unchanged_entry)

    _write(
        prose_equiv_path,
        '''
        def ProseEquivalent(value: int) -> int:
            """Return the input value."""
            ...
        ''',
    )
    prose_equiv_old = _entry(
        spec_ref="m:ProseEquivalent",
        module="m",
        qualname="ProseEquivalent",
        source_file=str(prose_equiv_path),
    )
    prose_equiv_snapshot = contract_snapshot(prose_equiv_old)
    _write(
        prose_equiv_path,
        '''
        def ProseEquivalent(value: int) -> int:
            """Return the input value unchanged."""
            ...
        ''',
    )
    prose_equiv_entry = _entry(
        spec_ref="m:ProseEquivalent",
        module="m",
        qualname="ProseEquivalent",
        source_file=str(prose_equiv_path),
    )

    _write(
        prose_meaningful_path,
        '''
        def ProseMeaningful(value: int) -> int:
            """Return the input value."""
            ...
        ''',
    )
    prose_meaningful_old = _entry(
        spec_ref="m:ProseMeaningful",
        module="m",
        qualname="ProseMeaningful",
        source_file=str(prose_meaningful_path),
    )
    prose_meaningful_snapshot = contract_snapshot(prose_meaningful_old)
    _write(
        prose_meaningful_path,
        '''
        def ProseMeaningful(value: int) -> int:
            """Return twice the input value."""
            ...
        ''',
    )
    prose_meaningful_entry = _entry(
        spec_ref="m:ProseMeaningful",
        module="m",
        qualname="ProseMeaningful",
        source_file=str(prose_meaningful_path),
    )

    _write(
        new_path,
        '''
        def NewSpec(value: int) -> int:
            """Return the value."""
            ...
        ''',
    )
    new_entry = _entry(
        spec_ref="m:NewSpec",
        module="m",
        qualname="NewSpec",
        source_file=str(new_path),
    )

    gate_tokens = ["EQUIVALENT", "MEANINGFUL"]
    gate_calls: list[dict[str, object]] = []

    async def fake_run_exec(**kwargs: object) -> SimpleNamespace:
        gate_calls.append(kwargs)
        return SimpleNamespace(final_message=gate_tokens[len(gate_calls) - 1])

    entries = [
        structural_entry,
        unchanged_entry,
        prose_equiv_entry,
        prose_meaningful_entry,
        new_entry,
    ]
    old_snapshots = {
        str(structural_entry.spec_ref): structural_snapshot,
        str(unchanged_entry.spec_ref): unchanged_snapshot,
        str(prose_equiv_entry.spec_ref): prose_equiv_snapshot,
        str(prose_meaningful_entry.spec_ref): prose_meaningful_snapshot,
    }

    verdicts = asyncio.run(
        assess_specs(
            entries,
            old_snapshots,
            SemanticGateConfig(),
            run_exec=fake_run_exec,
        )
    )

    assert verdicts == {
        structural_entry.spec_ref: "MEANINGFUL",
        unchanged_entry.spec_ref: "EQUIVALENT",
        prose_equiv_entry.spec_ref: "EQUIVALENT",
        prose_meaningful_entry.spec_ref: "MEANINGFUL",
        new_entry.spec_ref: "MEANINGFUL",
    }
    assert len(gate_calls) == 2


def test_sidecar_path_appends_contract_json_suffix() -> None:
    assert sidecar_path(Path("/x/foo.py")) == Path("/x/foo.py.contract.json")


def test_contract_sidecar_round_trip(tmp_path: Path) -> None:
    p = tmp_path / "m.py.contract.json"
    snapshots = {
        "m:Foo": {
            "kind": "function",
            "signature": "def Foo() -> None",
            "decorator_meta": "{}",
            "prose": "Do the thing.",
        }
    }

    write_contract_sidecar(p, snapshots)

    assert read_contract_sidecar(p) == snapshots


def test_read_contract_sidecar_missing_file_returns_empty(tmp_path: Path) -> None:
    assert read_contract_sidecar(tmp_path / "nope.json") == {}


def test_read_contract_sidecar_corrupt_file_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("not json{", encoding="utf-8")

    assert read_contract_sidecar(p) == {}


def test_read_contract_sidecar_non_dict_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "list.json"
    p.write_text("[]", encoding="utf-8")

    assert read_contract_sidecar(p) == {}


def test_read_contract_sidecar_filters_non_dict_values(tmp_path: Path) -> None:
    p = tmp_path / "mixed.json"
    p.write_text('{"a": {"x": 1}, "b": 5}', encoding="utf-8")

    assert read_contract_sidecar(p) == {"a": {"x": 1}}
