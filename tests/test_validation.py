from __future__ import annotations

from jaunt.validation import validate_generated_source


def test_validate_ok_with_expected_names() -> None:
    src = "def foo():\n    return 1\n"
    assert validate_generated_source(src, ["foo"]) == []


def test_validate_missing_name_mentions_symbol() -> None:
    src = "def foo():\n    return 1\n"
    errs = validate_generated_source(src, ["bar"])
    assert errs
    assert any("bar" in e for e in errs)


def test_validate_syntax_error_mentions_syntax() -> None:
    src = "def foo(:\n    pass\n"
    errs = validate_generated_source(src, ["foo"])
    assert errs
    joined = "\n".join(errs).lower()
    assert "syntax" in joined


def test_validate_class_and_assignment_count_as_defined() -> None:
    src = "class A:\n    pass\n\nCONSTANT = 1\n"
    assert validate_generated_source(src, ["A", "CONSTANT"]) == []


def test_validate_empty_expected_names_with_empty_source_ok() -> None:
    assert validate_generated_source("", []) == []
