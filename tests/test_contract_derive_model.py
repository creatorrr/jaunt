# tests/test_contract_derive_model.py
from __future__ import annotations

import asyncio
import json

from jaunt.contract.derive import ContractBlocks, ExampleRow, RaisesRow, extract_blocks_via_model

CANNED = json.dumps(
    {
        "examples": [{"input": '"hi"', "expected": '"HI"'}],
        "raises": [{"input": '""', "exc": "ValueError"}],
    }
)


def test_extract_blocks_via_model_parses_json() -> None:
    async def fake_complete(system: str, user: str) -> str:
        assert "contract" in system.lower()
        return CANNED

    blocks = asyncio.run(extract_blocks_via_model("Shout the text.", complete=fake_complete))
    assert blocks.examples == (ExampleRow('"hi"', '"HI"'),)
    assert blocks.raises == (RaisesRow('""', "ValueError"),)


def test_extract_blocks_via_model_tolerates_fenced_json() -> None:
    async def fake_complete(system: str, user: str) -> str:
        return f"```json\n{CANNED}\n```"

    blocks = asyncio.run(extract_blocks_via_model("x", complete=fake_complete))
    assert not blocks.is_empty()


def test_reconcile_entry_uses_model_when_unstructured(tmp_path) -> None:
    # Body satisfies the model-derived contract; reconcile should write the battery.
    from jaunt import registry
    from jaunt.contract import runner

    registry.clear_registries()
    src = (
        "import jaunt\n\n\n"
        "@jaunt.contract\n"
        "def shout(text):\n"
        '    "Shout the text. Empty input is an error."\n'
        "    if not text:\n"
        "        raise ValueError('empty')\n"
        "    return text.upper()\n"
    )
    mod_dir = tmp_path / "src"
    mod_dir.mkdir()
    (mod_dir / "demo.py").write_text(src, encoding="utf-8")

    import sys

    sys.path.insert(0, str(mod_dir))
    sys.modules.pop("demo", None)
    try:
        import importlib

        mod = importlib.import_module("demo")
        entry = next(iter(registry.get_contract_registry().values()))

        def model_extract(prose: str) -> ContractBlocks:
            return ContractBlocks(
                examples=(ExampleRow('"hi"', '"HI"'),),
                raises=(RaisesRow('""', "ValueError"),),
            )

        result = runner.reconcile_entry(
            tmp_path,
            "tests/contract",
            ["examples", "errors"],
            False,
            entry,
            module_namespace=vars(mod),
            tool_version="0.4.4",
            model_extract=model_extract,
        )
        assert result.ok
        assert (tmp_path / "tests" / "contract" / "demo" / "test_shout.py").is_file()
    finally:
        sys.path.remove(str(mod_dir))
        sys.modules.pop("demo", None)
        registry.clear_registries()
