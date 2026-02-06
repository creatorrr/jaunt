"""Tests for the expression evaluator."""

from __future__ import annotations

import jaunt


@jaunt.test()
def test_tokenize_arithmetic():
    """
    Tokenize "3 + 4.5 * 2" and verify:
    - 6 tokens total (5 meaningful + EOF)
    - Token kinds in order: NUMBER, PLUS, NUMBER, STAR, NUMBER, EOF
    - Token positions are correct offsets into the input string
      (0 for "3", 2 for "+", 4 for "4.5", 8 for "*", 10 for "2")
    """


@jaunt.test()
def test_tokenize_power_operator():
    """
    Tokenize "x ** 2" and verify:
    - 4 tokens: IDENT, POWER, NUMBER, EOF
    - The "**" is a single POWER token, not two separate STAR tokens
    """


@jaunt.test()
def test_tokenize_error_on_invalid_char():
    """
    Tokenize "3 + @" and verify SyntaxError is raised.
    The '@' is at 0-based index 4 in the input string.
    The error message should contain the '@' character.
    """


@jaunt.test()
def test_parse_precedence_via_evaluation():
    """
    Verify operator precedence by evaluating expressions:
    - evaluate("2 + 3 * 4") => 14.0  (multiplication before addition)
    - evaluate("2 * 3 + 4") => 10.0  (multiplication before addition)
    - evaluate("(2 + 3) * 4") => 20.0  (parens override precedence)
    - evaluate("2 + 6 / 3") => 4.0  (division before addition)
    """


@jaunt.test()
def test_parse_right_associative_power():
    """
    Verify right-associativity of ** through evaluation:
    - evaluate("2 ** 3 ** 2") => 512.0
      because 2 ** (3 ** 2) = 2 ** 9 = 512
      NOT (2 ** 3) ** 2 = 8 ** 2 = 64
    """


@jaunt.test()
def test_parse_function_call_structure():
    """
    parse("max(1, 2 + 3)") should return an AST node representing
    a function call to "max" with 2 arguments.
    Verify by evaluating: evaluate("max(1, 2 + 3)") => 5.0
    Also: evaluate("min(10, 20, 5)") => 5.0
    """


@jaunt.test()
def test_unary_minus():
    """
    evaluate("-5")     => -5.0
    evaluate("--5")    => 5.0  (double negation)
    evaluate("-(-3)")  => 3.0
    evaluate("2 * -3") => -6.0
    """


@jaunt.test()
def test_evaluate_basic_arithmetic():
    """
    evaluate("2 + 3")       => 5.0
    evaluate("10 - 4 * 2")  => 2.0
    evaluate("(10 - 4) * 2") => 12.0
    evaluate("7 % 3")       => 1.0
    evaluate("2 ** 10")     => 1024.0
    """


@jaunt.test()
def test_evaluate_nested_functions():
    """
    evaluate("max(abs(-5), min(3, 7))")  => 5.0
    evaluate("sqrt(abs(-16))")           => 4.0
    evaluate("round(3.7)")               => 4.0
    """


@jaunt.test()
def test_evaluate_variables():
    """
    evaluate("x * 2 + y", {"x": 5, "y": 3})  => 13.0
    evaluate("a ** b", {"a": 2, "b": 8})      => 256.0
    """


@jaunt.test()
def test_evaluate_undefined_variable_raises():
    """
    evaluate("x + 1") should raise NameError containing "x".
    evaluate("foo(1)") where "foo" is not a built-in should raise NameError
    containing "foo".
    """


@jaunt.test()
def test_evaluate_division_by_zero():
    """
    evaluate("1 / 0")  should raise ZeroDivisionError.
    evaluate("5 % 0")  should raise ZeroDivisionError.
    """


@jaunt.test()
def test_evaluate_sqrt_negative():
    """
    evaluate("sqrt(-1)") should raise ValueError.
    """


@jaunt.test()
def test_evaluate_wrong_arity():
    """
    evaluate("abs()")       should raise TypeError (needs exactly 1 arg).
    evaluate("abs(1, 2)")   should raise TypeError (needs exactly 1 arg).
    evaluate("min()")       should raise TypeError (needs at least 1 arg).
    """


@jaunt.test()
def test_calc_convenience():
    """
    calc("x**2 + y**2", x=3, y=4) => 25.0
    calc("2 * pi * r", pi=3.14159, r=10) => close to 62.8318 (within 0.01)
    """


@jaunt.test()
def test_complex_expression():
    """
    A stress test combining many features:
    calc("max(abs(a - b), sqrt(c)) + ceil(2.3) ** 2", a=1, b=10, c=49)

    Expected:
    - abs(1 - 10) = 9
    - sqrt(49) = 7
    - max(9, 7) = 9
    - ceil(2.3) = 3
    - 3 ** 2 = 9
    - 9 + 9 = 18.0
    """
