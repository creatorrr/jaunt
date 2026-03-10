from __future__ import annotations

from pathlib import Path

from jaunt.module_contract import (
    build_module_contract,
    extract_targeted_test_entries,
    target_modules_by_name,
    target_refs_by_test_name,
)
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


def _write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
    )


def test_module_contract_includes_handwritten_symbols_outside_expected_names(
    tmp_path: Path,
) -> None:
    source = tmp_path / "specs.py"
    _write(
        source,
        "from dataclasses import dataclass\n\n"
        "@dataclass(frozen=True)\n"
        "class Mark:\n"
        '    """Player mark."""\n'
        "    value: str\n\n"
        "WIN_LINES = ((1, 2, 3),)\n\n"
        "def play() -> str:\n"
        '    """Generated spec."""\n'
        "    raise RuntimeError\n",
    )

    contract = build_module_contract(
        entries=[_entry(module="pkg.specs", qualname="play", source_file=str(source))],
        expected_names=["play"],
    )

    assert contract.source_file == str(source)
    assert "Mark" in contract.handwritten_names
    assert "WIN_LINES" in contract.handwritten_names
    assert "play" not in contract.handwritten_names
    assert "class Mark" in contract.prompt_block
    assert "WIN_LINES" in contract.prompt_block


def test_module_contract_digest_changes_when_handwritten_helper_changes(tmp_path: Path) -> None:
    source = tmp_path / "specs.py"
    _write(
        source,
        "HELPER = 1\n\ndef play() -> str:\n    raise RuntimeError\n",
    )
    entry = _entry(module="pkg.specs", qualname="play", source_file=str(source))
    first = build_module_contract(entries=[entry], expected_names=["play"])

    _write(
        source,
        "HELPER = 2\n\ndef play() -> str:\n    raise RuntimeError\n",
    )
    second = build_module_contract(entries=[entry], expected_names=["play"])

    assert first.digest != second.digest


def test_test_target_refs_by_name_normalizes_explicit_targets(tmp_path: Path) -> None:
    source = tmp_path / "tests_specs.py"
    _write(source, "def test_render() -> None:\n    raise AssertionError\n")
    entry = _entry(
        module="tests.specs",
        qualname="test_render",
        source_file=str(source),
    )
    entry = SpecEntry(
        kind="test",
        spec_ref=entry.spec_ref,
        module=entry.module,
        qualname=entry.qualname,
        source_file=entry.source_file,
        obj=entry.obj,
        decorator_kwargs={"targets": ["pkg.ui.render_screen", "pkg.ui:play_cli"]},
    )

    refs = target_refs_by_test_name([entry])

    assert refs == {
        "test_render": (
            normalize_spec_ref("pkg.ui:play_cli"),
            normalize_spec_ref("pkg.ui:render_screen"),
        )
    }


def test_target_modules_by_name_uses_explicit_targets() -> None:
    entry = SpecEntry(
        kind="test",
        spec_ref=normalize_spec_ref("tests.specs:test_method"),
        module="tests.specs",
        qualname="test_method",
        source_file="tests/specs.py",
        obj=object(),
        decorator_kwargs={"targets": ["pkg.board:TaskBoard.validate_priority"]},
    )

    targets = target_modules_by_name([entry])

    assert targets == {"test_method": ("pkg.board",)}


def test_extract_targeted_test_entries_resolves_imported_names_and_methods(tmp_path: Path) -> None:
    source = tmp_path / "tests_specs.py"
    _write(
        source,
        "\n".join(
            [
                "import jaunt",
                "from pkg.board import TaskBoard, summarize",
                "from pkg.ui import render_screen",
                "",
                "@jaunt.test(",
                "    targets=[TaskBoard, TaskBoard.validate_priority, summarize, render_screen]",
                ")",
                "def test_board_flow() -> None:",
                '    """Exercise the board APIs."""',
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )

    entries = extract_targeted_test_entries("tests.specs", str(source))

    assert len(entries) == 1
    assert entries[0].decorator_kwargs["targets"] == (
        normalize_spec_ref("pkg.board:TaskBoard"),
        normalize_spec_ref("pkg.board:TaskBoard.validate_priority"),
        normalize_spec_ref("pkg.board:summarize"),
        normalize_spec_ref("pkg.ui:render_screen"),
    )


def test_extract_targeted_test_entries_rejects_unsupported_target_syntax(tmp_path: Path) -> None:
    source = tmp_path / "tests_specs.py"
    _write(
        source,
        "\n".join(
            [
                "import jaunt",
                "",
                "@jaunt.test(targets=build_targets())",
                "def test_board_flow() -> None:",
                '    raise AssertionError("spec stub")',
                "",
            ]
        ),
    )

    try:
        extract_targeted_test_entries("tests.specs", str(source))
    except ValueError as exc:
        assert "Unsupported target reference" in str(exc)
    else:
        raise AssertionError("expected unsupported static target syntax to fail")
