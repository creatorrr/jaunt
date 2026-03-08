from __future__ import annotations

from pathlib import Path

from jaunt.module_contract import build_module_contract
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
