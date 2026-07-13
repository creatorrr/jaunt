"""Deterministic parsing and rendering for TypeScript ``@prop`` cases."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from jaunt.errors import JauntConfigError

PROPERTY_RENDERER_SCHEME = "jaunt-ts-property/2"

_TAG = re.compile(r"(?m)^[ \t]*(?:/\*\*?[ \t]*)?(?:\*[ \t]*)?@prop\b(?P<body>[^\r\n]*)")
_BULLET_PREFIX = re.compile(r"^given\s+(?P<name>[A-Za-z_$][\w$]*)\s*:\s*")
_TOKEN = re.compile(
    r"\s+|"
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|"
    r"(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"(?:[A-Za-z_$][\w$]*)|"
    r"(?:[()[\].,+\-*/%])"
)
_IDENTIFIER = re.compile(r"^[A-Za-z_$][\w$]*$")
_NUMBER = re.compile(r"^\d")

_RESERVED_NAMES = {
    "await",
    "break",
    "case",
    "catch",
    "class",
    "const",
    "continue",
    "debugger",
    "default",
    "delete",
    "do",
    "else",
    "enum",
    "export",
    "extends",
    "false",
    "finally",
    "for",
    "function",
    "if",
    "implements",
    "import",
    "in",
    "instanceof",
    "interface",
    "let",
    "new",
    "null",
    "package",
    "private",
    "protected",
    "public",
    "return",
    "static",
    "super",
    "switch",
    "this",
    "throw",
    "true",
    "try",
    "typeof",
    "undefined",
    "var",
    "void",
    "while",
    "with",
    "yield",
}
_GLOBALS = {
    "Array",
    "BigInt",
    "Boolean",
    "Date",
    "JSON",
    "Math",
    "Number",
    "Object",
    "Promise",
    "RegExp",
    "String",
    "Uint8Array",
    "decodeURIComponent",
    "encodeURIComponent",
}
_LITERALS = {"false", "null", "true", "undefined"}
_BINARY_PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2, "%": 2}
_TYPE_STRATEGIES = {
    "string": ("string", "fc.string()"),
    "number": (
        "number",
        "fc.double({ noNaN: true, noDefaultInfinity: true })",
    ),
    "boolean": ("boolean", "fc.boolean()"),
    "bigint": ("bigint", "fc.bigInt()"),
    "Uint8Array": ("Uint8Array", "fc.uint8Array()"),
    "string[]": ("string[]", "fc.array(fc.string())"),
    "number[]": (
        "number[]",
        "fc.array(fc.double({ noNaN: true, noDefaultInfinity: true }))",
    ),
    "boolean[]": ("boolean[]", "fc.array(fc.boolean())"),
}
_ARBITRARY_TYPES = {
    "string": "string",
    "boolean": "boolean",
    "integer": "number",
    "nat": "number",
    "float": "number",
    "double": "number",
    "bigInt": "bigint",
    "uint8Array": "Uint8Array",
}
_STRATEGY_TOKEN = re.compile(
    r"\s+|"
    r'"(?:\\.|[^"\\])*"|'
    r"'(?:\\.|[^'\\])*'|"
    r"-?(?:\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)|"
    r"(?:[A-Za-z_$][\w$]*)|"
    r"(?:[()[\]{},.:])"
)
_STRATEGY_LITERALS = {"false", "null", "true", "undefined"}
_REJECTED_ARBITRARIES = {"anything"}
_UNSAFE_MEMBERS = {
    "__proto__",
    "constructor",
    "getOwnPropertyDescriptor",
    "getOwnPropertyDescriptors",
    "prototype",
}


@dataclass(frozen=True, slots=True)
class PropertyExpression:
    tokens: tuple[str, ...]
    root_positions: tuple[int, ...]
    asynchronous: bool

    @property
    def canonical(self) -> str:
        return _join_tokens(self.tokens)


@dataclass(frozen=True, slots=True)
class PropertyCase:
    name: str
    expected_type: str
    arbitrary: str
    operator: str
    left: PropertyExpression
    right: PropertyExpression
    case_id: str
    seed: int

    @property
    def asynchronous(self) -> bool:
        return self.left.asynchronous or self.right.asynchronous

    @property
    def invariant(self) -> str:
        operator = "equals" if self.operator == "equals" else "does not equal"
        return f"{self.left.canonical} {operator} {self.right.canonical}"

    def payload(self) -> dict[str, object]:
        return {
            "caseId": self.case_id,
            "seed": self.seed,
            "name": self.name,
            "expectedType": self.expected_type,
            "arbitrary": self.arbitrary,
            "operator": self.operator,
            "left": self.left.canonical,
            "right": self.right.canonical,
            "async": self.asynchronous,
        }


class _ExpressionParser:
    def __init__(self, tokens: Sequence[str]) -> None:
        self.tokens = tuple(tokens)
        self.position = 0
        self.roots: list[int] = []
        self.asynchronous = False

    def parse(self) -> PropertyExpression:
        self._expression()
        if self.position != len(self.tokens):
            raise ValueError(f"unexpected token {self.tokens[self.position]!r}")
        return PropertyExpression(self.tokens, tuple(self.roots), self.asynchronous)

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of expression")
        if expected is not None and token != expected:
            raise ValueError(f"expected {expected!r}, found {token!r}")
        self.position += 1
        return token

    def _expression(self, minimum_precedence: int = 0) -> None:
        self._unary()
        while (token := self._peek()) in _BINARY_PRECEDENCE:
            precedence = _BINARY_PRECEDENCE[token]
            if precedence < minimum_precedence:
                return
            self._take()
            self._expression(precedence + 1)

    def _unary(self) -> None:
        token = self._peek()
        if token in {"+", "-"}:
            self._take()
            self._unary()
            return
        if token == "await":
            self.asynchronous = True
            self._take()
            self._unary()
            return
        if token == "new":
            self._take()
            constructor_position = self.position
            constructor = self._take()
            if constructor not in _GLOBALS:
                raise ValueError(f"unsupported constructor {constructor!r}")
            self.roots.append(constructor_position)
            self._postfix()
            return
        self._primary()
        self._postfix()

    def _primary(self) -> None:
        token = self._peek()
        if token is None:
            raise ValueError("missing expression")
        if token == "(":
            self._take()
            self._expression()
            self._take(")")
            return
        if token == "[":
            self._take()
            if self._peek() != "]":
                while True:
                    self._expression()
                    if self._peek() != ",":
                        break
                    self._take(",")
            self._take("]")
            return
        if _IDENTIFIER.fullmatch(token):
            position = self.position
            self._take()
            if token not in _LITERALS:
                self.roots.append(position)
            return
        if _NUMBER.match(token) or token.startswith(('"', "'")):
            self._take()
            return
        raise ValueError(f"unsupported token {token!r}")

    def _postfix(self) -> None:
        while True:
            if self._peek() == ".":
                self._take()
                member = self._take()
                if (
                    not _IDENTIFIER.fullmatch(member)
                    or member in _RESERVED_NAMES
                    or member in _UNSAFE_MEMBERS
                ):
                    raise ValueError(f"invalid property name {member!r}")
                continue
            if self._peek() == "(":
                self._take()
                if self._peek() != ")":
                    while True:
                        self._expression()
                        if self._peek() != ",":
                            break
                        self._take(",")
                self._take(")")
                continue
            if self._peek() == "[":
                self._take()
                expression_start = self.position
                self._expression()
                expression_tokens = self.tokens[expression_start : self.position]
                self._take("]")
                if (
                    len(expression_tokens) != 1
                    or re.fullmatch(r"\d+", expression_tokens[0]) is None
                ):
                    raise ValueError("computed properties require one numeric literal index")
                if self._peek() == "(":
                    raise ValueError("calling computed properties is unsupported")
                continue
            return


def _tokens(value: str) -> tuple[str, ...]:
    tokens: list[str] = []
    cursor = 0
    while cursor < len(value):
        match = _TOKEN.match(value, cursor)
        if match is None:
            raise ValueError(f"unsupported text near {value[cursor : cursor + 12]!r}")
        token = match.group(0)
        cursor = match.end()
        if not token.isspace():
            tokens.append(token)
    return tuple(tokens)


def _join_tokens(tokens: Sequence[str]) -> str:
    rendered: list[str] = []
    previous = ""
    for token in tokens:
        if (
            rendered
            and (
                (_IDENTIFIER.fullmatch(previous) or _NUMBER.match(previous))
                or previous in {"await", "new"}
            )
            and (_IDENTIFIER.fullmatch(token) or _NUMBER.match(token))
        ):
            rendered.append(" ")
        elif rendered and previous in {"await", "new"} and re.match(r"[A-Za-z_$]", token):
            rendered.append(" ")
        elif rendered and previous == token and token in {"+", "-"}:
            rendered.append(" ")
        rendered.append(token)
        previous = token
    return "".join(rendered)


def _split_invariant(value: str) -> tuple[str, str, str]:
    tokens = _tokens(value)
    depth = 0
    candidates: list[tuple[int, int, str]] = []
    for index, token in enumerate(tokens):
        if token in {"(", "["}:
            depth += 1
        elif token in {
            ")",
            "]",
        }:
            depth -= 1
            if depth < 0:
                raise ValueError("unbalanced invariant delimiters")
        elif (
            depth == 0
            and token == "equals"
            and (index == 0 or tokens[index - 1] != ".")
            and (index + 1 >= len(tokens) or tokens[index + 1] != "(")
        ):
            candidates.append((index, 1, "equals"))
        elif (
            depth == 0
            and token == "does"
            and tuple(tokens[index : index + 3]) == ("does", "not", "equal")
        ):
            candidates.append((index, 3, "not_equals"))
    if depth != 0:
        raise ValueError("unbalanced invariant delimiters")
    if len(candidates) != 1:
        raise ValueError(
            "invariant must contain exactly one top-level `equals` or `does not equal`"
        )
    index, width, operator = candidates[0]
    left = tokens[:index]
    right = tokens[index + width :]
    if not left or not right:
        raise ValueError("invariant equality requires expressions on both sides")
    return _join_tokens(left), operator, _join_tokens(right)


def _split_property_body(body: str) -> tuple[str, str, str]:
    prefix = _BULLET_PREFIX.match(body)
    if prefix is None:
        raise ValueError("expected `given name: type-or-strategy :: left equals right`")
    name = prefix.group("name")
    start = prefix.end()
    quote: str | None = None
    escaped = False
    stack: list[str] = []
    pairs = {")": "(", "]": "[", "}": "{"}
    delimiters: list[int] = []
    index = start
    while index < len(body):
        character = body[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            index += 1
            continue
        if character in {'"', "'"}:
            quote = character
            index += 1
            continue
        if character in "([{":
            stack.append(character)
        elif character in ")]}":
            if not stack or stack[-1] != pairs[character]:
                raise ValueError("unbalanced strategy delimiters")
            stack.pop()
        elif not stack and body.startswith("::", index):
            delimiters.append(index)
            index += 2
            continue
        index += 1
    if quote is not None:
        raise ValueError("unterminated string in property strategy")
    if stack:
        raise ValueError("unbalanced strategy delimiters")
    if len(delimiters) != 1:
        raise ValueError("property intent must contain exactly one top-level `::`")
    delimiter = delimiters[0]
    strategy = body[start:delimiter].strip()
    invariant = body[delimiter + 2 :].strip()
    if not strategy or not invariant:
        raise ValueError("property strategy and invariant must not be empty")
    return name, strategy, invariant


class _StrategyParser:
    """Parse the executable subset allowed in an authored fast-check strategy.

    Strategy text is eventually compiled by the owning TypeScript project, but it
    is also executable test input.  Keeping the grammar to ``fc`` calls plus data
    literals admits compositional arbitraries without turning a TSDoc tag into an
    arbitrary JavaScript escape hatch.
    """

    def __init__(self, value: str) -> None:
        self.tokens = self._tokenize(value)
        self.position = 0

    @staticmethod
    def _tokenize(value: str) -> tuple[str, ...]:
        tokens: list[str] = []
        cursor = 0
        while cursor < len(value):
            match = _STRATEGY_TOKEN.match(value, cursor)
            if match is None:
                raise ValueError(f"unsupported strategy text near {value[cursor : cursor + 12]!r}")
            token = match.group(0)
            cursor = match.end()
            if not token.isspace():
                tokens.append(token)
        return tuple(tokens)

    def _peek(self) -> str | None:
        return self.tokens[self.position] if self.position < len(self.tokens) else None

    def _take(self, expected: str | None = None) -> str:
        token = self._peek()
        if token is None:
            raise ValueError("unexpected end of strategy")
        if expected is not None and token != expected:
            raise ValueError(f"expected {expected!r}, found {token!r}")
        self.position += 1
        return token

    def parse(self) -> tuple[str, str]:
        if not self.tokens:
            raise ValueError("strategy is empty")
        rendered, method = self._value(top_level=True)
        if self.position != len(self.tokens):
            raise ValueError(f"unexpected strategy token {self.tokens[self.position]!r}")
        if method is None:
            raise ValueError("strategy must be a fast-check call beginning with `fc.`")
        return rendered, method

    def _value(self, *, top_level: bool = False) -> tuple[str, str | None]:
        token = self._peek()
        if token == "fc":
            return self._call()
        if token == "{":
            if top_level:
                raise ValueError("strategy must be a fast-check call beginning with `fc.`")
            return self._object(), None
        if token == "[":
            if top_level:
                raise ValueError("strategy must be a fast-check call beginning with `fc.`")
            return self._array(), None
        if token in _STRATEGY_LITERALS or (
            token is not None
            and (
                token.startswith(('"', "'"))
                or re.fullmatch(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?", token)
            )
        ):
            if top_level:
                raise ValueError("strategy must be a fast-check call beginning with `fc.`")
            return self._take(), None
        raise ValueError(f"unsupported strategy value {token!r}")

    def _call(self) -> tuple[str, str]:
        self._take("fc")
        self._take(".")
        method = self._take()
        if not _IDENTIFIER.fullmatch(method) or method in _RESERVED_NAMES:
            raise ValueError(f"invalid fast-check arbitrary name {method!r}")
        if method in _REJECTED_ARBITRARIES:
            raise ValueError(f"unsupported fast-check arbitrary fc.{method}")
        self._take("(")
        arguments: list[str] = []
        if self._peek() != ")":
            while True:
                argument, _ = self._value()
                arguments.append(argument)
                if self._peek() != ",":
                    break
                self._take(",")
                if self._peek() == ")":
                    break
        self._take(")")
        return f"fc.{method}({','.join(arguments)})", method

    def _object(self) -> str:
        self._take("{")
        properties: list[str] = []
        if self._peek() != "}":
            while True:
                key = self._take()
                if not _IDENTIFIER.fullmatch(key):
                    raise ValueError(f"invalid strategy option key {key!r}")
                if key in _UNSAFE_MEMBERS:
                    raise ValueError(f"unsafe strategy option key {key!r}")
                self._take(":")
                value, _ = self._value()
                properties.append(f"{key}:{value}")
                if self._peek() != ",":
                    break
                self._take(",")
                if self._peek() == "}":
                    break
        self._take("}")
        return "{" + ",".join(properties) + "}"

    def _array(self) -> str:
        self._take("[")
        values: list[str] = []
        if self._peek() != "]":
            while True:
                value, _ = self._value()
                values.append(value)
                if self._peek() != ",":
                    break
                self._take(",")
                if self._peek() == "]":
                    break
        self._take("]")
        return "[" + ",".join(values) + "]"


def _strategy(value: str) -> tuple[str, str]:
    normalized = re.sub(r"\s+", "", value)
    if re.search(r"\bany\b", value):
        raise ValueError("property types and strategies must not contain `any`")
    if normalized in _TYPE_STRATEGIES:
        return _TYPE_STRATEGIES[normalized]
    arbitrary, method = _StrategyParser(value).parse()
    return _ARBITRARY_TYPES.get(method, "inferred"), arbitrary


def _semantic_case(
    *,
    name: str,
    expected_type: str,
    arbitrary: str,
    operator: str,
    left: PropertyExpression,
    right: PropertyExpression,
) -> tuple[str, int]:
    payload = json.dumps(
        {
            "name": name,
            "expectedType": expected_type,
            "arbitrary": arbitrary,
            "operator": operator,
            "left": left.canonical,
            "right": right.canonical,
            "async": left.asynchronous or right.asynchronous,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    digest = hashlib.sha256(payload.encode()).hexdigest()
    case_id = f"prop-{digest[:16]}"
    seed = int.from_bytes(hashlib.sha256(case_id.encode()).digest()[:4], "big") & 0x7FFF_FFFF
    return case_id, seed


def _validate_roots(
    expression: PropertyExpression,
    *,
    name: str,
    public_symbols: set[str],
    fixture_names: set[str],
    async_symbols: set[str],
) -> None:
    for position in expression.root_positions:
        identifier = expression.tokens[position]
        if identifier not in {name, *public_symbols, *fixture_names, *_GLOBALS}:
            raise ValueError(f"invariant references unsupported identifier {identifier!r}")
        if identifier in async_symbols and not expression.asynchronous:
            raise ValueError(f"async symbol {identifier!r} must be awaited in a property invariant")


def parse_property_cases(
    sources: Iterable[str],
    *,
    label: str,
    public_symbols: Iterable[str],
    fixture_names: Iterable[str] = (),
    async_symbols: Iterable[str] = (),
) -> tuple[PropertyCase, ...]:
    """Parse the supported one-line ``@prop`` grammar or raise before generation."""

    public = set(public_symbols)
    fixtures = set(fixture_names)
    asynchronous = set(async_symbols)
    cases: list[PropertyCase] = []
    seen: set[str] = set()
    for source in sources:
        matches = list(_TAG.finditer(source))
        for match in matches:
            body = match.group("body").split("*/", 1)[0].strip()
            try:
                name, strategy, invariant = _split_property_body(body)
            except ValueError as error:
                raise JauntConfigError(f"{label} has malformed @prop intent: {error}") from error
            if name in _RESERVED_NAMES:
                raise JauntConfigError(f"{label} property variable {name!r} is reserved")
            try:
                expected_type, arbitrary = _strategy(strategy)
                left_source, operator, right_source = _split_invariant(invariant)
                left = _ExpressionParser(_tokens(left_source)).parse()
                right = _ExpressionParser(_tokens(right_source)).parse()
                _validate_roots(
                    left,
                    name=name,
                    public_symbols=public,
                    fixture_names=fixtures,
                    async_symbols=asynchronous,
                )
                _validate_roots(
                    right,
                    name=name,
                    public_symbols=public,
                    fixture_names=fixtures,
                    async_symbols=asynchronous,
                )
            except ValueError as error:
                raise JauntConfigError(f"{label} has unsupported @prop intent: {error}") from error
            case_id, seed = _semantic_case(
                name=name,
                expected_type=expected_type,
                arbitrary=arbitrary,
                operator=operator,
                left=left,
                right=right,
            )
            if case_id in seen:
                raise JauntConfigError(f"{label} repeats the same @prop case {case_id}")
            seen.add(case_id)
            cases.append(
                PropertyCase(
                    name=name,
                    expected_type=expected_type,
                    arbitrary=arbitrary,
                    operator=operator,
                    left=left,
                    right=right,
                    case_id=case_id,
                    seed=seed,
                )
            )
    return tuple(cases)


def _render_expression(
    expression: PropertyExpression,
    *,
    symbol_aliases: Mapping[str, str],
    local_names: frozenset[str] = frozenset(),
) -> str:
    root_positions = set(expression.root_positions)
    tokens = [
        f"{symbol_aliases[token]}.{token}"
        if index in root_positions and token in symbol_aliases and token not in local_names
        else token
        for index, token in enumerate(expression.tokens)
    ]
    return _join_tokens(tokens)


def render_property_block(
    cases: Sequence[PropertyCase],
    *,
    symbol_specifiers: Mapping[str, str],
    num_runs: int,
    fixture_specifier: str = "",
    fixture_names: Sequence[str] = (),
) -> str:
    """Render complete typed property tests with collision-resistant bindings."""

    if not cases:
        return ""
    fixture_name_set = frozenset(fixture_names)
    referenced = {
        expression.tokens[position]
        for case in cases
        for expression in (case.left, case.right)
        for position in expression.root_positions
        if expression.tokens[position] in symbol_specifiers
        and expression.tokens[position] not in {case.name, *fixture_name_set}
    }
    specifiers = sorted({symbol_specifiers[name] for name in referenced})
    aliases = {
        specifier: f"__jauntPropertyTarget{index}" for index, specifier in enumerate(specifiers)
    }
    symbol_aliases = {
        name: aliases[specifier]
        for name, specifier in symbol_specifiers.items()
        if name in referenced
    }
    imports = [
        'import * as fc from "fast-check";',
        'import { expect as __jauntPropertyExpect } from "vitest";',
        (
            f"import {{ test as __jauntPropertyTest }} from {json.dumps(fixture_specifier)};"
            if fixture_specifier
            else 'import { test as __jauntPropertyTest } from "vitest";'
        ),
        *(
            f"import * as {alias} from {json.dumps(specifier)};"
            for specifier, alias in aliases.items()
        ),
    ]
    rendered = [*imports, ""]
    fixture_argument = f"({{ {', '.join(sorted(fixture_names))} }})" if fixture_names else "()"
    for case in cases:
        suffix = case.case_id.removeprefix("prop-")
        arbitrary = f"__jauntPropertyArbitrary_{suffix}"
        rendered.extend(
            [
                (
                    f"const {arbitrary} = {case.arbitrary} satisfies fc.Arbitrary<unknown>;"
                    if case.expected_type == "inferred"
                    else (
                        f"const {arbitrary}: fc.Arbitrary<{case.expected_type}> = {case.arbitrary};"
                    )
                ),
                f"__jauntPropertyTest({json.dumps(f'@prop {case.case_id}: {case.invariant}')}, "
                f"{'async ' if case.asynchronous else ''}{fixture_argument} => {{",
                f"  {'await ' if case.asynchronous else ''}fc.assert(",
                f"    fc.{'asyncProperty' if case.asynchronous else 'property'}(",
                f"      {arbitrary},",
                f"      {'async ' if case.asynchronous else ''}({case.name}) => {{",
            ]
        )
        local_names = frozenset((case.name, *fixture_name_set))
        left = _render_expression(
            case.left,
            symbol_aliases=symbol_aliases,
            local_names=local_names,
        )
        right = _render_expression(
            case.right,
            symbol_aliases=symbol_aliases,
            local_names=local_names,
        )
        matcher = "toEqual" if case.operator == "equals" else "not.toEqual"
        rendered.extend(
            [
                f"        __jauntPropertyExpect({left}).{matcher}({right});",
                "      },",
                "    ),",
                f"    {{ seed: {case.seed}, numRuns: {num_runs} }},",
                "  );",
                "});",
                "",
            ]
        )
    return "\n".join(rendered).rstrip() + "\n"


def attach_property_block(source: str, block: str) -> str:
    if not block:
        return source
    return f"{block.rstrip()}\n\n{source.lstrip()}".rstrip() + "\n"
