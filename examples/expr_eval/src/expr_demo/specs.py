"""
Expression Evaluator — Jaunt Example

A recursive-descent mathematical expression parser and evaluator
supporting arithmetic operators with correct precedence, parentheses,
variables, unary minus, and built-in function calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto

import jaunt


# ── Token types ──────────────────────────────────────────────────────


class TokenKind(Enum):
    """All lexical token types produced by the tokenizer."""

    NUMBER = auto()  # integer or float literal
    IDENT = auto()  # variable name or function name
    PLUS = auto()  # +
    MINUS = auto()  # -
    STAR = auto()  # *
    SLASH = auto()  # /
    PERCENT = auto()  # %
    POWER = auto()  # **
    LPAREN = auto()  # (
    RPAREN = auto()  # )
    COMMA = auto()  # ,
    EOF = auto()  # end of input


@dataclass(frozen=True)
class Token:
    """A single lexical token."""

    kind: TokenKind
    value: str  # raw text of the token (e.g. "3.14", "+", "sin")
    pos: int  # 0-based character offset in the input string


# ── Tokenizer ────────────────────────────────────────────────────────


@jaunt.magic()
def tokenize(expr: str) -> list[Token]:
    """
    Lex a mathematical expression string into tokens.

    Rules:
    - Skip whitespace.
    - Number literals: one or more digits, optionally followed by a '.'
      and one or more digits (e.g. "42", "3.14"). Leading dot is NOT a
      valid number (e.g. ".5" is invalid — raise SyntaxError).
    - Identifiers: a letter or underscore followed by zero or more
      letters, underscores, or digits (e.g. "x", "sin", "_foo2").
    - Two consecutive '*' characters form a single POWER token ("**").
      A lone '*' is STAR.
    - Single-character operators: + - / % ( ) ,
    - Raise SyntaxError with a message containing the offending character
      position if an unexpected character is encountered.
    - Always append an EOF token at the end.
    """
    raise RuntimeError("spec stub")


# ── AST nodes ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NumberLit:
    """Literal numeric value."""

    value: float


@dataclass(frozen=True)
class UnaryOp:
    """Unary operator (currently only negation)."""

    op: str  # "-"
    operand: ASTNode


@dataclass(frozen=True)
class BinOp:
    """Binary operator node."""

    op: str  # "+", "-", "*", "/", "%", "**"
    left: ASTNode
    right: ASTNode


@dataclass(frozen=True)
class VarRef:
    """Variable reference to be looked up in the environment."""

    name: str


@dataclass(frozen=True)
class FuncCall:
    """
    Function call node.

    name:  function identifier (e.g. "sin", "max")
    args:  list of argument expression nodes
    """

    name: str
    args: list[ASTNode] = field(default_factory=list)


# Union type alias
ASTNode = NumberLit | UnaryOp | BinOp | VarRef | FuncCall


# ── Parser ───────────────────────────────────────────────────────────


@jaunt.magic(deps=[tokenize])
def parse(expr: str) -> ASTNode:
    """
    Parse an expression string into an AST using recursive descent.

    Operator precedence (lowest to highest):
      1. Addition / subtraction   (+, -)       left-associative
      2. Multiplication / division / modulo (*, /, %)  left-associative
      3. Exponentiation (**)                  RIGHT-associative
      4. Unary minus (-)
      5. Atoms: numbers, variables, function calls, parenthesised exprs

    Function calls:
    - An identifier immediately followed by '(' is a function call.
    - Arguments are comma-separated expressions.
    - Zero arguments is valid: "rand()".
    - A bare identifier (not followed by '(') is a variable reference.

    Errors:
    - Raise SyntaxError if the token stream does not form a valid
      expression or if tokens remain after a complete expression.
    - Raise SyntaxError("unexpected end of expression") if EOF is
      encountered where a token was expected.
    - Include the token position in error messages.
    """
    raise RuntimeError("spec stub")


# ── Evaluator ────────────────────────────────────────────────────────


@jaunt.magic(deps=[parse])
def evaluate(
    expr: str,
    env: dict[str, float] | None = None,
) -> float:
    """
    Parse and evaluate an expression, returning a float result.

    `env` maps variable names to float values (default empty).

    Built-in functions (always available, case-sensitive):
      abs(x)         — absolute value
      min(a, b, ...) — minimum of 1+ args
      max(a, b, ...) — maximum of 1+ args
      sqrt(x)        — square root (x must be >= 0)
      round(x)       — round to nearest integer
      floor(x)       — floor
      ceil(x)        — ceiling

    Evaluation rules:
    - Division by zero raises ZeroDivisionError.
    - Modulo by zero raises ZeroDivisionError.
    - sqrt of a negative number raises ValueError.
    - Reference to an undefined variable raises NameError with the
      variable name in the message.
    - Call to an unknown function raises NameError with the function
      name in the message.
    - Wrong arity for a fixed-arity builtin (e.g. abs() with 0 or 2
      args) raises TypeError.
    - min/max with zero args raises TypeError.

    The result should be returned as a Python float. Integer expressions
    like "2 + 3" may return 5.0.
    """
    raise RuntimeError("spec stub")


# ── Convenience ──────────────────────────────────────────────────────


@jaunt.magic(deps=[evaluate])
def calc(expr: str, **variables: float) -> float:
    """
    Evaluate an expression with keyword-argument variables.

    This is the top-level convenience function:
        calc("x**2 + y**2", x=3, y=4)  # => 25.0

    Simply delegates to evaluate(expr, variables).
    """
    raise RuntimeError("spec stub")
