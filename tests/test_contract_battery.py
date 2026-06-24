from __future__ import annotations

from jaunt.contracts.battery import DerivedRegion, merge_battery, parse_battery, render_battery

FIELDS = {
    "derived_from": "demo:slugify",
    "prose_digest": "aa",
    "signature": "bb",
    "body_digest": "cc",
    "strength": "2/2",
    "tool_version": "0.4.4",
}

EX = DerivedRegion(
    region_id="examples",
    code='@pytest.mark.parametrize("arg,want", [("Hi", "hi")])\n'
    "def test_examples(arg, want):  # derived from: Examples\n"
    "    assert slugify(arg) == want",
)


def test_render_is_parseable_and_round_trips() -> None:
    text = render_battery(
        import_module="demo", func_name="slugify", regions=[EX], header_fields=FIELDS
    )
    assert "import pytest" in text
    assert "from demo import slugify" in text
    parsed = parse_battery(text)
    assert parsed.header is not None
    assert parsed.header["derived-from"] == "demo:slugify"
    assert "test_examples" in parsed.regions["examples"]
    assert parsed.preserved.strip() == ""


def test_merge_preserves_hand_added_cases_and_updates_region() -> None:
    text = render_battery(
        import_module="demo", func_name="slugify", regions=[EX], header_fields=FIELDS
    )
    # User appends a hand-written test outside the derived markers.
    text += "\n\ndef test_hand_added():\n    assert slugify('A') == 'a'\n"

    new_region = DerivedRegion(
        region_id="examples",
        code='@pytest.mark.parametrize("arg,want", [("Hi", "hi"), ("Yo", "yo")])\n'
        "def test_examples(arg, want):  # derived from: Examples\n"
        "    assert slugify(arg) == want",
    )
    merged = merge_battery(
        text,
        import_module="demo",
        func_name="slugify",
        regions=[new_region],
        header_fields={**FIELDS, "body_digest": "dd"},
    )
    assert "test_hand_added" in merged  # preserved
    assert '("Yo", "yo")' in merged  # region updated
    merged_header = parse_battery(merged).header
    assert merged_header is not None
    assert merged_header["body-digest"] == "sha256:dd"  # header refreshed


def test_merge_preserves_hand_added_imports_outside_preamble() -> None:
    text = render_battery(
        import_module="demo", func_name="slugify", regions=[EX], header_fields=FIELDS
    )
    # User appends a hand-written import + test below the derived markers. The
    # generated preamble strip must not reach down here and drop these lines.
    text += (
        "\n\nimport math\n"
        "from demo import other_helper\n\n"
        "def test_hand_added():\n"
        "    assert math.floor(slugify_len('A')) == 1\n"
        "    assert other_helper() is not None\n"
    )

    merged = merge_battery(
        text,
        import_module="demo",
        func_name="slugify",
        regions=[EX],
        header_fields=FIELDS,
    )
    assert "import math" in merged
    assert "from demo import other_helper" in merged
    assert "test_hand_added" in merged

    preserved = parse_battery(text).preserved
    assert "import math" in preserved
    assert "from demo import other_helper" in preserved
