from __future__ import annotations

from jaunt.contract.derive import (
    ExampleRow,
    RaisesRow,
    derive_regions,
    evaluate_blocks,
    extract_blocks_structured,
)

DOC = """
Convert a title to a slug.

Examples:
- "  Hello, World!  " -> "hello-world"
- "C++ > Java" -> "c-java"

Raises:
- "" raises ValueError
- "   " raises ValueError
"""


def test_extract_examples_and_raises() -> None:
    blocks = extract_blocks_structured(DOC)
    assert blocks.examples == (
        ExampleRow('"  Hello, World!  "', '"hello-world"'),
        ExampleRow('"C++ > Java"', '"c-java"'),
    )
    assert blocks.raises == (
        RaisesRow('""', "ValueError"),
        RaisesRow('"   "', "ValueError"),
    )


def test_input_less_raises_row_is_ignored_by_deterministic_path() -> None:
    blocks = extract_blocks_structured("Raises:\n- ValueError if the title is empty.\n")
    assert blocks.raises == ()


def test_derive_regions_emit_parseable_pytest() -> None:
    blocks = extract_blocks_structured(DOC)
    regions = derive_regions(blocks, func_name="slugify", derive=["examples", "errors"])
    ids = {r.region_id for r in regions}
    assert ids == {"examples", "errors"}
    examples = next(r for r in regions if r.region_id == "examples")
    assert "def test_examples(" in examples.code
    assert '"hello-world"' in examples.code
    errors = next(r for r in regions if r.region_id == "errors")
    assert "pytest.raises(ValueError)" in errors.code


def test_evaluate_blocks_against_real_function() -> None:
    import re

    def slugify(title: str) -> str:
        cleaned = re.sub(r"[^a-z0-9]+", "-", title.strip().lower()).strip("-")
        if not cleaned:
            raise ValueError("empty")
        return cleaned

    blocks = extract_blocks_structured(DOC)
    ns = {"slugify": slugify}
    assert evaluate_blocks(slugify, blocks, ns) == []  # body satisfies its own contract

    def broken(title: str) -> str:
        return title  # ignores the contract

    assert evaluate_blocks(broken, blocks, {"slugify": broken}) != []
