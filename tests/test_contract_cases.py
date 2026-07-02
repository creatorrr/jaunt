"""Tests for the call-plan case grammar (contract mode adoption parity)."""

from __future__ import annotations

from typing import Any

import pytest

from jaunt.contract.cases import CaseBlocks, CaseParseError, parse_case_blocks


def _parse(doc: str, **kw: Any) -> CaseBlocks:
    defaults: dict[str, Any] = dict(target="f", async_map={"f": False}, module_names=frozenset())
    defaults.update(kw)
    return parse_case_blocks(doc, **defaults)


class TestLegacySugar:
    def test_arrow_form_is_legacy_single_arg_call(self) -> None:
        blocks = _parse("Examples:\n    - 'a b' -> 'a-b'\n")
        assert len(blocks.examples) == 1
        case = blocks.examples[0]
        assert case.legacy is True
        assert case.call_expr == "f('a b')"
        assert case.expected_expr == "'a-b'"
        assert case.fixtures == ()
        assert case.imports == ()
        assert case.is_async is False

    def test_raises_sugar_forms(self) -> None:
        blocks = _parse("Raises:\n    - '' raises ValueError\n    - TypeError on 1\n")
        assert [(c.call_expr, c.exc_name) for c in blocks.raises] == [
            ("f('')", "ValueError"),
            ("f(1)", "TypeError"),
        ]
        assert all(c.legacy for c in blocks.raises)

    def test_unparseable_lines_are_skipped_like_today(self) -> None:
        # Legacy behavior: prose lines under Examples that are not parseable
        # cases are ignored, not errors (only explicit call-form lines error).
        blocks = _parse("Examples:\n    - lowercases everything\n")
        assert blocks.is_empty()


class TestCallForm:
    def test_multi_arg_kwargs_call(self) -> None:
        blocks = _parse("Examples:\n    - f([1, 2], sep='-') == '1-2'\n")
        case = blocks.examples[0]
        assert case.legacy is False
        assert case.call_expr == "f([1, 2], sep='-')"
        assert case.expected_expr == "'1-2'"

    def test_constructor_recipe_method_chain(self) -> None:
        blocks = _parse(
            "Examples:\n    - Counter(start=10).increment(5) == 15\n",
            target="Counter",
            async_map={"Counter.increment": False},
        )
        case = blocks.examples[0]
        assert case.call_expr == "Counter(start=10).increment(5)"
        assert case.method == "increment"

    def test_call_form_raises(self) -> None:
        blocks = _parse(
            "Raises:\n    - Counter(start=-1) raises ValueError\n",
            target="Counter",
            async_map={},
        )
        assert blocks.raises[0].call_expr == "Counter(start=-1)"
        assert blocks.raises[0].exc_name == "ValueError"

    def test_async_flag_from_async_map(self) -> None:
        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        assert blocks.examples[0].is_async is True

    def test_async_method_flag(self) -> None:
        blocks = _parse(
            "Examples:\n    - C().go(1) == 2\n",
            target="C",
            async_map={"C.go": True},
        )
        assert blocks.examples[0].is_async is True
        assert blocks.examples[0].method == "go"

    def test_chained_method_uses_outermost_attribute_for_async_flag(self) -> None:
        blocks = _parse(
            "Examples:\n    - C().sync().ago() == 2\n",
            target="C",
            async_map={"C.sync": False, "C.ago": True},
        )
        assert blocks.examples[0].is_async is True
        assert blocks.examples[0].method == "ago"


class TestNameClassification:
    def test_module_level_name_becomes_import(self) -> None:
        blocks = _parse(
            "Examples:\n    - f('alice') == User('alice')\n",
            module_names=frozenset({"User"}),
        )
        assert blocks.examples[0].imports == ("User",)

    def test_builtin_names_are_allowed_without_import(self) -> None:
        blocks = _parse("Examples:\n    - f(len('ab')) == 2\n")
        assert blocks.examples[0].imports == ()

    def test_unknown_name_is_parse_error_with_line(self) -> None:
        with pytest.raises(CaseParseError) as ei:
            _parse("Examples:\n    - f(mystery) == 1\n")
        assert "mystery" in str(ei.value)
        assert ei.value.line == "f(mystery) == 1"

    def test_custom_raises_exception_becomes_import(self) -> None:
        blocks = _parse(
            "Raises:\n    - f('') raises MyError\n",
            module_names=frozenset({"MyError"}),
        )
        assert blocks.raises[0].imports == ("MyError",)

    def test_builtin_raises_exception_is_not_imported(self) -> None:
        blocks = _parse(
            "Raises:\n    - f('') raises ValueError\n",
            module_names=frozenset({"ValueError"}),
        )
        assert blocks.raises[0].imports == ()

    def test_builtin_raises_exception_is_not_treated_as_fixture(self) -> None:
        blocks = _parse("Raises:\n    - f('') raises ValueError\n\nFixtures: ValueError\n")
        assert blocks.raises[0].fixtures == ()
        assert blocks.has_fixture_cases() is False

    def test_unknown_raises_exception_is_parse_error(self) -> None:
        with pytest.raises(CaseParseError) as ei:
            _parse("Raises:\n    - f('') raises MyError\n")
        assert "MyError" in str(ei.value)
        assert ei.value.line == "f('') raises MyError"


class TestFixtures:
    def test_fixtures_line_declares_names(self) -> None:
        doc = "Examples:\n    - f(db, 'alice') == 1\n\nFixtures: db\n"
        blocks = _parse(doc)
        assert blocks.fixtures_declared == ("db",)
        assert blocks.examples[0].fixtures == ("db",)
        assert blocks.has_fixture_cases() is True

    def test_declared_but_unused_fixture_is_fine(self) -> None:
        doc = "Examples:\n    - f(1) == 2\n\nFixtures: db\n"
        blocks = _parse(doc)
        assert blocks.fixtures_declared == ("db",)
        assert blocks.examples[0].fixtures == ()
        assert blocks.has_fixture_cases() is False


class TestMerged:
    def test_merged_concatenates_and_unions(self) -> None:
        a = _parse("Examples:\n    - f(1) == 1\n\nFixtures: db\n")
        b = _parse("Raises:\n    - f('') raises ValueError\n\nFixtures: tmp_path, db\n")
        m = a.merged(b)
        assert len(m.examples) == 1 and len(m.raises) == 1
        assert m.fixtures_declared == ("db", "tmp_path")


from jaunt.contract.derive import (  # noqa: E402
    battery_extra_imports,
    derive_case_regions,
)


class TestRenderRegions:
    def test_all_legacy_renders_byte_identical_to_today(self) -> None:
        doc = "Examples:\n    - 'a b' -> 'a-b'\n\nRaises:\n    - 1 raises TypeError\n"
        new = derive_case_regions(_parse(doc), target="f", derive=["examples", "errors"])
        golden_examples = (
            '@pytest.mark.parametrize("arg,want", [\n'
            "        ('a b', 'a-b'),\n"
            "    ])\n"
            "def test_examples(arg, want):  # derived from: Examples\n"
            "    assert f(arg) == want"
        )
        golden_errors = (
            '@pytest.mark.parametrize("arg", [1])\n'
            "def test_raises_typeerror(arg):  # derived from: Raises\n"
            "    with pytest.raises(TypeError):\n"
            "        f(arg)"
        )
        assert [(r.region_id, r.code) for r in new] == [
            ("examples", golden_examples),
            ("errors", golden_errors),
        ]

    def test_general_form_multi_arg(self) -> None:
        blocks = _parse("Examples:\n    - f([1, 2], sep='-') == '1-2'\n")
        [region] = derive_case_regions(blocks, target="f", derive=["examples"])
        assert region.region_id == "examples"
        assert "def test_examples():  # derived from: Examples" in region.code
        assert "assert f([1, 2], sep='-') == '1-2'" in region.code

    def test_async_examples_awaited(self) -> None:
        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        [region] = derive_case_regions(blocks, target="f", derive=["examples"])
        assert "async def test_examples():" in region.code
        assert "assert await f(1) == 2" in region.code

    def test_fixture_params(self) -> None:
        doc = "Examples:\n    - f(db, 'a') == 1\n\nFixtures: db\n"
        [region] = derive_case_regions(_parse(doc), target="f", derive=["examples"])
        assert "def test_examples(db):" in region.code

    def test_region_suffix_for_methods(self) -> None:
        blocks = _parse("Examples:\n    - C().go(1) == 2\n", target="C", async_map={"C.go": False})
        [region] = derive_case_regions(blocks, target="C", derive=["examples"], region_suffix="go")
        assert region.region_id == "examples-go"
        assert "def test_examples_go():" in region.code

    def test_general_raises(self) -> None:
        blocks = _parse("Raises:\n    - C(start=-1) raises ValueError\n", target="C", async_map={})
        [region] = derive_case_regions(blocks, target="C", derive=["errors"])
        assert "with pytest.raises(ValueError):" in region.code
        assert "C(start=-1)" in region.code

    def test_extra_imports_union(self) -> None:
        doc = "Examples:\n    - f('a') == User('a')\n    - f('b') == User('b')\n"
        blocks = _parse(doc, module_names=frozenset({"User"}))
        assert battery_extra_imports(blocks) == ("User",)


class TestBatteryExtraImports:
    def test_render_battery_emits_one_line_per_extra_import(self) -> None:
        from jaunt.contract.battery import render_battery

        text = render_battery(
            import_module="m",
            func_name="f",
            regions=[],
            header_fields={
                "derived_from": "m:f",
                "prose_digest": "0" * 64,
                "signature": "sha256:" + "0" * 64,
                "body_digest": "0" * 64,
                "strength": "0/0",
                "tool_version": "test",
            },
            extra_imports=("User",),
        )
        assert "from m import f\nfrom m import User\n" in text

    def test_no_extra_imports_is_byte_identical(self) -> None:
        from jaunt.contract.battery import render_battery

        header: dict[str, str] = {
            "derived_from": "m:f",
            "prose_digest": "0" * 64,
            "signature": "sha256:" + "0" * 64,
            "body_digest": "0" * 64,
            "strength": "0/0",
            "tool_version": "test",
        }
        without = render_battery(import_module="m", func_name="f", regions=[], header_fields=header)
        with_empty = render_battery(
            import_module="m",
            func_name="f",
            regions=[],
            header_fields=header,
            extra_imports=(),
        )
        assert without == with_empty


class TestEvaluateCases:
    def test_pure_example_pass_and_fail(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        blocks = _parse("Examples:\n    - f(1, 2) == 3\n    - f(1, 2) == 4\n")
        failures = evaluate_cases(blocks, namespace={"f": lambda a, b: a + b})
        assert len(failures) == 1
        assert "expected 4" in failures[0]

    def test_async_case_run_via_asyncio(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        async def f(x):
            return x + 1

        blocks = _parse("Examples:\n    - f(1) == 2\n", async_map={"f": True})
        assert evaluate_cases(blocks, namespace={"f": f}) == []

    def test_fixture_cases_are_skipped(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        doc = "Examples:\n    - f(db) == 1\n\nFixtures: db\n"
        assert evaluate_cases(_parse(doc), namespace={"f": lambda db: 1}) == []

    def test_raises_case(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        def f(x):
            if x == "":
                raise ValueError("empty")
            return x

        blocks = _parse("Raises:\n    - f('') raises ValueError\n")
        assert evaluate_cases(blocks, namespace={"f": f}) == []

    def test_custom_raises_case(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        class MyError(Exception):
            pass

        def f(x):
            if x == "":
                raise MyError("empty")
            return x

        blocks = _parse(
            "Raises:\n    - f('') raises MyError\n",
            module_names=frozenset({"MyError"}),
        )
        assert evaluate_cases(blocks, namespace={"f": f, "MyError": MyError}) == []

    def test_class_constructor_case(self) -> None:
        from jaunt.contract.derive import evaluate_cases

        class Counter:
            def __init__(self, start=0):
                self.n = start

            def increment(self, by):
                self.n += by
                return self.n

        blocks = _parse(
            "Examples:\n    - Counter(start=10).increment(5) == 15\n",
            target="Counter",
            async_map={"Counter.increment": False},
        )
        assert evaluate_cases(blocks, namespace={"Counter": Counter}) == []


class TestCaseStrength:
    def test_strength_counts_and_exclusions(self) -> None:
        from jaunt.contract.strength import compute_case_strength

        src = "def f(a, b):\n    return a + b\n"
        doc = "Examples:\n    - f(1, 2) == 3\n    - f(db, 1) == 2\n\nFixtures: db\n"
        blocks = _parse(doc)
        killed, applicable, excluded = compute_case_strength(src, "f", blocks, {})
        assert excluded == 1
        assert applicable > 0
        assert killed > 0


class TestHeaderStrengthExcluded:
    def test_field_omitted_when_zero(self) -> None:
        from jaunt.header import format_contract_battery_header

        base = dict(
            derived_from="m:f",
            prose_digest="0" * 64,
            signature="sha256:" + "0" * 64,
            body_digest="0" * 64,
            strength="1/2",
            tool_version="t",
        )
        assert "strength-excluded" not in format_contract_battery_header(**base)
        out = format_contract_battery_header(**base, strength_excluded="2")
        assert "# jaunt:strength-excluded=2" in out
