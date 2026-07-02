from __future__ import annotations

from pathlib import Path

from jaunt.contract.runner import battery_path, run_battery_file
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _entry(qualname: str) -> SpecEntry:
    return SpecEntry(
        kind="contract",
        spec_ref=normalize_spec_ref(f"pkg.mod:{qualname}"),
        module="pkg.mod",
        qualname=qualname,
        source_file="src/pkg/mod.py",
        obj=None,
        decorator_kwargs={},
    )


def test_battery_path_sanitizes_dots(tmp_path: Path) -> None:
    p = battery_path(tmp_path, "tests/contract", _entry("Cls.method"))
    assert p.name == "test_Cls_method.py"


def test_async_battery_runs_green(tmp_path: Path) -> None:
    battery = tmp_path / "test_async_case.py"
    battery.write_text(
        "async def test_ok():\n    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )
    assert run_battery_file(battery, root=tmp_path, source_roots=[]) is True


def test_async_battery_failure_detected(tmp_path: Path) -> None:
    battery = tmp_path / "test_async_fail.py"
    battery.write_text(
        "async def test_bad():\n    assert 1 + 1 == 3\n",
        encoding="utf-8",
    )
    assert run_battery_file(battery, root=tmp_path, source_roots=[]) is False
