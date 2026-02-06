"""
CSV → Typed Records — Test Specs
"""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_basic_parsing() -> None:
    """
    Given a dataclass:
        @dataclass
        class User:
            name: str
            age: int
            active: bool

    And CSV text:
        name,age,active
        Alice,30,true
        Bob,25,false

    parse_csv(text, User) should return [User("Alice", 30, True), User("Bob", 25, False)].
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_whitespace_handling() -> None:
    """
    Cell values with leading/trailing whitespace should be stripped
    before coercion.

    CSV: "name , age \\n  Alice , 30 "
    Should still parse correctly.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_strict_mode_extra_column_raises() -> None:
    """
    In strict mode, a CSV header with a column not in the dataclass
    should raise ValueError.

    Dataclass has fields: name, age
    CSV has headers: name, age, email
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_lenient_mode_skips_bad_rows() -> None:
    """
    In lenient mode (strict=False), rows where coercion fails
    should be silently skipped.

    Dataclass: name (str), age (int)
    CSV:
        name,age
        Alice,30
        Bob,not_a_number
        Carol,25

    Result should be [User("Alice", 30), User("Carol", 25)].
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_not_a_dataclass_raises() -> None:
    """
    parse_csv("a\\n1", dict) should raise TypeError
    because dict is not a dataclass.
    """
    raise AssertionError("spec stub (generated at test time)")


@jaunt.test()
def test_bool_coercion_variants() -> None:
    """
    All of these should coerce to True: "true", "True", "TRUE", "1", "yes", "YES"
    All of these should coerce to False: "false", "False", "0", "no", "NO"
    """
    raise AssertionError("spec stub (generated at test time)")
