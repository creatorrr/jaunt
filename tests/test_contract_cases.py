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
