from __future__ import annotations

from jaunt.validation import (
    validate_build_generated_source,
    validate_generated_source,
    validate_test_generated_source,
)


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


def test_build_validation_rejects_shadowing_handwritten_symbols() -> None:
    src = "class Mark:\n    pass\n\ndef play() -> str:\n    return 'ok'\n"
    errs = validate_build_generated_source(
        src,
        ["play"],
        spec_module="pkg.specs",
        handwritten_names={"Mark", "WIN_LINES"},
    )
    assert any("Mark" in err and "pkg.specs" in err for err in errs)


def test_test_validation_rejects_wrapper_introspection_by_default() -> None:
    src = (
        "def test_game_flow() -> None:\n"
        "    value = target.__globals__['Mark']\n"
        "    assert value is not None\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": True},
        target_modules_by_name={},
    )
    assert any("__globals__" in err for err in errs)


def test_test_validation_allows_white_box_opt_out() -> None:
    src = (
        "def test_game_flow() -> None:\n"
        "    value = target.__globals__['Mark']\n"
        "    assert value is not None\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": False},
        target_modules_by_name={},
    )
    assert errs == []


def test_test_validation_rejects_monkeypatching_target_module_attributes() -> None:
    src = (
        "import pkg.feature as feature\n\n"
        "def test_game_flow(monkeypatch) -> None:\n"
        "    monkeypatch.setattr(feature, 'helper', lambda: 1)\n"
        "    assert True\n"
    )
    errs = validate_test_generated_source(
        src,
        ["test_game_flow"],
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        public_api_only_by_name={"test_game_flow": True},
        target_modules_by_name={"test_game_flow": ("pkg.feature",)},
    )
    assert any("monkeypatch target-module attribute" in err for err in errs)
