"""Secure lifecycle and JSONL client for the project-local TypeScript worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
import stat
from bisect import bisect_right
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, NoReturn
from urllib.parse import unquote

from jaunt.errors import JauntConfigError
from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import (
    PROTOCOL_VERSION,
    InitializeParams,
    InitializeResult,
    ProtocolDiagnostic,
    ProtocolRequest,
    ProtocolResponse,
    ProtocolValidationError,
)

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_STARTUP_TIMEOUT = 10.0
_DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024
_DEFAULT_STDERR_BYTES = 64 * 1024
REQUIRED_WORKER_CAPABILITIES = (
    "analyze",
    "overlay",
    "sync",
    "orphans",
    "invalidate",
    "contract-projection",
    "recompose",
    "baseline-unselected",
    "release-programs",
    "test-runner-overlay-roots",
)
_CRASH_REPLAY_METHODS = frozenset(
    {
        "analyzeWorkspace",
        "analyzeContracts",
        "projectContract",
        "validateOverlay",
        "findOrphans",
    }
)
_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "HOME",
        "USERPROFILE",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SystemRoot",
        "COMSPEC",
        "PATHEXT",
        "LANG",
        "LC_ALL",
        "NO_COLOR",
    }
)
_REGEX_PREFIX_KEYWORDS = frozenset(
    {
        "await",
        "case",
        "default",
        "delete",
        "do",
        "else",
        "extends",
        "in",
        "instanceof",
        "new",
        "of",
        "return",
        "throw",
        "typeof",
        "void",
        "yield",
    }
)


def _identifier_precedes_regex(tokens: Sequence[tuple[str, str]]) -> bool:
    """Return whether the final identifier requires a following expression."""

    if not tokens or tokens[-1][0] != "identifier":
        return False
    return tokens[-1][1] in _REGEX_PREFIX_KEYWORDS and (
        len(tokens) < 2 or tokens[-2][1] not in {".", "?."}
    )


class TypeScriptWorkerError(JauntConfigError):
    """Base error for worker installation, process, or protocol failures."""


class WorkerToolchainChangedError(TypeScriptWorkerError):
    """The pinned project-local TypeScript toolchain changed mid-operation."""

    code = "JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.code}: {message}")


class WorkerProtocolError(TypeScriptWorkerError):
    """The worker emitted malformed or mismatched protocol data."""


class WorkerTimeoutError(TypeScriptWorkerError):
    """A worker request exceeded its deadline."""


class WorkerCrashedError(TypeScriptWorkerError):
    """The worker exited before completing a request."""


class WorkerOutOfMemoryError(WorkerCrashedError):
    """The Node analyzer exhausted its configured heap."""


class WorkerRemoteError(TypeScriptWorkerError):
    """A well-formed worker response reported an operation failure."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        retryable: bool,
        diagnostics: tuple[ProtocolDiagnostic, ...],
    ) -> None:
        super().__init__(f"TypeScript worker {code}: {message}")
        self.code = code
        self.retryable = retryable
        self.diagnostics = diagnostics


def validate_worker_capabilities(initialized: InitializeResult) -> None:
    """Reject a partial same-protocol worker during the handshake."""

    missing = sorted(set(REQUIRED_WORKER_CAPABILITIES) - set(initialized.capabilities))
    if missing:
        raise WorkerProtocolError(
            "TypeScript worker is missing required capabilities: "
            + ", ".join(missing)
            + ". Reinstall or upgrade the project-local @usejaunt/ts package."
        )


@dataclass(frozen=True, slots=True)
class WorkerInstallation:
    node: str
    worker_entry: Path
    compiler_module_path: Path
    package_root: Path
    tool_owner: Path
    package_managed: bool = False


@dataclass(frozen=True, slots=True)
class _PackageResolutionPin:
    start: Path
    boundary: Path | None
    package: str
    module_path: bool
    expected_name: str | None
    resolved_root: Path
    session_identity: str


@dataclass(frozen=True, slots=True)
class _AbsentPackageResolutionPin:
    start: Path
    boundary: Path | None
    package: str
    module_path: bool


@dataclass(frozen=True, slots=True)
class _RuntimePackageDependencyEdge:
    """One package-resolution edge discovered from a runtime package."""

    key: str
    importer: Path
    package: str
    required: bool


@dataclass(frozen=True, slots=True)
class _RuntimePackageResolutionEdge:
    """One resolved (or deliberately absent optional) runtime edge."""

    label: str
    importer: Path
    package: str
    required: bool
    resolved_root: Path | None


_RUNTIME_MANIFEST_FIELDS = (
    "name",
    "version",
    "type",
    "exports",
    "imports",
    "main",
    "module",
    "types",
    "typings",
    "typesVersions",
)
_RUNTIME_JAVASCRIPT_SUFFIXES = frozenset(
    {".js", ".jsx", ".cjs", ".mjs", ".ts", ".tsx", ".cts", ".mts"}
)
_RUNTIME_DECLARATION_SUFFIXES = (".d.ts", ".d.tsx", ".d.cts", ".d.mts")
_NODE_BUILTIN_PACKAGES = frozenset(
    {
        "assert",
        "async_hooks",
        "buffer",
        "child_process",
        "cluster",
        "console",
        "constants",
        "crypto",
        "dgram",
        "diagnostics_channel",
        "dns",
        "domain",
        "events",
        "fs",
        "http",
        "http2",
        "https",
        "module",
        "net",
        "os",
        "path",
        "perf_hooks",
        "process",
        "punycode",
        "querystring",
        "readline",
        "repl",
        "stream",
        "string_decoder",
        "sys",
        "timers",
        "tls",
        "trace_events",
        "tty",
        "url",
        "util",
        "v8",
        "vm",
        "wasi",
        "worker_threads",
        "zlib",
    }
)
_CONTROL_FLOW_PAREN_HEADS = frozenset({"catch", "for", "if", "switch", "while", "with"})
_DECLARATION_BODY_HEADS = frozenset(
    {"class", "enum", "function", "global", "interface", "module", "namespace", "type"}
)
_DECLARATION_PREFIXES = frozenset({"abstract", "async", "const", "declare", "default", "export"})
_TYPED_BINDING_PREFIXES = frozenset(
    {
        "abstract",
        "accessor",
        "const",
        "declare",
        "let",
        "override",
        "private",
        "protected",
        "public",
        "readonly",
        "static",
        "using",
        "var",
    }
)
_LABEL_CONTEXTUAL_IDENTIFIERS = frozenset(
    {
        "abstract",
        "async",
        "await",
        "declare",
        "implements",
        "interface",
        "module",
        "namespace",
        "of",
        "type",
    }
)
_JAVASCRIPT_LINE_TERMINATORS = frozenset({"\n", "\r", "\u2028", "\u2029"})
_RUNTIME_VALUE_TOKEN_KINDS = frozenset(
    {"computed-template", "jsx", "number", "regex", "string", "template"}
)


def _line_comment_end(source: str, start: int) -> int:
    """Return the next ECMAScript line terminator or ``len(source)``."""

    index = start
    while index < len(source) and source[index] not in _JAVASCRIPT_LINE_TERMINATORS:
        index += 1
    return index


class _RuntimeJavaScriptTokens(tuple[tuple[str, str], ...]):
    """Executable tokens plus the source line-break boundary before each token."""

    class_body_contexts: tuple[bool, ...]
    line_breaks_before: tuple[bool, ...]
    type_body_contexts: tuple[bool, ...]

    def __new__(
        cls,
        values: Sequence[tuple[str, str]],
        line_breaks_before: Sequence[bool],
        type_body_contexts: Sequence[bool],
        class_body_contexts: Sequence[bool],
    ) -> _RuntimeJavaScriptTokens:
        instance = super().__new__(cls, values)
        boundaries = tuple(line_breaks_before)
        type_contexts = tuple(type_body_contexts)
        class_contexts = tuple(class_body_contexts)
        if not all(
            len(instance) == len(metadata)
            for metadata in (boundaries, type_contexts, class_contexts)
        ):
            raise ValueError("Runtime token metadata is misaligned")
        instance.line_breaks_before = boundaries
        instance.type_body_contexts = type_contexts
        instance.class_body_contexts = class_contexts
        return instance


@dataclass(frozen=True)
class _RuntimeTokenEvent:
    """One executable token recorded before the final stream is flattened."""

    kind: str
    value: str
    line_break_before: bool


@dataclass(frozen=True)
class _RuntimeExpressionGroup:
    """A structured executable expression whose nested literals remain ropes."""

    events: tuple[_RuntimeTokenEvent | _RuntimeGroupedValueEvent, ...]


@dataclass(frozen=True)
class _RuntimeGroupedValueEvent:
    """Nested template/JSX expressions followed by their completed value."""

    groups: tuple[_RuntimeExpressionGroup, ...]
    kind: str
    value: str
    line_break_before: bool


def _type_only_import_assignment_require(
    tokens: Sequence[tuple[str, str]],
    require_index: int,
) -> bool:
    """Recognize the erased ``import type Name = require(...)`` form."""

    # Comments and whitespace are absent from the executable token stream, so
    # the external-module-reference grammar has a fixed local shape. Keeping
    # this check local avoids both crossing an ASI boundary into an earlier
    # ``import type`` and rescanning every preceding statement in semicolonless
    # CommonJS bundles.
    return (
        require_index >= 4
        and tokens[require_index - 4] == ("identifier", "import")
        and tokens[require_index - 3] == ("identifier", "type")
        and tokens[require_index - 2][0] == "identifier"
        and tokens[require_index - 1] == ("punctuation", "=")
    )


def _control_flow_parenthesis_head(
    tokens: Sequence[tuple[str, str]],
    *,
    end: int | None = None,
) -> str | None:
    """Return the statement-leading control keyword before the next ``(``."""

    token_count = len(tokens) if end is None else end
    if token_count <= 0:
        return None
    head = token_count - 1
    if tokens[head] == ("identifier", "await") and head > 0:
        head -= 1
    if tokens[head][0] != "identifier" or tokens[head][1] not in _CONTROL_FLOW_PAREN_HEADS:
        return None
    if head > 0 and tokens[head - 1][1] in {".", "?."}:
        return None
    return tokens[head][1]


def _opens_control_flow_parenthesis(
    tokens: Sequence[tuple[str, str]],
    *,
    end: int | None = None,
) -> bool:
    """Return whether the next ``(`` opens a statement-leading control head."""

    return _control_flow_parenthesis_head(tokens, end=end) is not None


def _closes_control_flow_parenthesis(tokens: Sequence[tuple[str, str]], close_index: int) -> bool:
    """Return whether one recorded ``)`` closes a control-flow head."""

    if close_index < 0 or tokens[close_index][1] != ")":
        return False
    depth = 1
    for index in range(close_index - 1, -1, -1):
        value = tokens[index][1]
        if value == ")":
            depth += 1
        elif value == "(":
            depth -= 1
            if depth == 0:
                return _opens_control_flow_parenthesis(tokens, end=index)
    return False


def _colon_opens_statement_block(
    tokens: Sequence[tuple[str, str]],
    *,
    enclosing_statement_brace: bool | None,
    end: int | None = None,
    line_breaks_before: Sequence[bool] | None = None,
) -> bool:
    """Recognize a label or completed switch clause before a block."""

    token_count = len(tokens) if end is None else end
    if token_count <= 0 or tokens[token_count - 1][1] != ":":
        return False
    if token_count >= 2 and tokens[token_count - 2][0] == "identifier":
        label_index = token_count - 2
        prefix = token_count - 3
        while prefix >= 1 and tokens[prefix][1] == ":" and tokens[prefix - 1][0] == "identifier":
            label_index = prefix - 1
            prefix -= 2
        if (
            prefix < 0
            or tokens[prefix][1] in {";", "}"}
            or (tokens[prefix][1] == "{" and enclosing_statement_brace is True)
            or (tokens[prefix][0] == "identifier" and tokens[prefix][1] in {"do", "else"})
            or (tokens[prefix][1] == ")" and _closes_control_flow_parenthesis(tokens, prefix))
            or (
                line_breaks_before is not None
                and len(line_breaks_before) >= token_count
                and line_breaks_before[label_index]
                and label_index > 0
                and _can_end_statement_before_label(*tokens[label_index - 1])
            )
        ):
            return True
    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{"}
    clause_index: int | None = None
    for index in range(token_count - 2, -1, -1):
        kind, value = tokens[index]
        if value in opening_for:
            expected_openings.append(opening_for[value])
            continue
        if expected_openings:
            if value == expected_openings[-1]:
                expected_openings.pop()
            continue
        if value in {";", "{", "}"}:
            break
        if kind == "identifier" and value in {"case", "default"}:
            clause_index = index
            break
    if clause_index is None:
        return False
    conditional_depth = 0
    expected_closings: list[str] = []
    closing_for = {"(": ")", "[": "]", "{": "}"}
    for index in range(clause_index + 1, token_count - 1):
        _kind, value = tokens[index]
        if value in closing_for:
            expected_closings.append(closing_for[value])
        elif expected_closings:
            if value == expected_closings[-1]:
                expected_closings.pop()
        elif value == "?":
            conditional_depth += 1
        elif value == ":" and conditional_depth:
            conditional_depth -= 1
    return conditional_depth == 0


def _can_end_statement_before_block(kind: str, value: str) -> bool:
    """Return whether ASI may put a standalone block after this token."""

    if kind in _RUNTIME_VALUE_TOKEN_KINDS:
        return True
    if value in {")", "]", "}", "++", "--"}:
        return True
    if kind != "identifier":
        return False
    return value not in {
        "abstract",
        "await",
        "case",
        "class",
        "const",
        "declare",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "function",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "let",
        "module",
        "namespace",
        "new",
        "of",
        "throw",
        "type",
        "typeof",
        "var",
        "void",
    }


def _can_end_statement_before_label(kind: str, value: str) -> bool:
    """Return whether a line break can make the next identifier a label."""

    return (
        kind == "identifier" and value in _LABEL_CONTEXTUAL_IDENTIFIERS
    ) or _can_end_statement_before_block(kind, value)


def _can_end_statement_before_declaration(
    tokens: Sequence[tuple[str, str]],
    index: int,
) -> bool:
    """Recognize expression terminals before an ASI-separated declaration."""

    kind, value = tokens[index]
    if _can_end_statement_before_block(kind, value):
        return True
    if value == "!" and index > 0:
        # TypeScript non-null assertions end an expression, while a leading
        # logical-not remains a unary continuation.
        return _can_end_statement_before_declaration(tokens, index - 1)
    if value == "const" and index > 0 and tokens[index - 1] == ("identifier", "as"):
        return True
    if value == ">":
        opening = _decorator_group_open(tokens, index)
        return (
            opening is not None
            and opening > 0
            and _can_end_statement_before_declaration(tokens, opening - 1)
        )
    return False


class _ConditionalExpressionTracker:
    """Track delimiter-local ternary expressions as tokens arrive."""

    def __init__(self) -> None:
        self._expected_closings: list[str] = []
        self._question_counts = [0]
        self._angle_depths = [0]
        self._previous: tuple[str, str] | None = None

    def append(self, kind: str, value: str) -> bool:
        """Record one token and return whether it is a ternary colon."""

        closing_for = {"(": ")", "[": "]", "{": "}"}
        is_conditional_colon = False
        if kind != "punctuation":
            pass
        elif value in closing_for:
            self._expected_closings.append(closing_for[value])
            self._question_counts.append(0)
            self._angle_depths.append(0)
        elif value in {")", "]", "}"}:
            if self._expected_closings and value == self._expected_closings[-1]:
                self._expected_closings.pop()
                self._question_counts.pop()
                self._angle_depths.pop()
            else:
                self._question_counts[-1] = 0
                self._angle_depths[-1] = 0
        elif value == "<" and self._question_counts[-1]:
            self._angle_depths[-1] += 1
        elif value == ">" and self._angle_depths[-1]:
            self._angle_depths[-1] -= 1
        elif value == "?":
            self._question_counts[-1] += 1
        elif value == ":" and self._question_counts[-1]:
            # An adjacent ``?:`` is a TypeScript optional marker rather than
            # a conditional with an empty consequent.
            if self._previous == ("punctuation", "?"):
                self._question_counts[-1] -= 1
            else:
                self._question_counts[-1] -= 1
                is_conditional_colon = True
            if not self._question_counts[-1]:
                self._angle_depths[-1] = 0
        elif value == "," and not self._angle_depths[-1]:
            self._question_counts[-1] = 0
        elif value == ";":
            self._question_counts[-1] = 0
            self._angle_depths[-1] = 0
        self._previous = (kind, value)
        return is_conditional_colon


class _SwitchClauseTracker:
    """Track case/default colons without rescanning their expressions."""

    def __init__(self) -> None:
        self._expected_closings: list[str] = []
        self._switch_body_depths: list[int | None] = [None]
        self._pending_clause_depths: list[int | None] = [None]
        self._previous: tuple[str, str] | None = None

    def append(
        self,
        kind: str,
        value: str,
        *,
        opens_switch_body: bool = False,
        conditional_colon: bool = False,
    ) -> bool:
        """Record one token and return whether it closes a switch clause."""

        is_clause_colon = False
        depth = len(self._expected_closings)
        if (
            kind == "identifier"
            and value in {"case", "default"}
            and self._switch_body_depths[-1] == depth
            and (self._previous is None or self._previous[1] not in {".", "?."})
        ):
            self._pending_clause_depths[-1] = depth
        elif kind == "punctuation" and value == ":":
            if self._pending_clause_depths[-1] == depth and not conditional_colon:
                self._pending_clause_depths[-1] = None
                is_clause_colon = True
        elif kind == "punctuation" and value == ";":
            if self._pending_clause_depths[-1] == depth:
                self._pending_clause_depths[-1] = None

        closing_for = {"(": ")", "[": "]", "{": "}"}
        if kind == "punctuation" and value in closing_for:
            self._expected_closings.append(closing_for[value])
            if value == "{":
                self._switch_body_depths.append(depth + 1 if opens_switch_body else None)
                self._pending_clause_depths.append(None)
        elif kind == "punctuation" and value in {")", "]", "}"}:
            if self._expected_closings and value == self._expected_closings[-1]:
                self._expected_closings.pop()
            if value == "}" and len(self._switch_body_depths) > 1:
                self._switch_body_depths.pop()
                self._pending_clause_depths.pop()
        self._previous = (kind, value)
        return is_clause_colon


def _conditional_expression_colons(
    tokens: Sequence[tuple[str, str]],
) -> frozenset[int]:
    """Find ternary colons in one linear pass over executable tokens."""

    tracker = _ConditionalExpressionTracker()
    return frozenset(
        index for index, (kind, value) in enumerate(tokens) if tracker.append(kind, value)
    )


def _annotation_initializer_starts(
    tokens: Sequence[tuple[str, str]],
) -> list[int]:
    """Precompute the existing tolerant type-suffix scan from every token."""

    closing_for = {"(": ")", "[": "]", "{": "}", "<": ">"}
    expected: list[tuple[int, str]] = []
    starts = [-1] * (len(tokens) + 1)
    for index, (_kind, value) in enumerate(tokens):
        if value in closing_for:
            expected.append((index, closing_for[value]))
        elif expected and value == expected[-1][1]:
            opening, _closing = expected.pop()
            starts[opening] = index

    stops = {";", ",", ")", "]", "}"}
    for index in range(len(tokens) - 1, -1, -1):
        value = tokens[index][1]
        if value in closing_for:
            close = starts[index]
            starts[index] = -1 if close < 0 else starts[close + 1]
        elif value == "=":
            starts[index] = index + 1
        elif value in stops:
            starts[index] = -1
        else:
            starts[index] = starts[index + 1]
    return starts


def _colon_follows_comma_at_current_depth(tokens: Sequence[tuple[str, str]]) -> bool:
    """Return whether the current colon's key started after a peer comma."""

    if not tokens or tokens[-1][1] != ":":
        return False
    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index in range(len(tokens) - 2, -1, -1):
        value = tokens[index][1]
        if value in opening_for:
            expected_openings.append(opening_for[value])
            continue
        if expected_openings:
            if value == expected_openings[-1]:
                expected_openings.pop()
            continue
        if value == ",":
            return True
        if value in {":", ";", "{", "}"}:
            return False
    return False


def _decorator_group_open(
    tokens: Sequence[tuple[str, str]],
    close_index: int,
    *,
    minimum_index: int = 0,
) -> int | None:
    """Return the opener for one balanced decorator suffix group."""

    structural_opening_for = {")": "(", "]": "[", "}": "{"}
    close_kind, close = tokens[close_index]
    if close_kind != "punctuation":
        return None
    if close not in {*structural_opening_for, ">"}:
        return None
    expected = ["<" if close == ">" else structural_opening_for[close]]
    for index in range(close_index - 1, minimum_index - 1, -1):
        kind, value = tokens[index]
        if kind != "punctuation":
            continue
        if value in structural_opening_for:
            expected.append(structural_opening_for[value])
        elif expected[-1] == "<" and value == ">":
            expected.append("<")
        elif value == expected[-1]:
            expected.pop()
            if not expected:
                return index
        elif value in {"(", "[", "{"}:
            return None
    return None


def _decorator_prefix_before(
    tokens: Sequence[tuple[str, str]],
    end: int,
    *,
    at_index: int | None = None,
) -> int | None:
    """Consume one decorator expression and return the token before its ``@``."""

    cursor = end
    consumed_primary = False
    minimum_index = 0 if at_index is None else at_index + 1
    while cursor >= 0:
        while (
            cursor >= 0
            and tokens[cursor][0] == "punctuation"
            and tokens[cursor][1] in {")", "]", "}", ">"}
        ):
            close = tokens[cursor][1]
            opening = _decorator_group_open(
                tokens,
                cursor,
                minimum_index=minimum_index,
            )
            if opening is None:
                return None
            if close in {"]", "}"} or (close == ")" and opening + 1 < cursor):
                consumed_primary = True
            cursor = opening - 1
        before_non_null = cursor
        while cursor >= 0 and tokens[cursor] == ("punctuation", "!"):
            cursor -= 1
        if cursor != before_non_null:
            continue
        if not consumed_primary and cursor >= 0 and tokens[cursor] == ("punctuation", "?."):
            cursor -= 1
            continue
        if cursor >= 0 and tokens[cursor][0] == "identifier":
            cursor -= 1
            consumed_primary = True
        elif not consumed_primary:
            return None
        if (
            cursor >= 0
            and tokens[cursor][0] == "punctuation"
            and tokens[cursor][1]
            in {
                ".",
                "?.",
            }
        ):
            cursor -= 1
            consumed_primary = False
            continue
        break
    if (
        consumed_primary
        and cursor >= 0
        and tokens[cursor] == ("punctuation", "@")
        and (at_index is None or cursor == at_index)
    ):
        return cursor - 1
    return None


def _declaration_prefix_before(
    tokens: Sequence[tuple[str, str]],
    head_index: int,
    *,
    decorator_at_indices: Sequence[int] | None,
) -> tuple[int, int, bool]:
    """Return the token before a declaration, its start, and decorator use."""

    declaration_start = head_index
    prefix = head_index - 1
    consumed_decorator = False
    while True:
        decorator_prefix: int | None = None
        if decorator_at_indices is None:
            decorator_prefix = _decorator_prefix_before(tokens, prefix)
        else:
            decorator_position = bisect_right(decorator_at_indices, prefix) - 1
            if decorator_position >= 0:
                decorator_prefix = _decorator_prefix_before(
                    tokens,
                    prefix,
                    at_index=decorator_at_indices[decorator_position],
                )
        if decorator_prefix is not None:
            consumed_decorator = True
            declaration_start = decorator_prefix + 1
            prefix = decorator_prefix
            continue
        if (
            prefix >= 0
            and tokens[prefix][0] == "identifier"
            and tokens[prefix][1] in _DECLARATION_PREFIXES
            and (tokens[prefix][1] != "const" or tokens[head_index][1] == "enum")
        ):
            declaration_start = prefix
            prefix -= 1
            continue
        break
    return prefix, declaration_start, consumed_decorator


class _DecoratorCandidateTracker:
    """Track decorator starts in the current balanced delimiter frame."""

    def __init__(self) -> None:
        self._frames: list[tuple[str | None, list[int]]] = [(None, [])]
        self._decorated_declaration_starts: dict[int, int] = {}

    @property
    def current(self) -> Sequence[int]:
        """Return decorator candidates visible before the next token."""

        return self._frames[-1][1]

    def consume_current(self) -> None:
        """Forget decorators consumed by the declaration now opening its body."""

        self._frames[-1][1].clear()

    def declaration_start(self, head_index: int) -> int | None:
        """Return the recorded start of one decorated declaration head."""

        return self._decorated_declaration_starts.get(head_index)

    def consume_declaration(self, head_index: int) -> None:
        """Forget a decorated declaration once its body brace is reached."""

        self._decorated_declaration_starts.pop(head_index, None)

    def append(
        self,
        tokens: Sequence[tuple[str, str]],
        index: int,
        kind: str,
        value: str,
    ) -> None:
        """Record one token without retaining candidates from finished declarations."""

        if (
            kind == "identifier"
            and value == "class"
            and self.current
            and (index == 0 or tokens[index - 1][1] not in {".", "?.", "@"})
        ):
            _prefix, declaration_start, consumed_decorator = _declaration_prefix_before(
                tokens,
                index,
                decorator_at_indices=self.current,
            )
            if consumed_decorator:
                self._decorated_declaration_starts[index] = declaration_start
            # A same-frame ``@`` that is not this declaration's prefix belongs
            # to an earlier decorated member or inert JSX text. Retire it on
            # the first declaration head so later class expressions cannot
            # rescan the same prefix quadratically.
            self.consume_current()
            return
        if kind != "punctuation":
            return
        if value == "@":
            self._frames[-1][1].append(index)
            return
        closing_for = {"(": ")", "[": "]", "{": "}"}
        if value in closing_for:
            self._frames.append((closing_for[value], []))
            return
        if value in {")", "]", "}"}:
            if len(self._frames) > 1 and self._frames[-1][0] == value:
                self._frames.pop()
            return
        if value == ";":
            self._frames[-1][1].clear()


def _declaration_head_can_open_body(
    tokens: Sequence[tuple[str, str]],
    head_index: int,
) -> bool:
    """Reject contextual head words used as names, types, or assignment targets."""

    _kind, head = tokens[head_index]
    if head_index > 0 and tokens[head_index - 1][1] in {".", "?."}:
        return False
    tail = head_index + 1
    if head == "global":
        return head_index > 0 and tokens[head_index - 1] == ("identifier", "declare")
    if head == "class":
        return tail >= len(tokens) or tokens[tail][1] not in {"=", ":"}
    if head == "function":
        depth = 0
        for index in range(tail, len(tokens)):
            _kind, value = tokens[index]
            if value in {"[", "{", "<"}:
                depth += 1
            elif value in {"]", "}", ">"} and depth:
                depth -= 1
            elif value == "(" and depth == 0:
                return True
        return False
    if tail >= len(tokens):
        return False
    if head == "module" and tokens[tail][0] == "string":
        return True
    return tokens[tail][0] == "identifier"


def _brace_lexical_scope_kind(
    tokens: Sequence[tuple[str, str]],
    *,
    opens_statement: bool,
    enclosing_statement_brace: bool | None,
) -> str | None:
    """Classify a brace-backed lexical scope.

    The distinction between ordinary blocks and function/static scopes matters
    for ``var``: a declaration in a nested statement block belongs to the
    nearest function or static block, while ``let`` and ``const`` remain local
    to the brace.
    """

    if not tokens:
        return "block" if opens_statement else None
    if tokens[-1] == ("punctuation", "=>"):
        return "function"
    if tokens[-1] == ("identifier", "static") and enclosing_statement_brace is True:
        return "static"
    # A colon-introduced object/type literal may follow a method parameter
    # list, but its brace is not the method implementation body.
    if tokens[-1][1] == ":":
        return "block"
    if opens_statement:
        # Type/object literals commonly follow a colon. They are not runtime
        # declaration scopes, and avoiding a declaration-prefix scan here is
        # essential for long interfaces containing many object-valued members.
        declaration_head = _declaration_body_head(tokens)
        if declaration_head == "class":
            return "class"
        if declaration_head == "function":
            return "function"
        if declaration_head in {"module", "namespace"}:
            return "namespace"
        return "block"
    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{", ">": "<"}
    for index in range(len(tokens) - 1, -1, -1):
        value = tokens[index][1]
        if value == ")" and not expected_openings:
            return "function"
        if value in opening_for:
            expected_openings.append(opening_for[value])
            continue
        if expected_openings:
            if value == expected_openings[-1]:
                expected_openings.pop()
            continue
        if value in {";", "{", "}", "=", ","}:
            return None
    return None


def _brace_opens_lexical_scope(
    tokens: Sequence[tuple[str, str]],
    *,
    opens_statement: bool,
    enclosing_statement_brace: bool | None,
) -> bool:
    """Recognize function, method, arrow, block, and static lexical scopes."""

    return (
        _brace_lexical_scope_kind(
            tokens,
            opens_statement=opens_statement,
            enclosing_statement_brace=enclosing_statement_brace,
        )
        is not None
    )


def _function_scope_start(
    tokens: Sequence[tuple[str, str]],
    paren_open_for_close: Mapping[int, int],
) -> int | None:
    """Return the parameter-list boundary owned by an upcoming function body."""

    arrow = bool(tokens and tokens[-1] == ("punctuation", "=>"))
    cursor = len(tokens) - (2 if arrow else 1)
    saw_arrow_type_boundary = False
    while cursor >= 0:
        kind, value = tokens[cursor]
        if value == ")":
            opening = paren_open_for_close.get(cursor)
            if opening is None:
                cursor -= 1
                continue
            previous = tokens[opening - 1][1] if opening else ""
            # Parenthesized return/function types can occur between the real
            # parameter list and the body arrow. They are preceded by a type
            # operator rather than a declaration/expression boundary.
            if previous not in {":", "<", "[", "|", "&", "=>"}:
                return opening
            cursor = opening - 1
            continue
        if arrow and value == ":":
            saw_arrow_type_boundary = True
            cursor -= 1
            continue
        if (
            arrow
            and kind == "identifier"
            and (
                saw_arrow_type_boundary
                or (cursor == len(tokens) - 2 and (cursor == 0 or tokens[cursor - 1][1] != ":"))
            )
        ):
            # A single unparenthesized arrow parameter owns the body scope.
            return max(-1, cursor - 1)
        if value in {";", "{"}:
            break
        cursor -= 1
    return None


def _declaration_body_head(tokens: Sequence[tuple[str, str]]) -> str | None:
    """Return the actual declaration head whose body brace follows."""

    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{"}
    for index in range(len(tokens) - 1, -1, -1):
        kind, value = tokens[index]
        if value == ">" and (not expected_openings or expected_openings[-1] == "<"):
            expected_openings.append("<")
            continue
        if value in opening_for:
            if value == "}" and not expected_openings:
                break
            expected_openings.append(opening_for[value])
            continue
        if expected_openings:
            if value == expected_openings[-1]:
                expected_openings.pop()
            continue
        if value in {";", "{"}:
            break
        if (
            kind == "identifier"
            and value in _DECLARATION_BODY_HEADS
            and _declaration_head_can_open_body(tokens, index)
        ):
            return value
    return None


def _opens_type_body_brace(
    tokens: Sequence[tuple[str, str]],
    *,
    declaration_head: str | None,
    enclosing_type_body: bool,
    enclosing_statement_brace: bool | None,
    enclosing_class_body: bool,
    opens_statement_brace: bool,
    nested_in_parentheses: bool,
    previous_colon_is_conditional: bool,
    previous_colon_is_switch_clause: bool,
) -> bool:
    """Recognize object-type literals whose members can start with ``<T>``.

    TSX otherwise makes a generic call signature look exactly like a JSX
    element. Declaration bodies and nested object types are unambiguous. For
    standalone object types, only accept braces in a known type-introducing
    position; notably, a colon in an ordinary object literal still introduces
    an executable property value.
    """

    if declaration_head in {"interface", "type"} or enclosing_type_body:
        return True
    if not tokens:
        return False
    kind, value = tokens[-1]
    if (
        kind == "identifier"
        and value in {"as", "satisfies"}
        and (len(tokens) < 2 or tokens[-2][1] not in {".", "?."})
    ):
        return True
    if value != ":" or previous_colon_is_conditional or previous_colon_is_switch_clause:
        return False
    if enclosing_class_body or nested_in_parentheses:
        return True
    if len(tokens) >= 2 and tokens[-2][1] == ")":
        # A closed parameter list followed by a colon introduces a return
        # type, including an arrow method used as an object property value.
        return True
    if enclosing_statement_brace is False:
        return False
    return not opens_statement_brace


def _opens_statement_brace(
    tokens: Sequence[tuple[str, str]],
    *,
    enclosing_statement_brace: bool | None,
    previous_closed_control_head: bool,
    previous_closed_statement_brace: bool,
    previous_colon_is_conditional: bool = False,
    previous_colon_is_switch_clause: bool = False,
    line_break_before: bool = False,
    line_breaks_before: Sequence[bool] | None = None,
    decorator_candidates: _DecoratorCandidateTracker | None = None,
) -> bool:
    """Return whether the next brace begins a statement/declaration body."""

    if not tokens:
        return True
    kind, value = tokens[-1]
    if value == "=>":
        return True
    if value == ")" and previous_closed_control_head:
        return True
    if kind == "identifier" and value in {"catch", "do", "else", "finally", "try"}:
        return True
    if value == ";" or (value == "}" and previous_closed_statement_brace):
        return True
    if value in {"(", "[", ","}:
        # A brace immediately inside an expression/group cannot open a
        # statement body. Avoid walking the complete preceding expression for
        # every object literal in a large bundled source.
        return False
    # Two adjacent opening braces cannot form an object/property expression;
    # the inner brace is a nested statement block (including inside an arrow
    # or function expression body, whose outer close remains expression-like).
    if value == "{":
        return True
    if value == ":" and previous_colon_is_conditional:
        return False
    if value == ":" and previous_colon_is_switch_clause:
        return True
    if value == ":" and len(tokens) >= 2 and tokens[-2][0] != "identifier":
        # Labels always end in an identifier, while switch clauses are
        # tracked above. Return-type, computed-key, string-key, and numeric-key
        # colons therefore introduce expression/type braces without a scan.
        return False
    if (
        value == ":"
        and line_breaks_before is not None
        and len(line_breaks_before) >= len(tokens)
        and len(tokens) >= 3
        and line_breaks_before[-2]
        and _can_end_statement_before_label(*tokens[-3])
    ):
        return True
    if value == ":" and _colon_follows_comma_at_current_depth(tokens):
        return False
    if value == ":" and _colon_opens_statement_block(
        tokens,
        enclosing_statement_brace=enclosing_statement_brace,
        line_breaks_before=line_breaks_before,
    ):
        return True
    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{"}
    for index in range(len(tokens) - 1, -1, -1):
        token_kind, token_value = tokens[index]
        if token_value == ">" and (not expected_openings or expected_openings[-1] == "<"):
            expected_openings.append("<")
            continue
        if token_value in opening_for:
            if token_value == "}" and not expected_openings:
                break
            expected_openings.append(opening_for[token_value])
            continue
        if expected_openings:
            if token_value == expected_openings[-1]:
                expected_openings.pop()
            continue
        if token_value in {";", "{"}:
            break
        if token_kind != "identifier" or token_value not in _DECLARATION_BODY_HEADS:
            continue
        if not _declaration_head_can_open_body(tokens, index):
            continue
        decorated_start = (
            None if decorator_candidates is None else decorator_candidates.declaration_start(index)
        )
        if decorated_start is None:
            prefix, declaration_start, _consumed_decorator = _declaration_prefix_before(
                tokens,
                index,
                decorator_at_indices=None if decorator_candidates is None else (),
            )
        else:
            declaration_start = decorated_start
            prefix = decorated_start - 1
        is_statement = False
        if (
            prefix >= 0
            and tokens[prefix][1] == ")"
            and _closes_control_flow_parenthesis(tokens, prefix)
        ):
            is_statement = True
        elif (
            prefix >= 0
            and tokens[prefix][1] == ":"
            and _colon_opens_statement_block(
                tokens,
                enclosing_statement_brace=enclosing_statement_brace,
                end=prefix + 1,
                line_breaks_before=line_breaks_before,
            )
        ):
            is_statement = True
        elif (
            prefix >= 0
            and line_breaks_before is not None
            and declaration_start < len(line_breaks_before)
            and line_breaks_before[declaration_start]
            and _can_end_statement_before_declaration(tokens, prefix)
        ):
            is_statement = True
        else:
            is_statement = (
                prefix < 0
                or tokens[prefix][1] in {";", "}"}
                or (tokens[prefix][1] == "{" and enclosing_statement_brace is True)
                or (tokens[prefix][0] == "identifier" and tokens[prefix][1] in {"do", "else"})
            )
        if (
            not is_statement
            and prefix >= 0
            and tokens[prefix][0] == "identifier"
            and tokens[prefix][1] in _DECLARATION_BODY_HEADS
        ):
            # Contextual declaration keywords remain legal declaration names,
            # e.g. ``class interface {}`` and ``function namespace() {}``.
            # Keep walking to the actual head instead of treating the name as
            # a failed expression-level declaration.
            continue
        if decorated_start is not None and decorator_candidates is not None:
            decorator_candidates.consume_declaration(index)
        return is_statement
    if line_break_before and _can_end_statement_before_block(kind, value):
        return True
    return False


def _ordered_json_identity(value: object) -> object:
    """Encode parsed JSON without losing semantically observable mapping order."""

    if isinstance(value, Mapping):
        return [[str(key), _ordered_json_identity(item)] for key, item in value.items()]
    if isinstance(value, list):
        return [_ordered_json_identity(item) for item in value]
    return value


def _runtime_manifest_identity(manifest: Mapping[str, Any]) -> list[list[object]]:
    """Keep resolution semantics while ignoring unrelated manifest formatting/data."""

    return [
        [field, _ordered_json_identity(manifest[field])]
        for field in _RUNTIME_MANIFEST_FIELDS
        if field in manifest
    ]


def _runtime_package_identity_files(package_root: Path) -> tuple[Path, ...]:
    """Enumerate every shipped file in one package, excluding dependency trees.

    JavaScript packages can execute more than source-shaped files: native
    ``.node`` addons, WASM payloads, and extensionless binaries are all common
    runtime inputs.  Keep the boundary at the resolved package itself and let
    the recursive dependency pin account for each separately resolved package.
    """

    try:
        physical_root = package_root.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {package_root}: {exc}"
        ) from exc
    if not physical_root.is_dir():
        raise TypeScriptWorkerError(f"Runtime package is not a directory: {package_root}")
    paths: set[Path] = set()
    manifest_path = physical_root / "package.json"

    def raise_walk_error(error: OSError) -> None:
        raise error

    try:
        for current, directories, filenames in os.walk(
            physical_root,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            # Do not even traverse nested dependency stores. Each dependency
            # selected by Node resolution receives its own package pin.
            directories[:] = [name for name in directories if name != "node_modules"]
            current_path = Path(current)
            for filename in filenames:
                path = current_path / filename
                if path == manifest_path or not path.is_file():
                    continue
                paths.add(path.resolve(strict=True))
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not enumerate runtime package files under {package_root}: {exc}"
        ) from exc
    if any(path != physical_root and physical_root not in path.parents for path in paths):
        raise TypeScriptWorkerError(f"Runtime package file escapes its package: {package_root}")
    return tuple(sorted(paths, key=lambda path: path.relative_to(physical_root).as_posix()))


def _runtime_package_symlink_topology(
    package_root: Path,
) -> tuple[tuple[str, str, str], ...]:
    """Return path-portable topology for symlinks owned by one package.

    Runtime package identities deliberately omit absolute installation paths,
    but a package-internal symlink is observable by Node and must not collapse
    to its target bytes.  Record the lexical package-relative entry, the exact
    link text, and whether the resolved target is a file or directory.  Broken,
    escaping, and special-file targets fail closed.
    """

    try:
        physical_root = package_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {package_root}: {exc}"
        ) from exc
    if not physical_root.is_dir():
        raise TypeScriptWorkerError(f"Runtime package is not a directory: {package_root}")

    def entry_epoch(value: os.stat_result) -> tuple[int, ...]:
        return (
            value.st_dev,
            value.st_ino,
            value.st_mode,
            value.st_size,
            value.st_mtime_ns,
            value.st_ctime_ns,
        )

    def inspect(path: Path) -> tuple[str, str, str]:
        relative = path.relative_to(physical_root).as_posix()
        try:
            before = path.lstat()
            if not stat.S_ISLNK(before.st_mode):
                raise TypeScriptWorkerError(
                    f"Runtime package tree changed while its symlink topology was read: {path}"
                )
            target_text = os.readlink(path)
            target = path.resolve(strict=True)
            if target != physical_root and physical_root not in target.parents:
                raise TypeScriptWorkerError(
                    f"Runtime package symlink escapes its package: {path} -> {target_text}"
                )
            target_stat = target.stat()
            if stat.S_ISREG(target_stat.st_mode):
                target_type = "file"
            elif stat.S_ISDIR(target_stat.st_mode):
                target_type = "directory"
            else:
                raise TypeScriptWorkerError(
                    "Runtime package symlink has an unsupported target type: "
                    f"{path} -> {target_text}"
                )
            after = path.lstat()
            after_target_text = os.readlink(path)
        except TypeScriptWorkerError:
            raise
        except (OSError, RuntimeError) as exc:
            raise TypeScriptWorkerError(
                f"Could not inspect runtime package symlink at {path}: {exc}"
            ) from exc
        if entry_epoch(before) != entry_epoch(after) or target_text != after_target_text:
            raise TypeScriptWorkerError(
                f"Runtime package symlink changed while its freshness identity was read: {path}"
            )
        return relative, target_text, target_type

    def raise_walk_error(error: OSError) -> None:
        raise error

    def is_symlink(path: Path) -> bool:
        try:
            return stat.S_ISLNK(path.lstat().st_mode)
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not inspect runtime package entry at {path}: {exc}"
            ) from exc

    links: list[tuple[str, str, str]] = []
    try:
        for current, directories, filenames in os.walk(
            physical_root,
            topdown=True,
            onerror=raise_walk_error,
            followlinks=False,
        ):
            # Nested dependencies have independent package pins and are outside
            # this package's identity boundary.
            entries = [name for name in (*directories, *filenames) if name != "node_modules"]
            current_path = Path(current)
            links.extend(
                inspect(current_path / name) for name in entries if is_symlink(current_path / name)
            )
            directories[:] = [name for name in directories if name != "node_modules"]
    except TypeScriptWorkerError:
        raise
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not enumerate runtime package symlinks under {package_root}: {exc}"
        ) from exc
    return tuple(sorted(links, key=lambda item: item[0]))


def runtime_package_identity(package_root: Path, *, expected_name: str | None = None) -> str:
    """Return a path-portable identity for one resolved JavaScript package.

    The package's own shipped files and Node resolution manifest fields are
    covered. Nested ``node_modules`` are intentionally not: each separately
    resolved tool is pinned at its actual owner boundary.
    """

    lexical_root = Path(os.path.abspath(package_root))
    try:
        physical_root = lexical_root.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {package_root}: {exc}"
        ) from exc
    manifest_path = physical_root / "package.json"
    manifest_bytes = _stable_bytes(manifest_path, label="runtime package.json")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TypeScriptWorkerError(
            f"Could not parse runtime package.json at {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise TypeScriptWorkerError(
            f"Invalid runtime package.json: expected an object at {manifest_path}"
        )
    if expected_name is not None and manifest.get("name") != expected_name:
        raise TypeScriptWorkerError(
            f"Resolved runtime package at {lexical_root} is not {expected_name!r}"
        )

    symlinks = _runtime_package_symlink_topology(physical_root)
    paths = _runtime_package_identity_files(physical_root)

    def file_digests(runtime_paths: tuple[Path, ...]) -> dict[str, str]:
        return {
            path.relative_to(physical_root).as_posix(): hashlib.sha256(
                _stable_bytes(path, label="runtime package file")
            ).hexdigest()
            for path in runtime_paths
        }

    files = file_digests(paths)
    after_paths = _runtime_package_identity_files(physical_root)
    after_files = file_digests(after_paths)
    after_manifest_bytes = _stable_bytes(manifest_path, label="runtime package.json")
    # Read topology last so a retarget during either validation pass cannot be
    # hidden by byte-identical targets.
    after_symlinks = _runtime_package_symlink_topology(physical_root)
    if (
        paths != after_paths
        or symlinks != after_symlinks
        or files != after_files
        or manifest_bytes != after_manifest_bytes
    ):
        raise TypeScriptWorkerError(
            f"Runtime package changed while its freshness identity was read: {lexical_root}"
        )
    payload = {
        "format": "javascript-runtime-package/4",
        # Package code and Vite resolvers may inspect fields beyond Node's
        # entry-point map (for example ``browser``, ``sideEffects``, or custom
        # plugin settings). Preserve the full parsed manifest, including object
        # order, while remaining insensitive to whitespace-only rewrites.
        "manifest": _ordered_json_identity(manifest),
        "manifestResolution": _runtime_manifest_identity(manifest),
        "files": files,
        "symlinks": [
            {"path": path, "target": target, "targetType": target_type}
            for path, target, target_type in symlinks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def runtime_package_session_identity(
    package_root: Path,
    *,
    expected_name: str | None = None,
) -> str:
    """Bind a resolved package identity to its command-local filesystem epoch."""

    lexical_root = Path(os.path.abspath(package_root))

    def entry_metadata() -> tuple[object, ...]:
        try:
            entry = lexical_root.lstat()
            target = os.readlink(lexical_root) if lexical_root.is_symlink() else ""
            physical = lexical_root.resolve(strict=True).stat()
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not inspect runtime package entry at {lexical_root}: {exc}"
            ) from exc
        return (
            entry.st_dev,
            entry.st_ino,
            entry.st_mode,
            entry.st_size,
            entry.st_mtime_ns,
            entry.st_ctime_ns,
            target,
            physical.st_dev,
            physical.st_ino,
            physical.st_mode,
            physical.st_size,
            physical.st_mtime_ns,
            physical.st_ctime_ns,
        )

    def paths() -> tuple[Path, ...]:
        physical_root = lexical_root.resolve(strict=True)
        return (
            physical_root / "package.json",
            *_runtime_package_identity_files(physical_root),
        )

    def metadata(runtime_paths: tuple[Path, ...]) -> dict[str, tuple[int, ...]]:
        physical_root = lexical_root.resolve(strict=True)
        result: dict[str, tuple[int, ...]] = {}
        for path in runtime_paths:
            try:
                physical = path.resolve(strict=True)
                item = physical.stat()
            except OSError as exc:
                raise TypeScriptWorkerError(
                    f"Could not inspect runtime package file at {path}: {exc}"
                ) from exc
            result[physical.relative_to(physical_root).as_posix()] = (
                item.st_dev,
                item.st_ino,
                item.st_mode,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
            )
        return result

    try:
        manifest_path = lexical_root.resolve(strict=True) / "package.json"
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {lexical_root}: {exc}"
        ) from exc
    before_entry = entry_metadata()
    before_symlinks = _runtime_package_symlink_topology(lexical_root)
    before_paths = paths()
    before_metadata = metadata(before_paths)
    before_manifest = _stable_bytes(manifest_path, label="runtime package.json")
    content_identity = runtime_package_identity(lexical_root, expected_name=expected_name)
    after_entry = entry_metadata()
    after_symlinks = _runtime_package_symlink_topology(lexical_root)
    after_paths = paths()
    after_metadata = metadata(after_paths)
    after_manifest = _stable_bytes(manifest_path, label="runtime package.json")
    if (
        before_entry != after_entry
        or before_symlinks != after_symlinks
        or before_paths != after_paths
        or before_metadata != after_metadata
        or before_manifest != after_manifest
    ):
        raise TypeScriptWorkerError(
            f"Runtime package changed while its session identity was read: {lexical_root}"
        )
    encoded = json.dumps(
        {
            "format": "javascript-runtime-package-session/3",
            "contentIdentity": content_identity,
            "manifestDigest": hashlib.sha256(before_manifest).hexdigest(),
            "packageEntry": before_entry,
            "files": before_metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _runtime_javascript_tokens(
    source: str,
    *,
    source_path: Path,
    _template_depth: int = 0,
) -> _RuntimeJavaScriptTokens:
    """Tokenize executable JavaScript while discarding inert lexical text.

    This deliberately is not a JavaScript parser. It recognizes enough lexical
    structure to find native ESM/CJS package loads without mistaking comments,
    regex bodies, or template text for executable code. Template expressions
    are tokenized recursively because they can themselves execute a load.
    """

    if _template_depth > 64:
        raise TypeScriptWorkerError(
            f"Runtime package source has excessively nested templates: {source_path}"
        )
    tokens: list[tuple[str, str]] = []
    line_breaks_before: list[bool] = []
    type_body_contexts: list[bool] = []
    class_body_contexts: list[bool] = []
    cursor = 1 if source.startswith("\ufeff") else 0
    if source.startswith("#!", cursor):
        cursor = _line_comment_end(source, cursor + 2)
    previous: tuple[str, str] | None = None
    control_parentheses: list[str | None] = []
    statement_braces: list[bool] = []
    type_body_braces: list[bool] = []
    class_body_braces: list[bool] = []
    brace_parenthesis_depths: list[int] = []
    previous_closed_control_head: str | None = None
    previous_closed_statement_brace = False
    previous_conditional_colon = False
    conditional_expressions = _ConditionalExpressionTracker()
    previous_switch_clause_colon = False
    switch_clauses = _SwitchClauseTracker()
    decorator_candidates = _DecoratorCandidateTracker()
    pending_line_break = False
    jsx_source = source_path.suffix.lower() in {".jsx", ".tsx"}
    expression_depth = [_template_depth]

    def append(kind: str, value: str) -> None:
        nonlocal pending_line_break, previous
        nonlocal previous_closed_control_head, previous_closed_statement_brace
        nonlocal previous_conditional_colon
        nonlocal previous_switch_clause_colon
        closes_control_head: str | None = None
        closes_statement_brace = False
        opens_statement_brace: bool | None = None
        opens_type_body = False
        inside_type_body = type_body_braces[-1] if type_body_braces else False
        inside_class_body = class_body_braces[-1] if class_body_braces else False
        opens_switch_body = (
            kind == "punctuation" and value == "{" and previous_closed_control_head == "switch"
        )
        if kind == "punctuation" and value == "(":
            control_parentheses.append(_control_flow_parenthesis_head(tokens))
        elif kind == "punctuation" and value == ")":
            closes_control_head = control_parentheses.pop() if control_parentheses else None
        elif kind == "punctuation" and value == "{":
            enclosing_type_body = type_body_braces[-1] if type_body_braces else False
            enclosing_statement_brace = statement_braces[-1] if statement_braces else None
            declaration_head = (
                None
                if tokens and tokens[-1] == ("punctuation", "=>")
                else _declaration_body_head(tokens)
            )
            opens_statement_brace = _opens_statement_brace(
                tokens,
                enclosing_statement_brace=enclosing_statement_brace,
                previous_closed_control_head=previous_closed_control_head is not None,
                previous_closed_statement_brace=previous_closed_statement_brace,
                previous_colon_is_conditional=previous_conditional_colon,
                previous_colon_is_switch_clause=previous_switch_clause_colon,
                line_break_before=pending_line_break,
                line_breaks_before=line_breaks_before,
                decorator_candidates=decorator_candidates,
            )
            opens_type_body = _opens_type_body_brace(
                tokens,
                declaration_head=declaration_head,
                enclosing_type_body=enclosing_type_body,
                enclosing_statement_brace=enclosing_statement_brace,
                enclosing_class_body=(class_body_braces[-1] if class_body_braces else False),
                opens_statement_brace=opens_statement_brace,
                nested_in_parentheses=(
                    len(control_parentheses)
                    > (brace_parenthesis_depths[-1] if brace_parenthesis_depths else 0)
                ),
                previous_colon_is_conditional=previous_conditional_colon,
                previous_colon_is_switch_clause=previous_switch_clause_colon,
            )
            statement_braces.append(opens_statement_brace)
            type_body_braces.append(opens_type_body)
            class_body_braces.append(declaration_head == "class")
            brace_parenthesis_depths.append(len(control_parentheses))
        elif kind == "punctuation" and value == "}":
            closes_statement_brace = statement_braces.pop() if statement_braces else False
            if type_body_braces:
                type_body_braces.pop()
            if class_body_braces:
                class_body_braces.pop()
            if brace_parenthesis_depths:
                brace_parenthesis_depths.pop()
        previous_closed_control_head = closes_control_head
        previous_closed_statement_brace = closes_statement_brace
        token = (kind, value)
        token_index = len(tokens)
        tokens.append(token)
        line_breaks_before.append(pending_line_break)
        type_body_contexts.append(inside_type_body)
        class_body_contexts.append(inside_class_body)
        decorator_candidates.append(
            tokens,
            token_index,
            kind,
            value,
        )
        previous_conditional_colon = conditional_expressions.append(kind, value)
        previous_switch_clause_colon = switch_clauses.append(
            kind,
            value,
            opens_switch_body=opens_switch_body,
            conditional_colon=previous_conditional_colon,
        )
        previous = token
        pending_line_break = False

    def append_grouped_value(
        expression_groups: Sequence[_RuntimeExpressionGroup],
        kind: str,
        value: str,
    ) -> None:
        """Replay embedded expressions at their lexical position, then one value."""

        nonlocal pending_line_break
        if not expression_groups:
            append(kind, value)
            return

        def append_expression_group(expression_group: _RuntimeExpressionGroup) -> None:
            nonlocal pending_line_break
            for event in expression_group.events:
                if isinstance(event, _RuntimeTokenEvent):
                    pending_line_break = event.line_break_before
                    append(event.kind, event.value)
                else:
                    pending_line_break = event.line_break_before
                    append_grouped_value(event.groups, event.kind, event.value)

        append("punctuation", "(")
        for expression_group in expression_groups:
            append("punctuation", "(")
            append_expression_group(expression_group)
            append("punctuation", ")")
            append("punctuation", ",")
        append(kind, value)
        append("punctuation", ")")

    def slash_starts_regex() -> bool:
        if previous is None:
            return True
        kind, value = previous
        if kind in _RUNTIME_VALUE_TOKEN_KINDS:
            return False
        if kind == "identifier":
            if pending_line_break and value in {"break", "continue", "debugger"}:
                return True
            return _identifier_precedes_regex(tokens)
        if value == ")" and previous_closed_control_head is not None:
            return True
        if value == "}" and previous_closed_statement_brace:
            return True
        return value not in {")", "]", "}", "++", "--"}

    def skip_quoted(start: int, quote: str) -> tuple[int, str]:
        index = start + 1
        while index < len(source):
            character = source[index]
            if character == "\\":
                index += 2
                continue
            if character == quote:
                return index + 1, source[start + 1 : index]
            if character in "\r\n" and quote != "`":
                break
            index += 1
        raise TypeScriptWorkerError(
            f"Runtime package source has an unterminated string literal: {source_path}"
        )

    def skip_regex(start: int) -> int:
        index = start + 1
        in_character_class = False
        while index < len(source):
            character = source[index]
            if character == "\\":
                index += 2
                continue
            if character in _JAVASCRIPT_LINE_TERMINATORS:
                break
            if character == "[":
                in_character_class = True
            elif character == "]":
                in_character_class = False
            elif character == "/" and not in_character_class:
                index += 1
                while index < len(source) and (source[index].isalpha() or source[index].isdigit()):
                    index += 1
                return index
            index += 1
        # A slash in expression position may still be division. Leave it to
        # the punctuation path instead of rejecting valid minified code.
        return start + 1

    def template_expression_end(start: int) -> tuple[int, _RuntimeExpressionGroup]:
        expression_depth[0] += 1
        if expression_depth[0] > 64:
            raise TypeScriptWorkerError(
                f"Runtime package source has excessively nested expressions: {source_path}"
            )
        index = start
        depth = 0
        expression_previous: tuple[str, str] | None = None
        expression_tokens: list[tuple[str, str]] = []
        expression_line_breaks_before: list[bool] = []
        expression_events: list[_RuntimeTokenEvent | _RuntimeGroupedValueEvent] = []
        expression_control_parentheses: list[str | None] = []
        expression_statement_braces: list[bool] = []
        expression_type_body_braces: list[bool] = []
        expression_class_body_braces: list[bool] = []
        expression_brace_parenthesis_depths: list[int] = []
        expression_previous_closed_control_head: str | None = None
        expression_previous_closed_statement_brace = False
        expression_previous_conditional_colon = False
        expression_conditional_expressions = _ConditionalExpressionTracker()
        expression_previous_switch_clause_colon = False
        expression_switch_clauses = _SwitchClauseTracker()
        expression_decorator_candidates = _DecoratorCandidateTracker()
        expression_pending_line_break = False

        def record_expression(kind: str, value: str, *, emit: bool = True) -> None:
            nonlocal expression_pending_line_break
            nonlocal expression_previous
            nonlocal expression_previous_closed_control_head
            nonlocal expression_previous_closed_statement_brace
            nonlocal expression_previous_conditional_colon
            nonlocal expression_previous_switch_clause_colon
            closes_control_head: str | None = None
            closes_statement_brace = False
            opens_statement_brace: bool | None = None
            opens_type_body = False
            opens_switch_body = (
                kind == "punctuation"
                and value == "{"
                and expression_previous_closed_control_head == "switch"
            )
            if kind == "punctuation" and value == "(":
                expression_control_parentheses.append(
                    _control_flow_parenthesis_head(expression_tokens)
                )
            elif kind == "punctuation" and value == ")":
                closes_control_head = (
                    expression_control_parentheses.pop() if expression_control_parentheses else None
                )
            elif kind == "punctuation" and value == "{":
                enclosing_type_body = (
                    expression_type_body_braces[-1] if expression_type_body_braces else False
                )
                enclosing_statement_brace = (
                    expression_statement_braces[-1] if expression_statement_braces else None
                )
                declaration_head = (
                    None
                    if expression_tokens and expression_tokens[-1] == ("punctuation", "=>")
                    else _declaration_body_head(expression_tokens)
                )
                opens_statement_brace = _opens_statement_brace(
                    expression_tokens,
                    enclosing_statement_brace=enclosing_statement_brace,
                    previous_closed_control_head=(
                        expression_previous_closed_control_head is not None
                    ),
                    previous_closed_statement_brace=(expression_previous_closed_statement_brace),
                    previous_colon_is_conditional=expression_previous_conditional_colon,
                    previous_colon_is_switch_clause=(expression_previous_switch_clause_colon),
                    line_break_before=expression_pending_line_break,
                    line_breaks_before=expression_line_breaks_before,
                    decorator_candidates=expression_decorator_candidates,
                )
                opens_type_body = _opens_type_body_brace(
                    expression_tokens,
                    declaration_head=declaration_head,
                    enclosing_type_body=enclosing_type_body,
                    enclosing_statement_brace=enclosing_statement_brace,
                    enclosing_class_body=(
                        expression_class_body_braces[-1] if expression_class_body_braces else False
                    ),
                    opens_statement_brace=opens_statement_brace,
                    nested_in_parentheses=(
                        len(expression_control_parentheses)
                        > (
                            expression_brace_parenthesis_depths[-1]
                            if expression_brace_parenthesis_depths
                            else 0
                        )
                    ),
                    previous_colon_is_conditional=expression_previous_conditional_colon,
                    previous_colon_is_switch_clause=expression_previous_switch_clause_colon,
                )
                expression_statement_braces.append(opens_statement_brace)
                expression_type_body_braces.append(opens_type_body)
                expression_class_body_braces.append(declaration_head == "class")
                expression_brace_parenthesis_depths.append(len(expression_control_parentheses))
            elif kind == "punctuation" and value == "}":
                closes_statement_brace = (
                    expression_statement_braces.pop() if expression_statement_braces else False
                )
                if expression_type_body_braces:
                    expression_type_body_braces.pop()
                if expression_class_body_braces:
                    expression_class_body_braces.pop()
                if expression_brace_parenthesis_depths:
                    expression_brace_parenthesis_depths.pop()
            expression_previous_closed_control_head = closes_control_head
            expression_previous_closed_statement_brace = closes_statement_brace
            expression_previous = (kind, value)
            expression_token_index = len(expression_tokens)
            expression_tokens.append(expression_previous)
            expression_line_breaks_before.append(expression_pending_line_break)
            if emit:
                expression_events.append(
                    _RuntimeTokenEvent(kind, value, expression_pending_line_break)
                )
            expression_decorator_candidates.append(
                expression_tokens,
                expression_token_index,
                kind,
                value,
            )
            expression_previous_conditional_colon = expression_conditional_expressions.append(
                kind, value
            )
            expression_previous_switch_clause_colon = expression_switch_clauses.append(
                kind,
                value,
                opens_switch_body=opens_switch_body,
                conditional_colon=expression_previous_conditional_colon,
            )
            expression_pending_line_break = False

        def record_grouped_value(
            expression_groups: Sequence[_RuntimeExpressionGroup],
            kind: str,
            value: str,
        ) -> None:
            """Replay nested literal expressions into this executable group."""

            nonlocal expression_pending_line_break
            if not expression_groups:
                record_expression(kind, value)
                return
            expression_events.append(
                _RuntimeGroupedValueEvent(
                    tuple(expression_groups),
                    kind,
                    value,
                    expression_pending_line_break,
                )
            )
            record_expression(kind, value, emit=False)

        def expression_slash_starts_regex() -> bool:
            if expression_previous is None:
                return True
            kind, value = expression_previous
            if kind in _RUNTIME_VALUE_TOKEN_KINDS:
                return False
            if kind == "identifier":
                if expression_pending_line_break and value in {"break", "continue", "debugger"}:
                    return True
                return _identifier_precedes_regex(expression_tokens)
            if value == ")" and expression_previous_closed_control_head is not None:
                return True
            if value == "}" and expression_previous_closed_statement_brace:
                return True
            return value not in {")", "]", "}", "++", "--"}

        while index < len(source):
            character = source[index]
            if character.isspace():
                expression_pending_line_break = (
                    expression_pending_line_break or character in _JAVASCRIPT_LINE_TERMINATORS
                )
                index += 1
                continue
            if source.startswith("//", index):
                line_end = _line_comment_end(source, index + 2)
                expression_pending_line_break = expression_pending_line_break or line_end < len(
                    source
                )
                index = len(source) if line_end == len(source) else line_end + 1
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                if end < 0:
                    raise TypeScriptWorkerError(
                        f"Runtime package source has an unterminated comment: {source_path}"
                    )
                expression_pending_line_break = expression_pending_line_break or any(
                    character in _JAVASCRIPT_LINE_TERMINATORS
                    for character in source[index : end + 2]
                )
                index = end + 2
                continue
            if character in {"'", '"'}:
                index, value = skip_quoted(index, character)
                record_expression("string", value)
                continue
            if character == "`":
                index, template_kind, template_value, template_groups = consume_template(index)
                record_grouped_value(template_groups, template_kind, template_value)
                continue
            if jsx_can_start(
                index,
                starts_operand=expression_slash_starts_regex(),
                type_member_context=(
                    expression_type_body_braces[-1] if expression_type_body_braces else False
                ),
            ):
                jsx_result = skip_jsx_element(index)
                if jsx_result is None:
                    raise TypeScriptWorkerError(
                        f"Runtime package source has malformed JSX: {source_path}"
                    )
                index, jsx_expression_groups = jsx_result
                record_grouped_value(jsx_expression_groups, "jsx", "element")
                continue
            if (
                character == "/"
                and not (jsx_source and index > 0 and source[index - 1] == "<")
                and expression_slash_starts_regex()
            ):
                end = skip_regex(index)
                if end > index + 1:
                    index = end
                    record_expression("regex", "regex")
                    continue
            if character.isalpha() or character in "_$":
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                record_expression("identifier", source[index:end])
                index = end
                continue
            if source.startswith("\\u", index):
                raise TypeScriptWorkerError(
                    f"Runtime package source uses an escaped executable identifier: {source_path}"
                )
            if character.isdigit():
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                    end += 1
                record_expression("number", source[index:end])
                index = end
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                if depth == 0:
                    expression_depth[0] -= 1
                    return index + 1, _RuntimeExpressionGroup(tuple(expression_events))
                depth -= 1
            triple = source[index : index + 3]
            pair = source[index : index + 2]
            punctuation = (
                triple
                if triple == "..."
                else (
                    pair
                    if pair in {"++", "--", "=>", "?.", "??", "&&", "||", "==", "!="}
                    else character
                )
            )
            record_expression("punctuation", punctuation)
            index += len(punctuation)
        raise TypeScriptWorkerError(
            f"Runtime package source has an unterminated template expression: {source_path}"
        )

    def consume_template(
        start: int,
    ) -> tuple[int, str, str, list[_RuntimeExpressionGroup]]:
        """Consume one template literal and retain its executable interpolations."""

        index = start + 1
        chunk_start = index
        chunks: list[str] = []
        expression_groups: list[_RuntimeExpressionGroup] = []
        while index < len(source):
            if source[index] == "\\":
                index += 2
                continue
            if source.startswith("${", index):
                chunks.append(source[chunk_start:index])
                index, expression_tokens = template_expression_end(index + 2)
                expression_groups.append(expression_tokens)
                chunk_start = index
                continue
            if source[index] == "`":
                chunks.append(source[chunk_start:index])
                kind = "computed-template" if expression_groups else "template"
                return index + 1, kind, "".join(chunks), expression_groups
            index += 1
        raise TypeScriptWorkerError(
            f"Runtime package source has an unterminated template: {source_path}"
        )

    parenthesis_end_cache: dict[int, int | None] = {}

    def matching_parenthesis_end(start: int) -> int | None:
        """Return the end of a balanced group with amortized linear scanning."""

        if start in parenthesis_end_cache:
            return parenthesis_end_cache[start]
        if start >= len(source) or source[start] != "(":
            return None
        openings = [start]
        index = start + 1
        while index < len(source):
            if source.startswith("//", index):
                index = _line_comment_end(source, index + 2)
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                if end < 0:
                    break
                index = end + 2
                continue
            character = source[index]
            if character in {"'", '"', "`"}:
                try:
                    index, _value = skip_quoted(index, character)
                except TypeScriptWorkerError:
                    break
                continue
            if character == "(":
                if index in parenthesis_end_cache:
                    cached_end = parenthesis_end_cache[index]
                    if cached_end is None:
                        break
                    index = cached_end
                    continue
                openings.append(index)
            elif character == ")":
                opening = openings.pop()
                end = index + 1
                parenthesis_end_cache[opening] = end
                if not openings:
                    return end
            index += 1
        for opening in openings:
            parenthesis_end_cache[opening] = None
        return None

    def tsx_generic_signature(start: int, *, type_member_context: bool) -> bool:
        """Reject a TSX generic arrow/call signature before JSX speculation."""

        index = start + 1
        angle_depth = 1
        quote: str | None = None
        top_level_words: list[str] = []
        definitive_marker = False
        while index < len(source) and angle_depth:
            character = source[index]
            if quote is not None:
                if character == "\\":
                    index += 2
                    continue
                if character == quote:
                    quote = None
                index += 1
                continue
            if character in {"'", '"', "`"}:
                quote = character
                index += 1
                continue
            if source.startswith("//", index):
                index = _line_comment_end(source, index + 2)
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                if end < 0:
                    return False
                index = end + 2
                continue
            if source.startswith("=>", index):
                index += 2
                continue
            if angle_depth == 1 and (character.isalpha() or character in "_$"):
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                word = source[index:end]
                top_level_words.append(word)
                if word == "extends":
                    following = end
                    while following < len(source) and source[following].isspace():
                        following += 1
                    definitive_marker = definitive_marker or (
                        following < len(source) and source[following] not in "=/>"
                    )
                index = end
                continue
            if angle_depth == 1 and character == ",":
                definitive_marker = True
            elif (
                angle_depth == 1
                and character == "="
                and (
                    len(top_level_words) == 1
                    or (len(top_level_words) == 2 and top_level_words[0] in {"const", "in", "out"})
                )
                and not source.startswith("=>", index)
            ):
                definitive_marker = True
            if character == "<":
                angle_depth += 1
            elif character == ">":
                angle_depth -= 1
            index += 1
        if angle_depth:
            return False
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source) or source[index] != "(":
            return False
        if definitive_marker:
            return True
        parenthesis_end = matching_parenthesis_end(index)
        if parenthesis_end is None:
            return False
        index = parenthesis_end
        while index < len(source) and source[index].isspace():
            index += 1
        if source.startswith("=>", index):
            return True
        if index < len(source) and source[index] == ":":
            return type_member_context
        return False

    def jsx_can_start(
        start: int,
        *,
        starts_operand: bool,
        type_member_context: bool,
    ) -> bool:
        """Return whether an operand-position ``<`` confidently begins JSX."""

        if not jsx_source or not starts_operand or source[start : start + 1] != "<":
            return False
        index = start + 1
        while index < len(source) and source[index].isspace():
            index += 1
        if index >= len(source) or source[index] == "/":
            return False
        if source[index] != ">" and not (source[index].isalpha() or source[index] in "_$"):
            return False
        return not tsx_generic_signature(start, type_member_context=type_member_context)

    def skip_jsx_element(start: int) -> tuple[int, list[_RuntimeExpressionGroup]] | None:
        """Skip one balanced JSX element and retain executable containers."""

        expression_groups: list[_RuntimeExpressionGroup] = []

        def tag(
            tag_start: int,
        ) -> tuple[int, str, bool, bool, list[_RuntimeExpressionGroup]] | None:
            if tag_start >= len(source) or source[tag_start] != "<":
                return None
            index = tag_start + 1
            while index < len(source) and source[index].isspace():
                index += 1
            closing = source.startswith("/", index)
            if closing:
                index += 1
                while index < len(source) and source[index].isspace():
                    index += 1
            if index < len(source) and source[index] == ">":
                return index + 1, "", closing, False, []
            if index >= len(source) or not (source[index].isalpha() or source[index] in "_$"):
                return None
            name_start = index
            index += 1
            while index < len(source) and (source[index].isalnum() or source[index] in "_$.:-"):
                index += 1
            name = source[name_start:index]
            if closing:
                while index < len(source) and source[index].isspace():
                    index += 1
                if index < len(source) and source[index] == ">":
                    return index + 1, name, True, False, []
                return None

            tag_expressions: list[_RuntimeExpressionGroup] = []
            type_argument_depth = 0
            while index < len(source):
                character = source[index]
                if character.isspace():
                    index += 1
                    continue
                if type_argument_depth and source.startswith("//", index):
                    index = _line_comment_end(source, index + 2)
                    continue
                if type_argument_depth and source.startswith("/*", index):
                    end = source.find("*/", index + 2)
                    if end < 0:
                        return None
                    index = end + 2
                    continue
                if character in {"'", '"'} or (type_argument_depth and character == "`"):
                    quote = character
                    index += 1
                    while index < len(source) and source[index] != quote:
                        if type_argument_depth and source[index] == "\\":
                            index += 2
                            continue
                        index += 1
                    if index >= len(source):
                        return None
                    index += 1
                    continue
                if character == "<":
                    type_argument_depth += 1
                    index += 1
                    continue
                if character == ">" and type_argument_depth:
                    if index > 0 and source[index - 1] == "=":
                        index += 1
                        continue
                    type_argument_depth -= 1
                    index += 1
                    continue
                if character == "{" and not type_argument_depth:
                    index, expression_tokens = template_expression_end(index + 1)
                    tag_expressions.append(expression_tokens)
                    continue
                if character == "/" and not type_argument_depth:
                    closing_index = index + 1
                    while closing_index < len(source) and source[closing_index].isspace():
                        closing_index += 1
                    if closing_index < len(source) and source[closing_index] == ">":
                        return closing_index + 1, name, False, True, tag_expressions
                if character == ">" and not type_argument_depth:
                    return index + 1, name, False, False, tag_expressions
                index += 1
            return None

        opening = tag(start)
        if opening is None:
            return None
        cursor, name, closing, self_closing, opening_expressions = opening
        if closing:
            return None
        expression_groups.extend(opening_expressions)
        if self_closing:
            return cursor, expression_groups
        names = [name]
        while cursor < len(source):
            if source[cursor] == "{":
                cursor, expression_tokens = template_expression_end(cursor + 1)
                expression_groups.append(expression_tokens)
                continue
            if source.startswith("</", cursor):
                closing_tag = tag(cursor)
                if closing_tag is None:
                    return None
                cursor, closing_name, is_closing, _self_closing, closing_expressions = closing_tag
                if not is_closing or closing_expressions or closing_name != names[-1]:
                    return None
                names.pop()
                if not names:
                    return cursor, expression_groups
                continue
            if source[cursor] == "<":
                nested = tag(cursor)
                if nested is None:
                    return None
                cursor, nested_name, is_closing, nested_self_closing, nested_expressions = nested
                if is_closing:
                    return None
                expression_groups.extend(nested_expressions)
                if not nested_self_closing:
                    names.append(nested_name)
                continue
            cursor += 1
        return None

    while cursor < len(source):
        character = source[cursor]
        if character.isspace():
            pending_line_break = pending_line_break or character in _JAVASCRIPT_LINE_TERMINATORS
            cursor += 1
            continue
        if source.startswith("//", cursor):
            line_end = _line_comment_end(source, cursor + 2)
            pending_line_break = pending_line_break or line_end < len(source)
            cursor = len(source) if line_end == len(source) else line_end + 1
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                raise TypeScriptWorkerError(
                    f"Runtime package source has an unterminated comment: {source_path}"
                )
            pending_line_break = pending_line_break or any(
                character in _JAVASCRIPT_LINE_TERMINATORS for character in source[cursor : end + 2]
            )
            cursor = end + 2
            continue
        if character in {"'", '"'}:
            cursor, value = skip_quoted(cursor, character)
            append("string", value)
            continue
        if character == "`":
            cursor, template_kind, template_value, template_groups = consume_template(cursor)
            append_grouped_value(template_groups, template_kind, template_value)
            continue
        if jsx_can_start(
            cursor,
            starts_operand=slash_starts_regex(),
            type_member_context=type_body_braces[-1] if type_body_braces else False,
        ):
            jsx_result = skip_jsx_element(cursor)
            if jsx_result is None:
                raise TypeScriptWorkerError(
                    f"Runtime package source has malformed JSX: {source_path}"
                )
            cursor, jsx_expression_groups = jsx_result
            append_grouped_value(jsx_expression_groups, "jsx", "element")
            continue
        if (
            character == "/"
            and not (jsx_source and cursor > 0 and source[cursor - 1] == "<")
            and slash_starts_regex()
        ):
            end = skip_regex(cursor)
            if end > cursor + 1:
                cursor = end
                append("regex", "regex")
                continue
        if character.isalpha() or character in "_$":
            end = cursor + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            append("identifier", source[cursor:end])
            cursor = end
            continue
        if source.startswith("\\u", cursor):
            raise TypeScriptWorkerError(
                f"Runtime package source uses an escaped executable identifier: {source_path}"
            )
        if character.isdigit():
            end = cursor + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            append("number", source[cursor:end])
            cursor = end
            continue
        triple = source[cursor : cursor + 3]
        pair = source[cursor : cursor + 2]
        if triple == "...":
            append("punctuation", triple)
            cursor += 3
        elif pair in {"++", "--", "=>", "?.", "??", "&&", "||", "==", "!="}:
            append("punctuation", pair)
            cursor += 2
        else:
            append("punctuation", character)
            cursor += 1
    return _RuntimeJavaScriptTokens(
        tokens,
        line_breaks_before,
        type_body_contexts,
        class_body_contexts,
    )


class _ScopeCapabilityNode:
    """One balanced interval-tree node for a proven lexical capability."""

    __slots__ = (
        "capability",
        "end",
        "height",
        "left",
        "max_end",
        "right",
        "start",
    )

    def __init__(self, start: int, end: int, capability: str) -> None:
        self.start = start
        self.end = end
        self.capability = capability
        self.left: _ScopeCapabilityNode | None = None
        self.right: _ScopeCapabilityNode | None = None
        self.height = 1
        self.max_end = end


class _ScopeCapabilityIndex:
    """Incrementally resolve nearest containing bindings in logarithmic time."""

    def __init__(
        self,
        bindings: Mapping[int, tuple[int, str]],
        scope_open: Sequence[int],
        scope_end_exclusive: Sequence[int],
    ) -> None:
        self._root: _ScopeCapabilityNode | None = None
        for scope, (_binding_index, capability) in bindings.items():
            self.add(
                start=scope_open[scope],
                end=scope_end_exclusive[scope],
                capability=capability,
            )

    @staticmethod
    def _height(node: _ScopeCapabilityNode | None) -> int:
        return 0 if node is None else node.height

    @classmethod
    def _refresh(cls, node: _ScopeCapabilityNode) -> None:
        node.height = max(cls._height(node.left), cls._height(node.right)) + 1
        node.max_end = max(
            node.end,
            -1 if node.left is None else node.left.max_end,
            -1 if node.right is None else node.right.max_end,
        )

    @classmethod
    def _rotate_left(cls, node: _ScopeCapabilityNode) -> _ScopeCapabilityNode:
        pivot = node.right
        assert pivot is not None
        node.right = pivot.left
        pivot.left = node
        cls._refresh(node)
        cls._refresh(pivot)
        return pivot

    @classmethod
    def _rotate_right(cls, node: _ScopeCapabilityNode) -> _ScopeCapabilityNode:
        pivot = node.left
        assert pivot is not None
        node.left = pivot.right
        pivot.right = node
        cls._refresh(node)
        cls._refresh(pivot)
        return pivot

    @classmethod
    def _rebalance(cls, node: _ScopeCapabilityNode) -> _ScopeCapabilityNode:
        cls._refresh(node)
        balance = cls._height(node.left) - cls._height(node.right)
        if balance > 1:
            assert node.left is not None
            if cls._height(node.left.left) < cls._height(node.left.right):
                node.left = cls._rotate_left(node.left)
            return cls._rotate_right(node)
        if balance < -1:
            assert node.right is not None
            if cls._height(node.right.right) < cls._height(node.right.left):
                node.right = cls._rotate_right(node.right)
            return cls._rotate_left(node)
        return node

    @classmethod
    def _insert(
        cls,
        node: _ScopeCapabilityNode | None,
        *,
        start: int,
        end: int,
        capability: str,
    ) -> _ScopeCapabilityNode:
        if node is None:
            return _ScopeCapabilityNode(start, end, capability)
        if start < node.start:
            node.left = cls._insert(
                node.left,
                start=start,
                end=end,
                capability=capability,
            )
        elif start > node.start:
            node.right = cls._insert(
                node.right,
                start=start,
                end=end,
                capability=capability,
            )
        else:
            node.end = end
            node.capability = capability
        return cls._rebalance(node)

    def add(self, *, start: int, end: int, capability: str) -> None:
        """Insert or replace one scope interval without rebuilding the index."""

        self._root = self._insert(
            self._root,
            start=start,
            end=end,
            capability=capability,
        )

    def capability_at(self, reference_index: int) -> str | None:
        """Return the binding with the deepest interval containing the token."""

        node = self._rightmost_containing(
            self._root,
            reference_index=reference_index,
        )
        return None if node is None else node.capability

    @classmethod
    def _rightmost_containing(
        cls,
        node: _ScopeCapabilityNode | None,
        *,
        reference_index: int,
    ) -> _ScopeCapabilityNode | None:
        if node is None or node.max_end <= reference_index:
            return None
        if node.start >= reference_index:
            return cls._rightmost_containing(
                node.left,
                reference_index=reference_index,
            )
        result = cls._rightmost_containing(
            node.right,
            reference_index=reference_index,
        )
        if result is not None:
            return result
        if node.end > reference_index:
            return node
        return cls._rightmost_containing(
            node.left,
            reference_index=reference_index,
        )


def _create_require_module_specifiers(
    tokens: tuple[tuple[str, str], ...],
    *,
    source_path: Path,
) -> tuple[str, ...]:
    """Track provenance-backed ``createRequire`` factories and loaders.

    Runtime packages commonly bridge from ESM to CommonJS through Node's
    ``createRequire``. Treat only bindings proven to originate from the Node
    ``module`` builtin as capabilities; a coincidentally named application
    function is not a loader. Once proven, aliases may only be assigned or
    called in simple static forms so the dependency graph cannot silently lose
    an edge through composition or reassignment.
    """

    node_module_specifiers = {"module", "node:module"}
    capability_names: set[str] = set()
    hoisted_capability_indices: set[int] = set()
    parameter_property_capabilities: set[str] = set()
    capability_bindings: dict[str, dict[int, tuple[int, str]]] = {}
    capability_interval_indices: dict[str, _ScopeCapabilityIndex] = {}
    safe_indices: set[int] = set()
    assignments: list[tuple[int, str, int]] = []
    specifiers: set[str] = set()
    loader_maps: set[str] = set()
    scope_at_token: list[int] = []
    scope_open = [-1]
    scope_end_exclusive = [len(tokens)]
    scope_kinds = ["function"]
    scope_var_owner = [0]
    scope_stack = [0]
    scope_control_parentheses: list[str | None] = []
    scope_parenthesis_indices: list[int] = []
    scope_paren_open_for_close: dict[int, int] = {}
    scope_enclosing_paren_at_token: list[int] = []
    scope_brace_indices: list[int] = []
    scope_brace_open_for_close: dict[int, int] = {}
    scope_statement_braces: list[bool] = []
    scope_lexical_braces: list[bool] = []
    scope_prebody_ranges: list[tuple[int, int, int, int]] = []
    scope_nonstatement_brace_indices: set[int] = set()
    scope_closed_control_before: list[bool] = []
    scope_previous_closed_control: str | None = None
    scope_previous_closed_statement = False
    scope_tokens: list[tuple[str, str]] = []
    token_line_breaks = getattr(tokens, "line_breaks_before", ())
    if len(token_line_breaks) != len(tokens):
        token_line_breaks = (False,) * len(tokens)
    conditional_expression_colons = _conditional_expression_colons(tokens)
    annotation_initializer_starts = _annotation_initializer_starts(tokens)
    scope_line_breaks_before: list[bool] = []
    scope_switch_clauses = _SwitchClauseTracker()
    scope_previous_switch_clause_colon = False
    scope_decorator_candidates = _DecoratorCandidateTracker()
    for token_index, (kind, value) in enumerate(tokens):
        scope_at_token.append(scope_stack[-1])
        scope_enclosing_paren_at_token.append(
            scope_parenthesis_indices[-1] if scope_parenthesis_indices else -1
        )
        scope_closed_control_before.append(scope_previous_closed_control is not None)
        closes_control: str | None = None
        closes_statement = False
        closes_lexical_scope = False
        opens_statement: bool | None = None
        opens_lexical_scope = False
        lexical_scope_kind: str | None = None
        function_scope_start: int | None = None
        opens_switch_body = value == "{" and scope_previous_closed_control == "switch"
        if value == "(":
            scope_control_parentheses.append(_control_flow_parenthesis_head(scope_tokens))
            scope_parenthesis_indices.append(token_index)
        elif value == ")":
            closes_control = scope_control_parentheses.pop() if scope_control_parentheses else None
            if scope_parenthesis_indices:
                scope_paren_open_for_close[token_index] = scope_parenthesis_indices.pop()
        elif value == "{":
            enclosing_statement_brace = (
                scope_statement_braces[-1] if scope_statement_braces else None
            )
            opens_statement = _opens_statement_brace(
                scope_tokens,
                enclosing_statement_brace=enclosing_statement_brace,
                previous_closed_control_head=scope_previous_closed_control is not None,
                previous_closed_statement_brace=scope_previous_closed_statement,
                previous_colon_is_conditional=(
                    token_index > 0 and token_index - 1 in conditional_expression_colons
                ),
                previous_colon_is_switch_clause=scope_previous_switch_clause_colon,
                line_breaks_before=scope_line_breaks_before,
                decorator_candidates=scope_decorator_candidates,
            )
            lexical_scope_kind = _brace_lexical_scope_kind(
                scope_tokens,
                opens_statement=opens_statement,
                enclosing_statement_brace=enclosing_statement_brace,
            )
            if (
                lexical_scope_kind == "function"
                and len(scope_tokens) >= 2
                and scope_tokens[-1][1] == "=>"
                and scope_tokens[-2][1] == "}"
            ):
                return_type_open = scope_brace_open_for_close.get(len(scope_tokens) - 2)
                if (
                    return_type_open is not None
                    and return_type_open > 0
                    and scope_tokens[return_type_open - 1][1] == ":"
                ):
                    function_scope_start = _function_scope_start(
                        scope_tokens[: return_type_open - 1],
                        scope_paren_open_for_close,
                    )
            if (
                lexical_scope_kind in {"block", "function"}
                and scope_tokens
                and scope_tokens[-1][1] == "}"
                and not scope_previous_closed_statement
            ):
                return_type_open = scope_brace_open_for_close.get(len(scope_tokens) - 1)
                if (
                    return_type_open is not None
                    and return_type_open > 0
                    and scope_tokens[return_type_open - 1][1] == ":"
                ):
                    function_scope_start = _function_scope_start(
                        scope_tokens[: return_type_open - 1],
                        scope_paren_open_for_close,
                    )
                    if function_scope_start is not None:
                        lexical_scope_kind = "function"
            opens_lexical_scope = lexical_scope_kind is not None
            scope_statement_braces.append(opens_statement)
            scope_lexical_braces.append(opens_lexical_scope)
            scope_brace_indices.append(token_index)
            if opens_lexical_scope:
                parent_scope = scope_stack[-1]
                scope_start = token_index
                if (
                    scope_previous_closed_control in {"catch", "for"}
                    and scope_tokens
                    and scope_tokens[-1][1] == ")"
                ):
                    scope_start = scope_paren_open_for_close.get(len(scope_tokens) - 1, token_index)
                elif lexical_scope_kind == "function":
                    if function_scope_start is None:
                        function_scope_start = _function_scope_start(
                            scope_tokens,
                            scope_paren_open_for_close,
                        )
                    if function_scope_start is not None:
                        scope_start = function_scope_start
                new_scope = len(scope_open)
                scope_open.append(scope_start)
                scope_end_exclusive.append(len(tokens))
                scope_kinds.append(lexical_scope_kind or "block")
                scope_var_owner.append(
                    new_scope
                    if lexical_scope_kind in {"function", "namespace", "static"}
                    else scope_var_owner[parent_scope]
                )
                scope_stack.append(new_scope)
                if scope_start < token_index:
                    scope_prebody_ranges.append(
                        (scope_start + 1, token_index, new_scope, parent_scope)
                    )
            else:
                scope_nonstatement_brace_indices.add(token_index)
        elif value == "}":
            if scope_brace_indices:
                scope_brace_open_for_close[token_index] = scope_brace_indices.pop()
            closes_statement = scope_statement_braces.pop() if scope_statement_braces else False
            closes_lexical_scope = scope_lexical_braces.pop() if scope_lexical_braces else False
            if closes_lexical_scope and len(scope_stack) > 1:
                scope_end_exclusive[scope_stack[-1]] = token_index + 1
                scope_stack.pop()
        scope_previous_closed_control = closes_control
        scope_previous_closed_statement = closes_statement
        scope_tokens.append((kind, value))
        scope_line_breaks_before.append(bool(token_line_breaks[token_index]))
        scope_decorator_candidates.append(
            scope_tokens,
            token_index,
            kind,
            value,
        )
        scope_previous_switch_clause_colon = scope_switch_clauses.append(
            kind,
            value,
            opens_switch_body=opens_switch_body,
            conditional_colon=(token_index in conditional_expression_colons),
        )

    scope_prebody_at_token = [-1] * len(tokens)
    prebody_ranges_by_start: dict[int, list[tuple[int, int, int]]] = {}
    for start, end, scope, parent_scope in scope_prebody_ranges:
        prebody_ranges_by_start.setdefault(start, []).append((end, scope, parent_scope))
    active_prebody_ranges: list[tuple[int, int, int]] = []
    for token_index, base_scope in enumerate(scope_at_token):
        while active_prebody_ranges and active_prebody_ranges[-1][0] <= token_index:
            active_prebody_ranges.pop()
        starting_ranges = prebody_ranges_by_start.get(token_index, ())
        for prebody_range in sorted(starting_ranges, reverse=True):
            active_prebody_ranges.append(prebody_range)
        if active_prebody_ranges and active_prebody_ranges[-1][2] == base_scope:
            scope_prebody_at_token[token_index] = active_prebody_ranges[-1][1]
    scope_paren_close_for_open = {
        opening: closing for closing, opening in scope_paren_open_for_close.items()
    }
    scope_brace_close_for_open = {
        opening: closing for closing, opening in scope_brace_open_for_close.items()
    }

    def token_value(index: int) -> str:
        return tokens[index][1] if 0 <= index < len(tokens) else ""

    simple_declaration_kinds: list[str | None] = [None] * len(tokens)
    declaration_kind_frames: list[str | None] = [None]
    declaration_expects_binding = [False]
    for index, (kind, value) in enumerate(tokens):
        if value in {"(", "[", "{"}:
            if declaration_expects_binding[-1]:
                # A destructuring pattern consumes the pending binding slot;
                # its aliases are handled by the dedicated provenance pass.
                declaration_expects_binding[-1] = False
            declaration_kind_frames.append(None)
            declaration_expects_binding.append(False)
            continue
        if value in {")", "]", "}"}:
            if len(declaration_kind_frames) > 1:
                declaration_kind_frames.pop()
                declaration_expects_binding.pop()
            continue
        if (
            kind == "identifier"
            and value in {"const", "let", "var"}
            and token_value(index - 1) not in {".", "?.", "as"}
        ):
            declaration_kind_frames[-1] = value
            declaration_expects_binding[-1] = True
            continue
        if kind == "identifier" and declaration_expects_binding[-1]:
            simple_declaration_kinds[index] = declaration_kind_frames[-1]
            declaration_expects_binding[-1] = False
            continue
        if value == "," and declaration_kind_frames[-1] is not None:
            declaration_expects_binding[-1] = True
        elif value == ";":
            declaration_kind_frames[-1] = None
            declaration_expects_binding[-1] = False

    def owned_scope_at(index: int) -> int:
        prebody_scope = scope_prebody_at_token[index]
        return scope_at_token[index] if prebody_scope < 0 else prebody_scope

    var_declared_names_by_scope: dict[int, set[str]] = {}
    for index, declaration_kind in enumerate(simple_declaration_kinds):
        if declaration_kind != "var" or tokens[index][0] != "identifier":
            continue
        owner = scope_var_owner[owned_scope_at(index)]
        var_declared_names_by_scope.setdefault(owner, set()).add(tokens[index][1])

    def matching_close(open_index: int, opening: str = "(", closing: str = ")") -> int | None:
        if token_value(open_index) != opening:
            return None
        if opening == "(" and closing == ")":
            return scope_paren_close_for_open.get(open_index)
        if opening == "{" and closing == "}":
            return scope_brace_close_for_open.get(open_index)
        depth = 1
        cursor = open_index + 1
        while cursor < len(tokens):
            value = token_value(cursor)
            if value == opening:
                depth += 1
            elif value == closing:
                depth -= 1
                if depth == 0:
                    return cursor
            cursor += 1
        return None

    def matching_open(close_index: int, opening: str = "(", closing: str = ")") -> int | None:
        if token_value(close_index) != closing:
            return None
        if opening == "(" and closing == ")":
            return scope_paren_open_for_close.get(close_index)
        if opening == "{" and closing == "}":
            return scope_brace_open_for_close.get(close_index)
        depth = 1
        cursor = close_index - 1
        while cursor >= 0:
            value = token_value(cursor)
            if value == closing:
                depth += 1
            elif value == opening:
                depth -= 1
                if depth == 0:
                    return cursor
            cursor -= 1
        return None

    unsupported_var_pattern_names_by_scope: dict[int, set[str]] = {}
    for index, token in enumerate(tokens):
        if token != ("identifier", "var") or token_value(index + 1) not in {"{", "["}:
            continue
        opening = token_value(index + 1)
        closing = "}" if opening == "{" else "]"
        close = matching_close(index + 1, opening, closing)
        if close is None:
            continue
        owner = scope_var_owner[owned_scope_at(index)]
        names = unsupported_var_pattern_names_by_scope.setdefault(owner, set())
        names.update(value for kind, value in tokens[index + 2 : close] if kind == "identifier")

    def decorator_before(index: int) -> bool:
        """Recognize a directly preceding parameter/member decorator."""

        cursor = index - 1
        while cursor >= 0:
            value = token_value(cursor)
            if value == "@":
                return True
            if tokens[cursor][0] == "identifier" or value in {".", "?."}:
                cursor -= 1
                continue
            if value in {")", "]", ">"}:
                opening = {")": "(", "]": "[", ">": "<"}[value]
                open_index = matching_open(cursor, opening, value)
                if open_index is None:
                    return False
                cursor = open_index - 1
                continue
            if value == "?":
                cursor -= 1
                continue
            return False
        return False

    def assignment_initializer(index: int) -> int | None:
        cursor = index + 1
        if token_value(cursor) == "=":
            return cursor + 1
        if token_value(cursor) != ":":
            return None
        if cursor in conditional_expression_colons:
            return None
        previous = token_value(index - 1)
        statement_brace_before = previous == "{" and scope_open[scope_at_token[index]] == index - 1
        nonstatement_brace_before = (
            previous == "{" and index - 1 in scope_nonstatement_brace_indices
        )
        typed_binding_prefix = (
            previous in {"(", ","}
            or previous in _TYPED_BINDING_PREFIXES
            or nonstatement_brace_before
            or (previous == "." and token_value(index - 2) == "." and token_value(index - 3) == ".")
            or decorator_before(index)
        )
        line_break_before = bool(token_line_breaks[index])
        asi_boundary_before = (
            line_break_before and index > 0 and _can_end_statement_before_label(*tokens[index - 1])
        )
        if (
            index == 0
            or previous in {";", "}"}
            or previous in {"catch", "do", "else", "finally", "try"}
            or statement_brace_before
            or scope_closed_control_before[index]
            or (asi_boundary_before and not typed_binding_prefix)
        ):
            # A statement-leading identifier followed by a colon is a label,
            # not a TypeScript binding annotation. Keep this check local: a
            # backwards statement scan for every typed parameter is quadratic.
            return None
        if previous == ":" or (
            not typed_binding_prefix
            and _colon_opens_statement_block(
                tokens,
                enclosing_statement_brace=True,
                end=cursor + 1,
                line_breaks_before=token_line_breaks,
            )
        ):
            # Catch nested labels and switch clauses without charging ordinary
            # typed parameter/declaration lists for a backwards statement scan.
            return None
        initializer = annotation_initializer_starts[cursor + 1]
        return None if initializer < 0 else initializer

    def static_import_clause(index: int) -> tuple[int, int, str] | None:
        next_index = index + 1
        if next_index >= len(tokens) or token_value(next_index) in {"(", ".", "?."}:
            return None
        if tokens[next_index][0] in {"string", "template"}:
            return next_index, next_index, tokens[next_index][1]
        cursor = next_index
        while cursor < len(tokens) and token_value(cursor) != ";":
            if token_value(cursor) == "=":
                # TypeScript import assignments have no ``from`` clause. In
                # semicolonless source, stopping here also prevents every
                # alias declaration from scanning the remainder of the file.
                return None
            if (
                tokens[cursor] == ("identifier", "from")
                and cursor + 1 < len(tokens)
                and tokens[cursor + 1][0] in {"string", "template"}
            ):
                return next_index, cursor, tokens[cursor + 1][1]
            cursor += 1
        return None

    def binding_declaration_kind(index: int) -> str | None:
        """Return the lexical declaration keyword owning a simple binding."""

        indexed = simple_declaration_kinds[index]
        if indexed is not None:
            return indexed
        previous = token_value(index - 1)
        if previous in {"const", "let", "var"}:
            return previous
        # Destructured aliases are separated from their declaration keyword by
        # one pattern brace. Keep this deliberately bounded; deeper binding
        # patterns are rejected by the provenance pass rather than guessed.
        cursor = index - 1
        while cursor >= 0 and index - cursor <= 8:
            value = token_value(cursor)
            if value == "{":
                declaration = token_value(cursor - 1)
                return declaration if declaration in {"const", "let", "var"} else None
            if value in {";", "=", "}"}:
                return None
            cursor -= 1
        return None

    def unsupported_prebody_binding(index: int, declaration_kind: str | None) -> str | None:
        """Return an unmodeled parameter/loop lifetime that must fail closed."""

        if scope_prebody_at_token[index] >= 0:
            return None
        opening = scope_enclosing_paren_at_token[index]
        if opening < 0:
            return None
        closing = scope_paren_close_for_open.get(opening)
        if closing is None:
            return None
        if token_value(opening - 1) == "for" and declaration_kind in {"const", "let"}:
            return "an unbraced for-head lexical binding"
        cursor = closing + 1
        while cursor < len(tokens) and token_value(cursor) != "=":
            if token_value(cursor) == "{":
                nested_close = scope_brace_close_for_open.get(cursor)
                if nested_close is not None:
                    cursor = nested_close + 1
                    continue
            if token_value(cursor) == ";":
                return None
            if token_value(cursor) == "=>":
                if token_value(cursor + 1) != "{":
                    return "an expression-bodied arrow parameter"
                return "an arrow parameter whose body scope could not be proven"
            cursor += 1
        return None

    def set_capability(
        name: str,
        capability: str,
        binding_index: int,
        *,
        declaration_kind_override: str | None = None,
    ) -> bool:
        binding_scope = owned_scope_at(binding_index)
        declaration_kind = declaration_kind_override or binding_declaration_kind(binding_index)
        modifier_cursor = binding_index - 1
        parameter_property = False
        while modifier_cursor >= 0 and binding_index - modifier_cursor <= 4:
            modifier = token_value(modifier_cursor)
            if modifier in {"private", "protected", "public", "readonly"}:
                parameter_property = True
                break
            if modifier in {"(", ","}:
                break
            modifier_cursor -= 1
        if parameter_property and scope_prebody_at_token[binding_index] >= 0:
            parameter_property_capabilities.add(name)
        unsupported_context = unsupported_prebody_binding(binding_index, declaration_kind)
        if unsupported_context is not None:
            raise TypeScriptWorkerError(
                f"Runtime package stores module-loading capability {name!r} in "
                f"{unsupported_context}; this lifetime is unsupported: {source_path}"
            )
        if declaration_kind == "var":
            binding_scope = scope_var_owner[binding_scope]
        elif declaration_kind is None:
            var_owner = scope_var_owner[binding_scope]
            if name in unsupported_var_pattern_names_by_scope.get(var_owner, ()):
                raise TypeScriptWorkerError(
                    f"Runtime package assigns module-loading capability {name!r} through a "
                    f"destructured var binding; this lifetime is unsupported: {source_path}"
                )
            if name in var_declared_names_by_scope.get(var_owner, ()):
                binding_scope = var_owner
        if scope_kinds[binding_scope] == "class":
            raise TypeScriptWorkerError(
                f"Runtime package stores module-loading capability {name!r} in a class field; "
                f"property-backed loaders are unsupported: {source_path}"
            )
        safe_indices.add(binding_index)
        bindings = capability_bindings.setdefault(name, {})
        existing = bindings.get(binding_scope)
        if existing is not None and existing[1] != capability:
            raise TypeScriptWorkerError(
                f"Runtime package ambiguously rebinds module-loading capability {name!r} "
                f"within one lexical scope: {source_path}"
            )
        changed = existing is None or binding_index > existing[0]
        if existing is None or binding_index > existing[0]:
            bindings[binding_scope] = (binding_index, capability)
            interval_index = capability_interval_indices.get(name)
            if interval_index is not None:
                interval_index.add(
                    start=scope_open[binding_scope],
                    end=scope_end_exclusive[binding_scope],
                    capability=capability,
                )
        capability_names.add(name)
        return changed

    def capability_at(index: int, name: str) -> str | None:
        """Return the nearest proven capability whose lexical brace owns a use."""

        if not 0 <= index < len(scope_at_token):
            return None
        bindings = capability_bindings.get(name)
        if bindings is None:
            return None
        reference_scope = scope_prebody_at_token[index]
        if reference_scope < 0:
            reference_scope = scope_at_token[index]
        exact = bindings.get(reference_scope)
        if exact is not None:
            return exact[1]
        interval_index = capability_interval_indices.get(name)
        if interval_index is None:
            interval_index = _ScopeCapabilityIndex(
                bindings,
                scope_open,
                scope_end_exclusive,
            )
            capability_interval_indices[name] = interval_index
        return interval_index.capability_at(index)

    # ESM imports are hoisted, so establish their provenance before walking
    # assignments and uses in source order.
    for index, token in enumerate(tokens):
        if token != ("identifier", "import") or token_value(index - 1) in {".", "?."}:
            continue
        clause = static_import_clause(index)
        if clause is None:
            continue
        clause_start, from_index, specifier = clause
        if specifier not in node_module_specifiers or clause_start == from_index:
            continue
        if tokens[clause_start] == ("identifier", "type"):
            continue
        if tokens[clause_start][0] == "identifier":
            set_capability(tokens[clause_start][1], "namespace", clause_start)
            hoisted_capability_indices.add(clause_start)
        cursor = clause_start
        while cursor < from_index:
            if (
                token_value(cursor) == "*"
                and tokens[cursor + 1 : cursor + 2] == (("identifier", "as"),)
                and cursor + 2 < from_index
                and tokens[cursor + 2][0] == "identifier"
            ):
                set_capability(tokens[cursor + 2][1], "namespace", cursor + 2)
                hoisted_capability_indices.add(cursor + 2)
                cursor += 3
                continue
            if token_value(cursor) != "{":
                cursor += 1
                continue
            close = matching_close(cursor, "{", "}")
            if close is None or close > from_index:
                break
            binding = cursor + 1
            while binding < close:
                type_only = tokens[binding] == ("identifier", "type")
                if type_only:
                    binding += 1
                if binding >= close or tokens[binding][0] != "identifier":
                    binding += 1
                    continue
                imported = tokens[binding][1]
                safe_indices.add(binding)
                local_index = binding
                local = imported
                if (
                    binding + 2 < close
                    and tokens[binding + 1] == ("identifier", "as")
                    and tokens[binding + 2][0] == "identifier"
                ):
                    local_index = binding + 2
                    local = tokens[local_index][1]
                    binding += 2
                if imported == "createRequire" and not type_only:
                    set_capability(local, "factory", local_index)
                    hoisted_capability_indices.add(local_index)
                elif imported in {"Module", "default"} and not type_only:
                    set_capability(local, "namespace", local_index)
                    hoisted_capability_indices.add(local_index)
                binding += 1
            cursor = close + 1

    def literal_call_argument(open_index: int) -> str | None:
        argument = open_index + 1
        if argument >= len(tokens) or tokens[argument][0] not in {"string", "template"}:
            # Runtime packages legitimately resolve user-provided ids. The
            # documented closure covers statically named loads; retain
            # capability provenance but do not invent a package edge here.
            return None
        return tokens[argument][1]

    def node_module_value_end(index: int) -> int | None:
        cursor = index
        awaited = token_value(cursor) == "await"
        if awaited:
            cursor += 1
        if tokens[cursor : cursor + 1] == (("identifier", "import"),):
            if not awaited:
                return None
            open_index = cursor + 1
        elif tokens[cursor : cursor + 1] == (("identifier", "require"),):
            if _type_only_import_assignment_require(tokens, cursor):
                return None
            open_index = cursor + 1
        elif (
            tokens[cursor : cursor + 1] == (("identifier", "module"),)
            and token_value(cursor + 1) in {".", "?."}
            and tokens[cursor + 2 : cursor + 3] == (("identifier", "require"),)
        ):
            open_index = cursor + 3
        else:
            return None
        if (
            token_value(open_index) != "("
            or open_index + 1 >= len(tokens)
            or tokens[open_index + 1][0] not in {"string", "template"}
            or tokens[open_index + 1][1] not in node_module_specifiers
        ):
            return None
        close = matching_close(open_index)
        return None if close is None else close + 1

    def factory_reference_end(index: int) -> int | None:
        if index >= len(tokens):
            return None
        namespace_value_end = node_module_value_end(index)
        if (
            namespace_value_end is not None
            and token_value(namespace_value_end) in {".", "?."}
            and tokens[namespace_value_end + 1 : namespace_value_end + 2]
            == (("identifier", "createRequire"),)
        ):
            return namespace_value_end + 2
        if token_value(index) == "(":
            group_close = matching_close(index)
            if (
                group_close is not None
                and node_module_value_end(index + 1) == group_close
                and token_value(group_close + 1) in {".", "?."}
                and tokens[group_close + 2 : group_close + 3] == (("identifier", "createRequire"),)
            ):
                return group_close + 3
        if tokens[index][0] != "identifier":
            return None
        name = tokens[index][1]
        if capability_at(index, name) == "factory":
            return index + 1
        if (
            capability_at(index, name) == "namespace"
            and token_value(index + 1) in {".", "?."}
            and tokens[index + 2 : index + 3] == (("identifier", "createRequire"),)
        ):
            return index + 3
        return None

    def rhs_capability(index: int) -> tuple[str, int] | None:
        factory_end = factory_reference_end(index)
        if factory_end is not None:
            if token_value(factory_end) != "(":
                return "factory", factory_end
            close = matching_close(factory_end)
            if close is None:
                raise TypeScriptWorkerError(
                    f"Runtime package has an unterminated createRequire call: {source_path}"
                )
            if token_value(close + 1) in {"(", ".", "?.", "["}:
                # The returned loader is consumed immediately; the whole
                # expression is a module value, not a loader alias.
                return None
            return "loader", close + 1
        namespace_end = node_module_value_end(index)
        if namespace_end is not None:
            return "namespace", namespace_end
        if index < len(tokens) and tokens[index][0] == "identifier":
            direct = capability_at(index, tokens[index][1])
            if direct in {"namespace", "loader"} and token_value(index + 1) not in {
                "(",
                ".",
                "?.",
                "[",
            }:
                return direct, index + 1
        return None

    def proven_rhs_capability(index: int) -> tuple[str, int] | None:
        """Recognize a capability through transparent parenthesis groups."""

        group_closes: list[int] = []
        cursor = index
        while token_value(cursor) == "(":
            close = matching_close(cursor)
            if close is None:
                return None
            group_closes.append(close)
            cursor += 1
        capability = rhs_capability(cursor)
        if capability is None:
            return None
        kind, end = capability
        for close in reversed(group_closes):
            if end != close:
                return None
            end = close + 1
        return kind, end

    def safe_rhs_suffix(index: int) -> bool:
        if index >= len(tokens):
            return True
        kind, value = tokens[index]
        if kind == "identifier":
            # An identifier can begin an ASI-separated statement.
            return value not in {"in", "instanceof"}
        return value in {";", ",", ")", "]", "}"}

    def initializer_is_function(index: int) -> bool:
        if token_value(index) in {"async", "function"}:
            return True
        if token_value(index) == "(":
            close = matching_close(index)
            return close is not None and token_value(close + 1) == "=>"
        return tokens[index][0] == "identifier" and token_value(index + 1) == "=>"

    def bundled_loader_wrapper(index: int) -> tuple[int, set[int]] | None:
        """Recognize the narrow esbuild dynamic-require forwarding shim.

        Esbuild emits a Proxy/IIFE that returns the ambient loader when it is
        available and otherwise forwards through ``require.apply``. Treat the
        resulting binding as a loader so literal calls remain visible, while
        accepting opaque calls without inventing a package edge.
        """

        if token_value(index) != "(":
            return None
        expected: list[str] = []
        closing_for = {"(": ")", "[": "]", "{": "}"}
        end = index
        while end < len(tokens):
            value = token_value(end)
            if value in closing_for:
                expected.append(closing_for[value])
            elif expected and value == expected[-1]:
                expected.pop()
            elif not expected and value in {";", ","}:
                break
            end += 1
        if end >= len(tokens) or token_value(end) not in {";", ","}:
            return None
        wrapper_tokens = tokens[index:end]
        values = {value for _kind, value in wrapper_tokens}
        if not {"Proxy", "function", "typeof"}.issubset(values) or not values.intersection(
            {"apply", "call"}
        ):
            return None
        references: set[int] = set()
        forwarded = False
        for cursor in range(index, end):
            if tokens[cursor][0] != "identifier":
                continue
            if capability_at(cursor, tokens[cursor][1]) != "loader":
                continue
            previous_value = token_value(cursor - 1)
            next_value = token_value(cursor + 1)
            if previous_value == "typeof":
                references.add(cursor)
                continue
            if (
                next_value in {".", "?."}
                and token_value(cursor + 2) in {"apply", "call"}
                and token_value(cursor + 3) == "("
            ):
                references.add(cursor)
                forwarded = True
                continue
            if previous_value in {"?", ":", "return"}:
                references.add(cursor)
                forwarded = True
                continue
            return None
        if not forwarded or not references:
            return None
        return end, references

    # Destructuring is the only assignment form whose LHS is not one name.
    for index, token in enumerate(tokens):
        if (
            token
            not in {
                ("identifier", "const"),
                ("identifier", "let"),
                ("identifier", "var"),
            }
            or token_value(index + 1) != "{"
        ):
            continue
        close = matching_close(index + 1, "{", "}")
        if close is None or token_value(close + 1) != "=":
            continue
        if node_module_value_end(close + 2) is None:
            continue
        cursor = index + 2
        while cursor < close:
            if tokens[cursor][0] != "identifier":
                cursor += 1
                continue
            imported = tokens[cursor][1]
            local_index = cursor
            local = imported
            if token_value(cursor + 1) == ":" and tokens[cursor + 2][0] == "identifier":
                local_index = cursor + 2
                local = tokens[local_index][1]
                cursor += 2
            if token[1] == "var":
                owner = scope_var_owner[owned_scope_at(local_index)]
                var_declared_names_by_scope.setdefault(owner, set()).add(local)
            if imported == "createRequire":
                set_capability(
                    local,
                    "factory",
                    local_index,
                    declaration_kind_override=token[1],
                )
            elif imported in {"Module", "default"}:
                set_capability(
                    local,
                    "namespace",
                    local_index,
                    declaration_kind_override=token[1],
                )
            cursor += 1

    uninitialized_bindings = {
        tokens[index + 1][1]: index + 1
        for index, token in enumerate(tokens[:-1])
        if token in {("identifier", "let"), ("identifier", "var")}
        and tokens[index + 1][0] == "identifier"
        and token_value(index + 2) in {",", ";", ""}
    }

    # Collect simple assignments, then propagate namespace/factory/loader
    # aliases through a dependency worklist. This supports forward-hoisted
    # imports and short alias chains while rejecting expression composition.
    for index, token in enumerate(tokens):
        if token[0] != "identifier" or token_value(index - 1) in {".", "?."}:
            continue
        initializer = assignment_initializer(index)
        if (
            initializer is None
            and token[1] in uninitialized_bindings
            and token_value(index + 1) == "??"
            and token_value(index + 2) == "="
        ):
            initializer = index + 3
        if initializer is not None:
            assignments.append((index, token[1], initializer))
    assignments_by_dependency: dict[str, list[int]] = {}
    for assignment_index, (_lhs_index, _name, initializer) in enumerate(assignments):
        dependency = initializer
        while token_value(dependency) == "(":
            dependency += 1
        if 0 <= dependency < len(tokens) and tokens[dependency][0] == "identifier":
            assignments_by_dependency.setdefault(tokens[dependency][1], []).append(assignment_index)

    pending_assignments = deque(range(len(assignments)))
    queued_assignments = set(pending_assignments)
    while pending_assignments:
        assignment_index = pending_assignments.popleft()
        queued_assignments.discard(assignment_index)
        lhs_index, name, initializer = assignments[assignment_index]
        capability = proven_rhs_capability(initializer)
        wrapper_references: set[int] = set()
        if capability is None and token_value(lhs_index - 1) in {"const", "let", "var"}:
            wrapper = bundled_loader_wrapper(initializer)
            if wrapper is not None:
                end, wrapper_references = wrapper
                capability = ("loader", end)
        if capability is None:
            continue
        capability_kind, end = capability
        dependency = initializer
        while token_value(dependency) == "(":
            dependency += 1
        if 0 <= dependency < len(tokens) and tokens[dependency][0] == "identifier":
            dependency_name = tokens[dependency][1]
            dependency_scope = owned_scope_at(dependency)
            dependency_binding = capability_bindings.get(dependency_name, {}).get(dependency_scope)
            if (
                dependency_binding is not None
                and dependency_binding[0] > lhs_index
                and dependency_binding[0] not in hoisted_capability_indices
            ):
                raise TypeScriptWorkerError(
                    f"Runtime package aliases module-loading capability {dependency_name!r} "
                    f"before its assignment; execution order is unsupported: {source_path}"
                )
        if not safe_rhs_suffix(end):
            raise TypeScriptWorkerError(
                f"Runtime package ambiguously composes module-loading capability {name!r}: "
                f"{source_path}"
            )
        changed = set_capability(name, capability_kind, lhs_index)
        safe_indices.update(range(initializer, end))
        safe_indices.update(wrapper_references)
        if changed:
            for dependent in assignments_by_dependency.get(name, ()):
                if dependent not in queued_assignments:
                    pending_assignments.append(dependent)
                    queued_assignments.add(dependent)
    safe_indices.update(
        binding_index
        for name, binding_index in uninitialized_bindings.items()
        if name in capability_names
    )

    for index, token in enumerate(tokens):
        if (
            token[0] == "identifier"
            and token[1] in parameter_property_capabilities
            and token_value(index - 1) in {".", "?."}
            and token_value(index - 2) == "this"
        ):
            raise TypeScriptWorkerError(
                f"Runtime package accesses parameter-property loader {token[1]!r}; "
                f"property-backed loaders are unsupported: {source_path}"
            )
        if (
            token[0] != "identifier"
            or token_value(index - 1) not in {".", "?."}
            or token_value(index + 1) != "="
        ):
            continue
        capability = proven_rhs_capability(index + 2)
        if capability is not None:
            raise TypeScriptWorkerError(
                f"Runtime package stores a module-loading capability in property {token[1]!r}; "
                f"property-backed loaders are unsupported: {source_path}"
            )

    parameter_modifiers = {
        "accessor",
        "override",
        "private",
        "protected",
        "public",
        "readonly",
    }
    for index, token in enumerate(tokens):
        if token[0] != "identifier" or token[1] not in capability_names or index in safe_indices:
            continue
        declaration_kind = simple_declaration_kinds[index]
        shadow_scope: int | None = None
        if declaration_kind is not None:
            shadow_scope = owned_scope_at(index)
            if declaration_kind == "var":
                shadow_scope = scope_var_owner[shadow_scope]
        elif token_value(index - 1) == "function" or (
            token_value(index - 1) == "*" and token_value(index - 2) == "function"
        ):
            shadow_scope = owned_scope_at(index)
        elif scope_prebody_at_token[index] >= 0:
            cursor = index - 1
            while token_value(cursor) in parameter_modifiers:
                cursor -= 1
            if token_value(cursor) in {"(", ","} or token_value(index + 1) in {
                ")",
                ",",
                ":",
                "=",
                "=>",
            }:
                shadow_scope = scope_prebody_at_token[index]
        if shadow_scope is None:
            continue
        bindings = capability_bindings.setdefault(token[1], {})
        existing = bindings.get(shadow_scope)
        if existing is not None and existing[1] != "shadow":
            raise TypeScriptWorkerError(
                f"Runtime package shadows module-loading capability {token[1]!r} within its "
                f"own lexical scope: {source_path}"
            )
        bindings[shadow_scope] = (index, "shadow")
        interval_index = capability_interval_indices.get(token[1])
        if interval_index is not None:
            interval_index.add(
                start=scope_open[shadow_scope],
                end=scope_end_exclusive[shadow_scope],
                capability="shadow",
            )

    # Any unknown reassignment of a proven capability invalidates the closure.
    for lhs_index, name, initializer in assignments:
        scoped_capability = capability_at(lhs_index, name)
        if scoped_capability in {None, "shadow"}:
            continue
        capability = proven_rhs_capability(initializer)
        if capability is None or capability[0] != scoped_capability:
            if token_value(lhs_index - 1) in {
                "const",
                "let",
                "static",
                "var",
            } or initializer_is_function(initializer):
                # The scope index tracks proven capabilities, not arbitrary
                # shadow bindings. A same-spelled inner declaration shadows
                # the outer capability; treating its calls as possible loader
                # calls is a safe over-approximation, while rejecting it would
                # break ordinary bundled code.
                safe_indices.add(lhs_index)
                continue
            raise TypeScriptWorkerError(
                f"Runtime package reassigns module-loading capability {name!r} to an "
                f"unproven value: {source_path}"
            )
        if lhs_index >= 2 and token_value(lhs_index - 1) in {"const", "let", "var"}:
            if token_value(lhs_index - 2) == "export":
                raise TypeScriptWorkerError(
                    f"Runtime package exports module-loading capability {name!r}: {source_path}"
                )

    def loader_call(index: int, name: str) -> bool:
        if token_value(index + 1) == "(":
            specifier = literal_call_argument(index + 1)
            if specifier is not None:
                specifiers.add(specifier)
            return True
        if (
            token_value(index + 1) in {".", "?."}
            and tokens[index + 2 : index + 3] == (("identifier", "resolve"),)
            and token_value(index + 3) == "("
        ):
            specifier = literal_call_argument(index + 3)
            if specifier is not None:
                specifiers.add(specifier)
            return True
        return False

    def loader_forward_call(index: int) -> bool:
        """Accept apply/call forwarding and retain a statically named argument."""

        if token_value(index + 1) not in {".", "?."} or token_value(index + 2) not in {
            "apply",
            "call",
        }:
            return False
        open_index = index + 3
        if token_value(open_index) != "(":
            return False
        close = matching_close(open_index)
        if close is None:
            raise TypeScriptWorkerError(
                f"Runtime package has an unterminated loader forwarding call: {source_path}"
            )
        cursor = open_index + 1
        depth = 0
        while cursor < close:
            value = token_value(cursor)
            if value in {"(", "[", "{"}:
                depth += 1
            elif value in {")", "]", "}"}:
                depth -= 1
            elif value == "," and depth == 0:
                argument = cursor + 1
                if token_value(index + 2) == "apply" and token_value(argument) == "[":
                    argument += 1
                specifier = literal_call_argument(argument - 1)
                if specifier is not None:
                    specifiers.add(specifier)
                break
            cursor += 1
        return True

    def inside_export_clause(index: int) -> bool:
        cursor = index - 1
        depth = 0
        while cursor >= 0 and token_value(cursor) != ";":
            value = token_value(cursor)
            if value == "}":
                depth += 1
            elif value == "{":
                if depth:
                    depth -= 1
                else:
                    return token_value(cursor - 1) == "export"
            cursor -= 1
        return False

    def factory_call(index: int, end: int, name: str) -> bool:
        if token_value(end) != "(":
            return False
        close = matching_close(end)
        if close is None:
            raise TypeScriptWorkerError(
                f"Runtime package has an unterminated createRequire call: {source_path}"
            )
        returned_loader = close + 1
        if token_value(returned_loader) == "(":
            specifier = literal_call_argument(returned_loader)
            if specifier is not None:
                specifiers.add(specifier)
            return True
        if (
            token_value(returned_loader) in {".", "?."}
            and tokens[returned_loader + 1 : returned_loader + 2] == (("identifier", "resolve"),)
            and token_value(returned_loader + 2) == "("
        ):
            specifier = literal_call_argument(returned_loader + 2)
            if specifier is not None:
                specifiers.add(specifier)
            return True
        return False

    def stored_in_map(index: int) -> str | None:
        """Return the local Map receiving a loader via ``.set``."""

        cursor = index - 1
        depth = 0
        while cursor >= 0 and token_value(cursor) != ";":
            value = token_value(cursor)
            if value == ")":
                depth += 1
            elif value == "(":
                if depth:
                    depth -= 1
                else:
                    if (
                        tokens[cursor - 1 : cursor] == (("identifier", "set"),)
                        and token_value(cursor - 2) in {".", "?."}
                        and tokens[cursor - 3][0] == "identifier"
                    ):
                        return tokens[cursor - 3][1]
                    return None
            cursor -= 1
        return None

    def inside_require_getter(index: int) -> bool:
        """Return whether a bare loader return is from a ``get require()`` body."""

        cursor = index - 1
        depth = 0
        block_open: int | None = None
        while cursor >= 0:
            value = token_value(cursor)
            if value == "}":
                depth += 1
            elif value == "{":
                if depth:
                    depth -= 1
                else:
                    block_open = cursor
                    break
            cursor -= 1
        if block_open is None or token_value(block_open - 1) != ")":
            return False
        cursor = block_open - 2
        depth = 1
        while cursor >= 0:
            value = token_value(cursor)
            if value == ")":
                depth += 1
            elif value == "(":
                depth -= 1
                if depth == 0:
                    return tokens[cursor - 1 : cursor] == (("identifier", "require"),) and tokens[
                        cursor - 2 : cursor - 1
                    ] == (("identifier", "get"),)
            cursor -= 1
        return False

    for index, token in enumerate(tokens):
        if token[0] != "identifier" or index in safe_indices:
            continue
        name = token[1]
        capability = capability_at(index, name)
        if capability in {None, "shadow"} or token_value(index - 1) in {".", "?."}:
            continue
        if token_value(index + 1) == ":" and token_value(index - 1) in {"{", ","}:
            # Object/destructuring property key, not a reference to the
            # same-spelled capability in the outer lexical scope.
            continue
        if token_value(index + 1) == ";" and token_value(index - 1) in {"{", ";"}:
            # Class field declaration (or an inert bare expression), not a
            # transfer of the capability.
            continue
        if capability == "loader":
            if loader_call(index, name):
                continue
            if token_value(index - 1) == "typeof":
                continue
            if loader_forward_call(index):
                continue
            if inside_export_clause(index):
                # Same-package export plumbing is runtime-selected. The source
                # package identity seals the local forwarding module while the
                # manifest supplies its conservative external dependency set.
                continue
            if token_value(index + 1) in {".", "?."} and token_value(index + 2) in {
                "cache",
                "extensions",
                "main",
                "resolve",
            }:
                # Node exposes these data properties on every require function.
                # Reading or mutating them does not resolve another package.
                continue
            map_name = stored_in_map(index)
            if map_name is not None:
                loader_maps.add(map_name)
                continue
            if token_value(index - 1) == "return" and inside_require_getter(index):
                # Vitest exposes a memoized Node-compatible ``require`` getter.
                # Calls through its local Map are scanned below when static.
                continue
            raise TypeScriptWorkerError(
                f"Runtime package passes or ambiguously uses loader alias {name!r}: {source_path}"
            )
        if capability == "factory":
            end = index + 1
            method_close = matching_close(end)
            if method_close is not None and token_value(method_close + 1) == "{":
                # A same-spelled class/object method is a declaration, not a
                # call of the imported factory.
                continue
            if factory_call(index, end, name):
                continue
            if token_value(end) == "(":
                # Creating or returning a loader does not itself resolve a
                # package. Direct aliases were propagated above; other uses
                # remain outside the static-load promise.
                continue
            raise TypeScriptWorkerError(
                f"Runtime package passes or ambiguously uses createRequire factory {name!r}: "
                f"{source_path}"
            )
        assert capability == "namespace"
        if token_value(index + 1) == "[":
            raise TypeScriptWorkerError(
                f"Runtime package uses computed access on Node module namespace {name!r}: "
                f"{source_path}"
            )
        factory_end = factory_reference_end(index)
        if factory_end is not None and factory_end > index + 1:
            if factory_call(index, factory_end, f"{name}.createRequire"):
                continue
            if token_value(factory_end) == "(":
                if matching_close(factory_end) is None:
                    raise TypeScriptWorkerError(
                        f"Runtime package has an unterminated createRequire call: {source_path}"
                    )
                # A proven factory-produced loader may be handed to another
                # same-package runtime component. No package is resolved at
                # creation; literal calls remain captured at direct call sites.
                continue
            raise TypeScriptWorkerError(
                f"Runtime package passes or ambiguously uses {name}.createRequire: {source_path}"
            )
        if token_value(index + 1) in {".", "?."}:
            # Other Node module namespace members do not confer a loader.
            continue
        raise TypeScriptWorkerError(
            f"Runtime package passes or ambiguously uses Node module namespace {name!r}: "
            f"{source_path}"
        )

    for index, token in enumerate(tokens):
        if token[0] != "identifier" or token[1] not in loader_maps:
            continue
        if (
            token_value(index + 1) not in {".", "?."}
            or tokens[index + 2 : index + 3] != (("identifier", "get"),)
            or token_value(index + 3) != "("
        ):
            continue
        get_close = matching_close(index + 3)
        if get_close is None:
            raise TypeScriptWorkerError(
                f"Runtime package has an unterminated loader cache lookup: {source_path}"
            )
        if token_value(get_close + 1) == "(":
            specifier = literal_call_argument(get_close + 1)
            if specifier is not None:
                specifiers.add(specifier)
        elif (
            token_value(get_close + 1) in {".", "?."}
            and tokens[get_close + 2 : get_close + 3] == (("identifier", "resolve"),)
            and token_value(get_close + 3) == "("
        ):
            specifier = literal_call_argument(get_close + 3)
            if specifier is not None:
                specifiers.add(specifier)

    # Direct ``require/import('node:module').createRequire(...)`` expressions
    # have no local capability name for the pass above to visit.
    for index in range(len(tokens)):
        direct_namespace_end = node_module_value_end(index)
        grouped_namespace = token_value(index) == "(" and token_value(index + 1) in {
            "await",
            "import",
            "module",
            "require",
        }
        if direct_namespace_end is None and not grouped_namespace:
            continue
        factory_end = factory_reference_end(index)
        if factory_end is not None:
            factory_call(index, factory_end, "Node module createRequire")
    return tuple(sorted(specifiers))


def _runtime_package_owner(specifier: str) -> str | None:
    """Return the installable owner of one bare Node package specifier."""

    if not specifier or specifier.startswith(
        (".", "/", "#", "data:", "file:", "http:", "https:", "node:")
    ):
        return None
    if "\\" in specifier:
        raise TypeScriptWorkerError(
            f"Runtime package uses an escaped or invalid module specifier: {specifier!r}"
        )
    if specifier.startswith("@"):
        parts = specifier.split("/")
        package = "/".join(parts[:2]) if len(parts) >= 2 else specifier
    else:
        package = specifier.split("/", 1)[0]
    if package in _NODE_BUILTIN_PACKAGES:
        return None
    parts = package.split("/")
    valid = (
        bool(package)
        and ":" not in package
        and all(part not in {"", ".", ".."} for part in parts)
        and (
            (package.startswith("@") and len(parts) == 2)
            or (not package.startswith("@") and len(parts) == 1)
        )
    )
    if not valid:
        raise TypeScriptWorkerError(f"Invalid runtime package specifier {specifier!r}")
    return package


def _erased_type_import_indices(
    tokens: Sequence[tuple[str, str]],
) -> frozenset[int]:
    """Find ``import(...)`` tokens used as TypeScript types in one pass."""

    token_count = len(tokens)
    type_body_contexts = getattr(tokens, "type_body_contexts", ())
    if len(type_body_contexts) != token_count:
        type_body_contexts = (False,) * token_count
    class_body_contexts = getattr(tokens, "class_body_contexts", ())
    if len(class_body_contexts) != token_count:
        class_body_contexts = (False,) * token_count
    line_breaks_before = getattr(tokens, "line_breaks_before", ())
    if len(line_breaks_before) != token_count:
        line_breaks_before = (False,) * token_count

    conditional_colons = _conditional_expression_colons(tokens)
    expected_closings: list[str] = []
    closing_for = {"(": ")", "[": "]", "{": "}"}
    pending_alias_depth: int | None = None
    alias_body_depth: int | None = None
    type_regions: dict[int, str] = {}
    erased: set[int] = set()
    declaration_starters = {
        "class",
        "const",
        "declare",
        "enum",
        "export",
        "function",
        "import",
        "interface",
        "let",
        "module",
        "namespace",
        "type",
        "var",
    }

    def token_value(index: int) -> str:
        return tokens[index][1] if 0 <= index < token_count else ""

    for index, (kind, value) in enumerate(tokens):
        depth = len(expected_closings)
        line_break_starts_statement = (
            bool(line_breaks_before[index])
            and kind == "identifier"
            and value in declaration_starters
            and index > 0
            and _can_end_statement_before_label(*tokens[index - 1])
        )
        if alias_body_depth == depth and line_break_starts_statement:
            alias_body_depth = None
        if alias_body_depth == depth and value == ";":
            alias_body_depth = None

        region_kind = type_regions.get(depth)
        if region_kind is not None:
            boundaries = (
                {"=", ",", ")", "]", "}", ";", "=>", "{"}
                if region_kind == "annotation"
                else {",", ")", "]", "}", ";", "=>", "?", ":", "{"}
            )
            if value in boundaries or line_break_starts_statement:
                type_regions.pop(depth)

        if (
            kind == "identifier"
            and value == "import"
            and (
                bool(type_body_contexts[index])
                or alias_body_depth is not None
                or bool(type_regions)
            )
        ):
            erased.add(index)

        if value == "=" and pending_alias_depth == depth:
            alias_body_depth = depth
            pending_alias_depth = None
        elif (
            kind == "identifier"
            and value == "type"
            and index + 1 < token_count
            and tokens[index + 1][0] == "identifier"
            and token_value(index - 1) not in {".", "?."}
        ):
            pending_alias_depth = depth
        elif (
            pending_alias_depth is not None
            and depth <= pending_alias_depth
            and value
            in {
                ";",
                "{",
                "}",
            }
        ):
            pending_alias_depth = None

        if value == ":" and index not in conditional_colons:
            previous = token_value(index - 1)
            binding_prefix = token_value(index - 2)
            inside_parentheses = bool(expected_closings) and expected_closings[-1] == ")"
            if (
                bool(type_body_contexts[index])
                or bool(class_body_contexts[index])
                or inside_parentheses
                or previous == ")"
                or binding_prefix in _TYPED_BINDING_PREFIXES
            ):
                type_regions[depth] = "annotation"
        elif (
            kind == "identifier"
            and value in {"as", "satisfies"}
            and token_value(index - 1) not in {".", "?."}
        ):
            type_regions[depth] = "assertion"

        if value in closing_for:
            expected_closings.append(closing_for[value])
        elif value in {")", "]", "}"} and expected_closings and value == expected_closings[-1]:
            expected_closings.pop()
    return frozenset(erased)


def _shadowed_native_loader_indices(
    tokens: Sequence[tuple[str, str]],
) -> frozenset[int]:
    """Return ``require``/``module`` identifiers shadowed by local bindings.

    The runtime dependency scanner intentionally recognizes ambient CommonJS
    loaders without requiring a full JavaScript parser.  Local bindings with
    the same names must nevertheless win.  This pass models balanced brace
    scopes and the common binding forms emitted by TypeScript and bundlers.
    """

    target_names = frozenset({"module", "require"})
    opening_for = {")": "(", "]": "[", "}": "{"}
    delimiter_stack: list[tuple[int, str]] = []
    matching_open: dict[int, int] = {}
    matching_close: dict[int, int] = {}
    for index, (_kind, value) in enumerate(tokens):
        if value in {"(", "[", "{"}:
            delimiter_stack.append((index, value))
        elif (
            value in opening_for
            and delimiter_stack
            and delimiter_stack[-1][1] == opening_for[value]
        ):
            open_index, _opening = delimiter_stack.pop()
            matching_open[index] = open_index
            matching_close[open_index] = index

    scope_parent = [-1]
    scope_at_token: list[int] = []
    scope_for_open: dict[int, int] = {}
    scope_stack = [0]
    for index, (_kind, value) in enumerate(tokens):
        scope_at_token.append(scope_stack[-1])
        if value == "{":
            scope_parent.append(scope_stack[-1])
            scope = len(scope_parent) - 1
            scope_for_open[index] = scope
            scope_stack.append(scope)
        elif value == "}" and len(scope_stack) > 1:
            scope_stack.pop()

    bindings: list[set[str]] = [set() for _scope in scope_parent]
    function_scopes: set[int] = set()
    expression_arrow_bindings: list[tuple[int, int, frozenset[str]]] = []

    def token_value(index: int) -> str:
        return tokens[index][1] if 0 <= index < len(tokens) else ""

    def pattern_names(start: int, end: int) -> set[str]:
        """Collect names bound by one parameter or destructuring pattern."""

        while start < end and token_value(start) in {
            "...",
            "accessor",
            "override",
            "private",
            "protected",
            "public",
            "readonly",
        }:
            start += 1
        if start >= end:
            return set()
        if tokens[start][0] == "identifier":
            return {tokens[start][1]} if tokens[start][1] in target_names else set()
        opening = token_value(start)
        closing = matching_close.get(start)
        if opening not in {"{", "["} or closing is None or closing >= end:
            return set()
        names: set[str] = set()
        segment_start = start + 1
        cursor = segment_start
        while cursor <= closing:
            at_end = cursor == closing
            if not at_end and token_value(cursor) in {"(", "[", "{"}:
                cursor = matching_close.get(cursor, cursor)
            if at_end or token_value(cursor) == ",":
                segment_end = cursor
                colon: int | None = None
                nested = segment_start
                while nested < segment_end:
                    if token_value(nested) in {"(", "[", "{"}:
                        nested = matching_close.get(nested, nested)
                    elif token_value(nested) == ":":
                        colon = nested
                        break
                    nested += 1
                binding_start = colon + 1 if colon is not None else segment_start
                names.update(pattern_names(binding_start, segment_end))
                segment_start = cursor + 1
            cursor += 1
        return names

    def parameter_names(open_index: int, close_index: int) -> set[str]:
        names: set[str] = set()
        segment_start = open_index + 1
        cursor = segment_start
        while cursor <= close_index:
            at_end = cursor == close_index
            if not at_end and token_value(cursor) in {"(", "[", "{"}:
                cursor = matching_close.get(cursor, cursor)
            if at_end or token_value(cursor) == ",":
                names.update(pattern_names(segment_start, cursor))
                segment_start = cursor + 1
            cursor += 1
        return names

    def bind_function_body(open_index: int, close_index: int, body_open: int) -> None:
        scope = scope_for_open.get(body_open)
        if scope is None:
            return
        function_scopes.add(scope)
        bindings[scope].update(parameter_names(open_index, close_index))

    # Function, method, catch, and braced-arrow parameters belong to the body
    # scope.  Control-flow heads other than ``catch`` are not bindings.
    for close_index, open_index in matching_open.items():
        if token_value(close_index) != ")":
            continue
        body_open: int | None = None
        if token_value(close_index + 1) == "{":
            body_open = close_index + 1
        elif token_value(close_index + 1) == "=>" and token_value(close_index + 2) == "{":
            body_open = close_index + 2
        if body_open is None:
            continue
        control_head = _control_flow_parenthesis_head(tokens, end=open_index)
        if control_head is not None and control_head != "catch":
            continue
        if control_head == "catch":
            scope = scope_for_open.get(body_open)
            if scope is not None:
                bindings[scope].update(parameter_names(open_index, close_index))
        else:
            bind_function_body(open_index, close_index, body_open)

    # A TypeScript return annotation may sit between the parameter close and
    # body brace. Find that parameter group without treating control heads as
    # function scopes.
    annotated_function_candidates: set[int] = set()
    for body_open, scope in scope_for_open.items():
        if scope in function_scopes:
            continue
        cursor = body_open - 1
        if token_value(cursor) == "=>":
            cursor -= 1
        while cursor >= 0 and token_value(cursor) not in {";", "{", "}", "=", ","}:
            if token_value(cursor) == ")":
                open_index = matching_open.get(cursor)
                if (
                    open_index is not None
                    and _control_flow_parenthesis_head(tokens, end=open_index) is None
                ):
                    function_scopes.add(scope)
                    bindings[scope].update(parameter_names(open_index, cursor))
                    if any(token_value(index) == ":" for index in range(cursor + 1, body_open)):
                        annotated_function_candidates.add(scope)
                break
            cursor -= 1

    # An object-shaped return annotation has its own brace before the actual
    # body. Transfer the candidate function scope to the following brace.
    for body_open, scope in scope_for_open.items():
        previous_close = body_open - 1
        annotation_open = matching_open.get(previous_close)
        if annotation_open is None:
            continue
        annotation_scope = scope_for_open.get(annotation_open)
        if annotation_scope not in annotated_function_candidates:
            continue
        function_scopes.discard(annotation_scope)
        function_scopes.add(scope)
        bindings[scope].update(bindings[annotation_scope])
        bindings[annotation_scope].difference_update(target_names)

    # Single-parameter braced arrows do not have a parenthesized head.
    for index, token in enumerate(tokens):
        if token != ("punctuation", "=>") or index == 0:
            continue
        arrow_names: set[str] = set()
        if tokens[index - 1][0] == "identifier":
            if tokens[index - 1][1] in target_names:
                arrow_names.add(tokens[index - 1][1])
        elif token_value(index - 1) == ")":
            open_index = matching_open.get(index - 1)
            if open_index is not None:
                arrow_names.update(parameter_names(open_index, index - 1))
        if not arrow_names and token_value(index - 1) != ")":
            cursor = index - 1
            while cursor >= 0 and token_value(cursor) not in {";", "{", "}", "=", ","}:
                if token_value(cursor) == ")":
                    open_index = matching_open.get(cursor)
                    if open_index is not None:
                        arrow_names.update(parameter_names(open_index, cursor))
                    break
                cursor -= 1
        if token_value(index + 1) == "{":
            scope = scope_for_open.get(index + 1)
            if scope is not None:
                function_scopes.add(scope)
                bindings[scope].update(arrow_names)
            continue
        if not arrow_names:
            continue
        end = index + 1
        while end < len(tokens) and token_value(end) not in {",", ";", ")", "]", "}"}:
            if token_value(end) in {"(", "[", "{"}:
                end = matching_close.get(end, end)
            end += 1
        expression_arrow_bindings.append((index + 1, end, frozenset(arrow_names)))

    for body_open, scope in scope_for_open.items():
        if token_value(body_open - 1) == "static":
            function_scopes.add(scope)

    # Function declarations and named function expressions bind their name in
    # the containing scope and body respectively.
    for index, token in enumerate(tokens):
        if token != ("identifier", "function"):
            continue
        cursor = index + 1
        if token_value(cursor) == "*":
            cursor += 1
        name_index = cursor if cursor < len(tokens) and tokens[cursor][0] == "identifier" else None
        if name_index is None or tokens[name_index][1] not in target_names:
            continue
        previous = token_value(index - 1)
        is_declaration = index == 0 or previous in {
            ";",
            "{",
            "}",
            "async",
            "declare",
            "default",
            "export",
        }
        if is_declaration:
            bindings[scope_at_token[index]].add(tokens[name_index][1])
        while cursor < len(tokens) and token_value(cursor) != "(":
            cursor += 1
        close_index = matching_close.get(cursor)
        body_open = None if close_index is None else close_index + 1
        if body_open is None or token_value(body_open) != "{":
            continue
        body_scope = scope_for_open.get(body_open)
        if body_scope is not None:
            bindings[body_scope].add(tokens[name_index][1])

    for index, token in enumerate(tokens):
        if token != ("identifier", "class") or tokens[index + 1 : index + 2] == ():
            continue
        name_index = index + 1
        if tokens[name_index][0] != "identifier" or tokens[name_index][1] not in target_names:
            continue
        body_open = name_index + 1
        while body_open < len(tokens) and token_value(body_open) != "{":
            body_open += 1
        body_scope = scope_for_open.get(body_open)
        if body_scope is not None:
            bindings[body_scope].add(tokens[name_index][1])
        previous = token_value(index - 1)
        if index == 0 or previous in {
            ";",
            "{",
            "}",
            "abstract",
            "declare",
            "default",
            "export",
        }:
            bindings[scope_at_token[index]].add(tokens[name_index][1])

    # Direct lexical declarations cover the overwhelmingly common shadowing
    # forms.  ``var`` hoists to the nearest function/static-block scope.
    declaration_heads = {"const", "let", "using", "var"}
    token_line_breaks = getattr(tokens, "line_breaks_before", ())
    for index, (kind, value) in enumerate(tokens):
        if (
            kind != "identifier"
            or value not in declaration_heads
            or token_value(index - 1) in {"#", ".", "?."}
            or not (
                tokens[index + 1 : index + 2]
                and (tokens[index + 1][0] == "identifier" or token_value(index + 1) in {"{", "["})
            )
        ):
            continue
        scope = scope_at_token[index]
        if value == "var":
            while scope != 0 and scope not in function_scopes:
                scope = scope_parent[scope]
        binding_start = index + 1
        while binding_start < len(tokens):
            binding_end = binding_start + 1
            if token_value(binding_start) in {"{", "["}:
                binding_end = matching_close.get(binding_start, binding_start) + 1
            bindings[scope].update(pattern_names(binding_start, binding_end))
            cursor = binding_end
            in_type_annotation = token_value(cursor) == ":"
            angle_depth = 0
            while cursor < len(tokens):
                if (
                    cursor > binding_end
                    and tokens[cursor][0] == "identifier"
                    and token_value(cursor)
                    in {
                        "class",
                        "const",
                        "declare",
                        "enum",
                        "export",
                        "function",
                        "import",
                        "interface",
                        "let",
                        "module",
                        "namespace",
                        "type",
                        "var",
                    }
                    and _can_end_statement_before_block(*tokens[cursor - 1])
                ):
                    cursor = len(tokens)
                    break
                if token_value(cursor) in {"(", "[", "{"}:
                    cursor = matching_close.get(cursor, cursor) + 1
                    continue
                if in_type_annotation and token_value(cursor) == "<":
                    angle_depth += 1
                elif in_type_annotation and token_value(cursor) == ">" and angle_depth:
                    angle_depth -= 1
                elif token_value(cursor) == "=" and angle_depth == 0:
                    in_type_annotation = False
                if token_value(cursor) in {";", "in", "of"}:
                    cursor = len(tokens)
                    break
                if token_value(cursor) == "," and angle_depth == 0:
                    binding_start = cursor + 1
                    break
                if (
                    len(token_line_breaks) == len(tokens)
                    and token_line_breaks[cursor]
                    and cursor > binding_end
                    and _can_end_statement_before_block(*tokens[cursor - 1])
                ):
                    cursor = len(tokens)
                    break
                cursor += 1
            else:
                cursor = len(tokens)
            if cursor >= len(tokens):
                break

    # Static imports introduce local bindings for the whole module. Advance
    # over each clause once so semicolonless imports cannot trigger rescans.
    index = 0
    while index < len(tokens):
        if tokens[index] != ("identifier", "import") or token_value(index + 1) in {
            "(",
            ".",
            "?.",
        }:
            index += 1
            continue
        if tokens[index + 1 : index + 2] and tokens[index + 1][0] in {
            "string",
            "template",
        }:
            index += 2
            continue
        cursor = index + 1
        while cursor < len(tokens):
            if tokens[cursor][0] == "identifier" and tokens[cursor][1] in target_names:
                previous = token_value(cursor - 1)
                following = token_value(cursor + 1)
                if previous == "as" or following in {",", "from", "=", "}"}:
                    bindings[0].add(tokens[cursor][1])
            if token_value(cursor) in {";", "from", "="}:
                cursor += 1
                break
            cursor += 1
        index = max(index + 1, cursor)

    effective_bindings: list[frozenset[str]] = []
    for scope, parent in enumerate(scope_parent):
        inherited = frozenset() if parent < 0 else effective_bindings[parent]
        effective_bindings.append(inherited | bindings[scope])

    arrow_deltas = {name: [0] * (len(tokens) + 1) for name in target_names}
    for start, end, names in expression_arrow_bindings:
        for name in names:
            arrow_deltas[name][start] += 1
            arrow_deltas[name][end] -= 1

    active_arrow_bindings = {name: 0 for name in target_names}
    shadowed: set[int] = set()
    for index, (kind, value) in enumerate(tokens):
        for name in target_names:
            active_arrow_bindings[name] += arrow_deltas[name][index]
        if kind != "identifier" or value not in target_names:
            continue
        scope = scope_at_token[index]
        if value in effective_bindings[scope] or active_arrow_bindings[value]:
            shadowed.add(index)
    return frozenset(shadowed)


def _runtime_module_specifiers(source: str, *, source_path: Path) -> tuple[str, ...]:
    """Extract statically named native ESM and CommonJS runtime loads."""

    tokens = _runtime_javascript_tokens(source, source_path=source_path)
    specifiers: set[str] = set()
    erased_type_imports = _erased_type_import_indices(tokens)
    shadowed_native_loaders = _shadowed_native_loader_indices(tokens)

    def literal(index: int) -> str | None:
        if index >= len(tokens):
            return None
        kind, value = tokens[index]
        if kind not in {"string", "template"}:
            return None
        if "\\" in value:
            raise TypeScriptWorkerError(
                f"Runtime package uses an escaped module specifier in {source_path}"
            )
        return value

    def named_clause_is_type_only(open_index: int, close_index: int) -> bool:
        """Return whether every named import/export carries an inline type modifier."""

        segment: list[tuple[str, str]] = []
        saw_segment = False
        for cursor in range(open_index + 1, close_index + 1):
            token = tokens[cursor] if cursor < close_index else ("punctuation", ",")
            if token == ("punctuation", ","):
                if segment and not (
                    len(segment) >= 2
                    and segment[0] == ("identifier", "type")
                    and segment[1] != ("identifier", "as")
                ):
                    return False
                saw_segment = saw_segment or bool(segment)
                segment = []
            else:
                segment.append(token)
        return saw_segment

    def clause_is_type_only(start: int, end: int) -> bool:
        """Disambiguate TypeScript type modifiers from a binding named ``type``."""

        if start >= end:
            return False
        if tokens[start] == ("identifier", "type"):
            following = tokens[start + 1] if start + 1 <= end else None
            if following not in {
                ("identifier", "from"),
                ("punctuation", ","),
                ("punctuation", "="),
            }:
                return True
        if tokens[start] != ("punctuation", "{"):
            return False
        depth = 1
        close = start + 1
        while close < end and depth:
            if tokens[close] == ("punctuation", "{"):
                depth += 1
            elif tokens[close] == ("punctuation", "}"):
                depth -= 1
            close += 1
        return depth == 0 and close == end and named_clause_is_type_only(start, close - 1)

    def static_export_clause(index: int) -> tuple[int, int, str] | None:
        """Return one syntax-bounded re-export clause, if present."""

        clause_start = index + 1
        body_start = clause_start
        if body_start < len(tokens) and tokens[body_start] == ("identifier", "type"):
            body_start += 1
        if body_start >= len(tokens):
            return None
        if tokens[body_start] == ("punctuation", "{"):
            depth = 1
            cursor = body_start + 1
            while cursor < len(tokens) and depth:
                if tokens[cursor] == ("punctuation", "{"):
                    depth += 1
                elif tokens[cursor] == ("punctuation", "}"):
                    depth -= 1
                cursor += 1
            if depth:
                return None
            from_index = cursor
        elif tokens[body_start] == ("punctuation", "*"):
            from_index = body_start + 1
            if (
                from_index + 1 < len(tokens)
                and tokens[from_index] == ("identifier", "as")
                and tokens[from_index + 1][0] == "identifier"
            ):
                from_index += 2
        else:
            # ``export const``, declarations, assignments, and object keys
            # cannot introduce a package re-export.
            return None
        if from_index + 1 < len(tokens) and tokens[from_index] == ("identifier", "from"):
            specifier = literal(from_index + 1)
            if specifier is not None:
                return clause_start, from_index, specifier
        return None

    for index, (kind, value) in enumerate(tokens):
        if kind != "identifier":
            continue
        previous = tokens[index - 1] if index else None
        if value == "import" and previous not in {
            ("punctuation", "."),
            ("punctuation", "?."),
            ("punctuation", "#"),
        }:
            if index + 1 < len(tokens) and tokens[index + 1] == ("punctuation", "("):
                if index in erased_type_imports:
                    continue
                specifier = literal(index + 2)
                if specifier is not None:
                    specifiers.add(specifier)
                continue
            specifier = literal(index + 1)
            if specifier is not None:
                specifiers.add(specifier)
                continue
            for cursor in range(index + 1, len(tokens) - 1):
                if tokens[cursor] == ("punctuation", "="):
                    break
                if tokens[cursor] == ("identifier", "from"):
                    specifier = literal(cursor + 1)
                    if specifier is not None and not clause_is_type_only(index + 1, cursor):
                        specifiers.add(specifier)
                    break
                if tokens[cursor] == ("punctuation", ";"):
                    break
        elif value == "export" and previous not in {
            ("punctuation", "."),
            ("punctuation", "?."),
        }:
            clause = static_export_clause(index)
            if clause is not None:
                clause_start, from_index, specifier = clause
                if not clause_is_type_only(clause_start, from_index):
                    specifiers.add(specifier)
        elif value == "require":
            if _type_only_import_assignment_require(tokens, index):
                continue
            call_index = index + 1
            if previous == ("punctuation", "#"):
                continue
            runtime_import_assignment = (
                index >= 3
                and tokens[index - 1] == ("punctuation", "=")
                and tokens[index - 3] == ("identifier", "import")
            )
            member_access = previous in {
                ("punctuation", "."),
                ("punctuation", "?."),
            }
            is_native_loader = not member_access and (
                runtime_import_assignment or index not in shadowed_native_loaders
            )
            if member_access:
                owner_index = index - 2
                owner = tokens[owner_index] if owner_index >= 0 else None
                is_native_loader = (
                    owner == ("identifier", "module")
                    and (owner_index == 0 or tokens[owner_index - 1][1] not in {"#", ".", "?."})
                    and owner_index not in shadowed_native_loaders
                )
            if not is_native_loader:
                continue
            if (
                call_index + 2 < len(tokens)
                and tokens[call_index][1] in {".", "?."}
                and tokens[call_index + 1] == ("identifier", "resolve")
            ):
                call_index += 2
            if (
                call_index + 1 < len(tokens)
                and tokens[call_index] == ("punctuation", "?.")
                and tokens[call_index + 1] == ("punctuation", "(")
            ):
                call_index += 1
            if call_index < len(tokens) and tokens[call_index] == ("punctuation", "("):
                specifier = literal(call_index + 1)
                if specifier is not None:
                    specifiers.add(specifier)
    specifiers.update(
        _create_require_module_specifiers(
            tokens,
            source_path=source_path,
        )
    )
    return tuple(sorted(specifiers))


def _runtime_package_import_match(
    imports: Mapping[str, object],
    specifier: str,
) -> tuple[object, str | None, bool] | None:
    """Match one ``#imports`` key using Node's package-pattern precedence."""

    if specifier in imports:
        return imports[specifier], None, False
    patterns = [
        key
        for key in imports
        if isinstance(key, str)
        and (
            (key.endswith("/") and specifier.startswith(key))
            or (
                key.count("*") == 1
                and specifier.startswith(key.partition("*")[0])
                and specifier.endswith(key.partition("*")[2])
                and len(specifier) >= len(key) - 1
            )
        )
    ]

    def precedence(key: str) -> tuple[int, int, int, str]:
        star = key.find("*")
        base_length = len(key) if star < 0 else star + 1
        return (-base_length, star < 0, -len(key), key)

    patterns.sort(key=precedence)
    if not patterns:
        return None
    key = patterns[0]
    star = key.find("*")
    if star < 0:
        return imports[key], specifier[len(key) :], True
    suffix_length = len(key) - star - 1
    wildcard_end = len(specifier) - suffix_length if suffix_length else len(specifier)
    return imports[key], specifier[star:wildcard_end], False


def _unsafe_runtime_package_import_fragment(value: str) -> bool:
    """Return whether a package target contains an encoded or lexical escape."""

    if "%2f" in value.casefold() or "%5c" in value.casefold():
        return True
    percent = 0
    while True:
        percent = value.find("%", percent)
        if percent < 0:
            break
        if percent + 2 >= len(value) or any(
            character not in "0123456789abcdefABCDEF"
            for character in value[percent + 1 : percent + 3]
        ):
            return True
        percent += 3
    decoded = unquote(value).replace("\\", "/")
    return any(
        segment.casefold() in {"", ".", "..", "node_modules"} for segment in decoded.split("/")
    )


def _runtime_package_scope(package_root: Path, source_path: Path) -> Path:
    """Return the nearest package scope owning a shipped runtime source."""

    physical_root = package_root.resolve(strict=True)
    current = source_path.resolve(strict=True).parent
    while current == physical_root or physical_root in current.parents:
        if (current / "package.json").is_file():
            return current
        if current == physical_root:
            break
        current = current.parent
    raise TypeScriptWorkerError(f"Runtime package source has no owning package.json: {source_path}")


def _runtime_package_import_targets(
    package_root: Path,
    specifier: str,
) -> tuple[str, ...]:
    """Resolve external package owners reachable through one ``#imports`` alias.

    Every conditional and array branch is retained because Node custom
    conditions can select a different branch at process startup. Package-local
    and self targets are already covered by the owning package's full runtime
    identity, so only external owners become closure edges.
    """

    manifest_path = package_root / "package.json"
    manifest_bytes = _stable_bytes(manifest_path, label="runtime package.json")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TypeScriptWorkerError(
            f"Could not parse runtime package.json at {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise TypeScriptWorkerError(
            f"Invalid runtime package.json: expected an object at {manifest_path}"
        )
    imports = manifest.get("imports")
    if not isinstance(imports, Mapping):
        raise TypeScriptWorkerError(
            f"Runtime package import {specifier!r} has no imports mapping in {manifest_path}"
        )
    own_name = manifest.get("name")
    own_package = own_name if isinstance(own_name, str) else None
    packages: set[str] = set()

    def fail(reason: str) -> NoReturn:
        raise TypeScriptWorkerError(
            f"Invalid runtime package import {specifier!r} in {manifest_path}: {reason}"
        )

    def resolve_alias(alias: str, seen: frozenset[str]) -> None:
        if alias in seen:
            fail(f"cyclic alias through {alias!r}")
        if alias == "#" or alias.endswith("/"):
            fail(f"invalid alias {alias!r}")
        match = _runtime_package_import_match(imports, alias)
        if match is None:
            fail(f"unresolved alias {alias!r}")
        else:
            target, wildcard, append_subpath = match
        next_seen = seen | {alias}

        def visit(value: object) -> None:
            if value is None:
                return
            if isinstance(value, str):
                if append_subpath and not value.endswith("/"):
                    fail("trailing-slash mapping has a non-directory target")
                resolved = value
                if wildcard is not None:
                    if _unsafe_runtime_package_import_fragment(wildcard):
                        fail(f"unsafe wildcard match {wildcard!r}")
                    resolved = (
                        f"{value}{wildcard}" if append_subpath else value.replace("*", wildcard)
                    )
                elif "*" in value:
                    fail(f"target {value!r} uses a wildcard without a pattern key")
                if resolved.startswith("#"):
                    resolve_alias(resolved, next_seen)
                    return
                if resolved.startswith("./"):
                    if _unsafe_runtime_package_import_fragment(resolved[2:]):
                        fail(f"unsafe package-relative target {resolved!r}")
                    target_path = Path(os.path.abspath(package_root / resolved))
                    physical_root = Path(os.path.abspath(package_root))
                    if target_path != physical_root and physical_root not in target_path.parents:
                        fail(f"package-relative target escapes its package: {resolved!r}")
                    return
                package = _runtime_package_owner(resolved)
                if package is None:
                    is_builtin = resolved.startswith("node:") or resolved.split("/", 1)[0] in (
                        _NODE_BUILTIN_PACKAGES
                    )
                    if not is_builtin:
                        fail(f"invalid external target {resolved!r}")
                    return
                subpath = resolved[len(package) :]
                if subpath and (
                    not subpath.startswith("/")
                    or _unsafe_runtime_package_import_fragment(subpath[1:])
                ):
                    fail(f"unsafe external target {resolved!r}")
                if package != own_package:
                    packages.add(package)
                return
            if isinstance(value, list):
                for entry in value:
                    visit(entry)
                return
            if isinstance(value, Mapping):
                for condition, entry in value.items():
                    if not isinstance(condition, str) or (
                        condition.isdigit()
                        and str(int(condition)) == condition
                        and int(condition) < 2**32 - 1
                    ):
                        fail(f"invalid condition key {condition!r}")
                    visit(entry)
                return
            fail(f"non-string target {value!r}")

        visit(target)

    resolve_alias(specifier, frozenset())
    if manifest_bytes != _stable_bytes(manifest_path, label="runtime package.json"):
        raise TypeScriptWorkerError(
            f"Runtime package imports changed while they were read: {package_root}"
        )
    return tuple(sorted(packages))


def _runtime_package_static_dependencies(
    package_root: Path,
) -> tuple[tuple[Path, str], ...]:
    """Return statically executable bare package loads from shipped sources."""

    physical_root = package_root.resolve(strict=True)
    dependencies: set[tuple[Path, str]] = set()
    for path in _runtime_package_identity_files(physical_root):
        relative_name = path.relative_to(physical_root).as_posix()
        if relative_name.endswith(_RUNTIME_DECLARATION_SUFFIXES):
            continue
        content = _stable_bytes(path, label="runtime package source")
        is_javascript = path.suffix.casefold() in _RUNTIME_JAVASCRIPT_SUFFIXES
        is_node_script = not path.suffix and content.startswith(b"#!") and b"node" in content[:256]
        if not is_javascript and not is_node_script:
            continue
        try:
            source = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TypeScriptWorkerError(
                f"Could not decode runtime package source at {path}: {exc}"
            ) from exc
        for specifier in _runtime_module_specifiers(source, source_path=path):
            if specifier.startswith("#"):
                scope = _runtime_package_scope(physical_root, path)
                for package in _runtime_package_import_targets(scope, specifier):
                    dependencies.add((path, package))
                continue
            package = _runtime_package_owner(specifier)
            if package is not None:
                dependencies.add((path, package))
    return tuple(
        sorted(
            dependencies,
            key=lambda item: (item[1], item[0].relative_to(physical_root).as_posix()),
        )
    )


def _runtime_package_dependencies(package_root: Path) -> tuple[tuple[str, bool], ...]:
    """Return declared runtime dependency names and whether each is required."""

    try:
        physical_root = package_root.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {package_root}: {exc}"
        ) from exc
    manifest_path = physical_root / "package.json"
    manifest_bytes = _stable_bytes(manifest_path, label="runtime package.json")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TypeScriptWorkerError(
            f"Could not parse runtime package.json at {manifest_path}: {exc}"
        ) from exc
    if not isinstance(manifest, Mapping):
        raise TypeScriptWorkerError(
            f"Invalid runtime package.json: expected an object at {manifest_path}"
        )

    requirements: dict[str, bool] = {}

    def dependency_map(field: str) -> Mapping[str, object]:
        value = manifest.get(field)
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise TypeScriptWorkerError(
                f"Invalid {field!r} in runtime package.json at {manifest_path}"
            )
        return value

    dependencies = dependency_map("dependencies")
    optional_dependencies = dependency_map("optionalDependencies")
    peer_dependencies = dependency_map("peerDependencies")
    for name in dependencies:
        requirements[str(name)] = name not in optional_dependencies
    for name in optional_dependencies:
        requirements.setdefault(str(name), False)
    for name in peer_dependencies:
        # Peer packages are selected by the install topology rather than this
        # package. Pin them when present and pin their absence otherwise.
        requirements.setdefault(str(name), False)

    for name in requirements:
        parts = name.split("/")
        valid = (
            bool(name)
            and "\\" not in name
            and ":" not in name
            and all(part not in {"", ".", ".."} for part in parts)
            and (
                (name.startswith("@") and len(parts) == 2)
                or (not name.startswith("@") and len(parts) == 1)
            )
        )
        if not valid:
            raise TypeScriptWorkerError(
                f"Invalid runtime dependency name {name!r} in {manifest_path}"
            )

    if manifest_bytes != _stable_bytes(manifest_path, label="runtime package.json"):
        raise TypeScriptWorkerError(
            f"Runtime package dependencies changed while they were read: {package_root}"
        )
    return tuple(sorted(requirements.items()))


def _runtime_package_dependency_edges(
    package_root: Path,
) -> tuple[_RuntimePackageDependencyEdge, ...]:
    """Merge manifest declarations with actual static runtime package loads.

    A static edge keeps its importing file so Node resolution starts from the
    same physical location execution would use (important for pnpm stores).
    Declared dependencies that are not statically visible remain conservative
    manifest-root edges. Optional/peer declarations keep their optionality.
    """

    physical_root = package_root.resolve(strict=True)
    declared = dict(_runtime_package_dependencies(physical_root))
    static_dependencies = _runtime_package_static_dependencies(physical_root)
    static_names = {package for _importer, package in static_dependencies}
    raw_edges = [
        # A package may ship tests/examples that are not part of its executed
        # entry graph. Undeclared static names are therefore optional closure
        # candidates: pin them when present and pin their absence otherwise.
        (importer, package, declared.get(package, False))
        for importer, package in static_dependencies
    ]
    manifest_path = physical_root / "package.json"
    raw_edges.extend(
        (manifest_path, package, required)
        for package, required in declared.items()
        if package not in static_names
    )
    counts: dict[str, int] = {}
    for _importer, package, _required in raw_edges:
        counts[package] = counts.get(package, 0) + 1
    edges: list[_RuntimePackageDependencyEdge] = []
    for importer, package, required in sorted(
        raw_edges,
        key=lambda item: (item[1], item[0].relative_to(physical_root).as_posix()),
    ):
        relative = importer.relative_to(physical_root).as_posix()
        key = package if counts[package] == 1 else f"{package}@{relative}"
        edges.append(
            _RuntimePackageDependencyEdge(
                key=key,
                importer=importer,
                package=package,
                required=required,
            )
        )
    return tuple(edges)


def resolve_node_package(
    start: Path,
    package: str,
    *,
    boundary: Path | None = None,
    module_path: bool = False,
) -> Path | None:
    """Resolve one package with Node's parent-search topology.

    Package-owner lookup is lexical so package-manager symlinks remain visible.
    A module-origin lookup starts at the module's physical location, matching
    Node's default behavior when ``--preserve-symlinks`` is not enabled.
    """

    package_path = Path(*package.split("/"))
    try:
        current = start.resolve(strict=True).parent if module_path else Path(os.path.abspath(start))
        resolved_boundary = Path(os.path.abspath(boundary)) if boundary is not None else None
    except OSError as exc:
        raise TypeScriptWorkerError(f"Could not resolve package search context at {start}") from exc
    if resolved_boundary is not None and (
        current != resolved_boundary and resolved_boundary not in current.parents
    ):
        raise TypeScriptWorkerError(
            f"Package search context {current} escapes its boundary {resolved_boundary}"
        )
    while True:
        candidate = current / "node_modules" / package_path
        if (candidate / "package.json").is_file():
            return candidate
        if current.parent == current or current == resolved_boundary:
            return None
        current = current.parent


def _runtime_package_resolution_closure(
    package_root: Path,
    *,
    root_label: str,
) -> tuple[_RuntimePackageResolutionEdge, ...]:
    """Resolve the exact recursive runtime graph shared by fingerprints and seals."""

    pending = [(root_label, Path(os.path.abspath(package_root)))]
    expanded: set[Path] = set()
    closure: list[_RuntimePackageResolutionEdge] = []
    while pending:
        label, current_root = pending.pop(0)
        try:
            physical_root = current_root.resolve(strict=True)
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not resolve runtime dependency closure at {current_root}: {exc}"
            ) from exc
        if physical_root in expanded:
            continue
        expanded.add(physical_root)
        for edge in _runtime_package_dependency_edges(physical_root):
            dependency_label = f"{label}>{edge.key}"
            resolved = resolve_node_package(
                edge.importer,
                edge.package,
                module_path=True,
            )
            resolution = _RuntimePackageResolutionEdge(
                label=dependency_label,
                importer=edge.importer,
                package=edge.package,
                required=edge.required,
                resolved_root=resolved,
            )
            closure.append(resolution)
            if resolved is None:
                if edge.required:
                    raise TypeScriptWorkerError(
                        f"Required runtime dependency {edge.package!r} loaded by "
                        f"{edge.importer} is not installed"
                    )
                continue
            pending.append((dependency_label, resolved))
    return tuple(closure)


def _compiler_package_root(compiler: Path) -> Path | None:
    lexical = Path(os.path.abspath(compiler))
    if lexical.parent.name != "lib" or lexical.name != "typescript.js":
        return None
    package_root = lexical.parent.parent
    if not (package_root / "package.json").is_file():
        return None
    return package_root


def compiler_runtime_identity(installation: WorkerInstallation) -> str:
    """Return the portable identity of the compiler actually given to the worker."""

    compiler = installation.compiler_module_path
    package_root = _compiler_package_root(compiler)
    if package_root is not None:
        return runtime_package_identity(package_root, expected_name="typescript")
    content = _stable_bytes(compiler, label="TypeScript compiler module")
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def compiler_session_identity(installation: WorkerInstallation) -> str:
    """Return the command-local filesystem identity of the selected compiler."""

    compiler = Path(os.path.abspath(installation.compiler_module_path))
    package_root = _compiler_package_root(compiler)
    if package_root is not None:
        return runtime_package_session_identity(package_root, expected_name="typescript")
    try:
        before = compiler.stat()
        content_identity = compiler_runtime_identity(installation)
        after = compiler.stat()
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not inspect TypeScript compiler module at {compiler}: {exc}"
        ) from exc
    before_epoch = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    after_epoch = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    if before_epoch != after_epoch:
        raise TypeScriptWorkerError(
            f"TypeScript compiler changed while its session identity was read: {compiler}"
        )
    encoded = json.dumps(
        {
            "format": "typescript-compiler-session/1",
            "contentIdentity": content_identity,
            "file": before_epoch,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _stable_bytes(path: Path, *, label: str) -> bytes:
    """Read one identity input while rejecting a concurrent replacement."""

    try:
        before = path.stat()
        content = path.read_bytes()
        after = path.stat()
    except OSError as exc:
        raise TypeScriptWorkerError(f"Could not read {label} at {path}: {exc}") from exc
    before_identity = (
        before.st_dev,
        before.st_ino,
        before.st_mode,
        before.st_size,
        before.st_mtime_ns,
    )
    after_identity = (
        after.st_dev,
        after.st_ino,
        after.st_mode,
        after.st_size,
        after.st_mtime_ns,
    )
    if before_identity != after_identity or len(content) != after.st_size:
        raise TypeScriptWorkerError(
            f"{label} changed while its freshness identity was read: {path}"
        )
    return content


def _runtime_package_files(
    package_root: Path,
    worker_entry: Path,
    *,
    include_test: bool = False,
) -> tuple[Path, ...]:
    """List path-independent runtime inputs for a packaged worker.

    Test-runner files are deliberately excluded because they have a separate
    fingerprint and can be reheadered without regenerating implementations.
    Every shipped file under ``dist`` participates. JavaScript packages may
    execute native addons, WASM payloads, or extensionless helpers in addition
    to source-shaped files. Test-runner files are excluded from the worker-only
    scope because they have a separate fingerprint.
    """

    try:
        physical_root = package_root.resolve(strict=True)
        physical_entry = worker_entry.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(f"Could not resolve @usejaunt/ts worker files: {exc}") from exc
    if physical_entry != physical_root and physical_root not in physical_entry.parents:
        raise TypeScriptWorkerError("@usejaunt/ts worker entry escapes its package")

    def snapshot() -> tuple[Path, ...]:
        dist = physical_root / "dist"
        if not dist.is_dir():
            raise TypeScriptWorkerError(
                f"Packaged @usejaunt/ts worker has no runtime directory: {dist}"
            )
        paths = {
            path
            for path in _runtime_package_identity_files(physical_root)
            if path.relative_to(physical_root).parts[:1] == ("dist",)
            and (include_test or path.relative_to(dist).parts[:1] != ("test",))
        }
        paths.add(physical_entry)
        if any(path != physical_root and physical_root not in path.parents for path in paths):
            raise TypeScriptWorkerError("@usejaunt/ts runtime file escapes its package")
        return tuple(sorted(paths, key=lambda path: path.relative_to(physical_root).as_posix()))

    before = snapshot()
    # Re-enumeration after the caller reads the files detects additions/removals.
    # The tuple is returned now and checked once more by worker_runtime_identity.
    return before


def worker_runtime_identity(
    installation: WorkerInstallation,
    *,
    include_test: bool = False,
) -> str:
    """Return a portable content identity for the exact worker runtime.

    A normal package and a source-tree override with the same packed runtime
    bytes receive the same identity. An arbitrary ``JAUNT_TS_WORKER`` override
    has no trusted version, so its executable bytes are the complete identity.
    """

    entry = installation.worker_entry
    compiler_identity = compiler_runtime_identity(installation)
    if not installation.package_managed:
        content = _stable_bytes(entry, label="TypeScript worker override")
        payload: object = {
            "format": "jaunt-ts-worker-runtime/3",
            "kind": "override",
            "scope": "full-package" if include_test else "worker",
            "entryDigest": hashlib.sha256(content).hexdigest(),
            "compilerRuntimeIdentity": compiler_identity,
        }
    else:
        package_root = installation.package_root.resolve()
        manifest_path = package_root / "package.json"
        manifest_bytes = _stable_bytes(manifest_path, label="@usejaunt/ts package.json")
        try:
            manifest = json.loads(manifest_bytes)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise TypeScriptWorkerError(
                f"Could not parse @usejaunt/ts package.json at {manifest_path}: {exc}"
            ) from exc
        if not isinstance(manifest, Mapping):
            raise TypeScriptWorkerError(
                f"Invalid @usejaunt/ts package.json: expected an object at {manifest_path}"
            )
        _validate_worker_package(manifest, manifest_path)
        paths = _runtime_package_files(package_root, entry, include_test=include_test)

        def file_digests(runtime_paths: tuple[Path, ...]) -> dict[str, str]:
            return {
                path.relative_to(package_root).as_posix(): hashlib.sha256(
                    _stable_bytes(path, label="@usejaunt/ts runtime file")
                ).hexdigest()
                for path in runtime_paths
            }

        files = file_digests(paths)
        after_paths = _runtime_package_files(package_root, entry, include_test=include_test)
        if (
            paths != after_paths
            or files != file_digests(after_paths)
            or manifest_bytes != _stable_bytes(manifest_path, label="@usejaunt/ts package.json")
        ):
            raise TypeScriptWorkerError(
                "@usejaunt/ts runtime tree changed while its freshness identity was read"
            )
        exports = manifest.get("exports")
        worker_export = exports.get("./worker") if isinstance(exports, Mapping) else None
        payload = {
            "format": "jaunt-ts-worker-runtime/3",
            "kind": "package",
            "scope": "full-package" if include_test else "worker",
            "name": "@usejaunt/ts",
            "version": str(manifest["version"]),
            "manifest": _ordered_json_identity(manifest),
            "manifestResolution": _runtime_manifest_identity(manifest),
            "workerExport": _export_target(worker_export),
            "entry": entry.resolve().relative_to(package_root).as_posix(),
            "files": files,
            "compilerRuntimeIdentity": compiler_identity,
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def toolchain_session_identity(
    installation: WorkerInstallation,
    *,
    include_test: bool,
) -> str:
    """Return an ephemeral content-and-filesystem token for one command.

    Unlike the portable persisted fingerprints, this command-local token binds
    the current package files to their filesystem identities. It therefore
    detects a clean/recreate ABA rebuild even when every replacement byte is
    identical.
    """

    lexical_root = Path(os.path.abspath(installation.package_root))
    package_root = lexical_root.resolve()

    def package_entry_metadata() -> tuple[object, ...]:
        if not installation.package_managed:
            return ()
        try:
            stat_result = lexical_root.lstat()
            target = os.readlink(lexical_root) if lexical_root.is_symlink() else ""
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not inspect @usejaunt/ts package entry at {lexical_root}: {exc}"
            ) from exc
        return (
            stat_result.st_dev,
            stat_result.st_ino,
            stat_result.st_mode,
            stat_result.st_size,
            stat_result.st_mtime_ns,
            stat_result.st_ctime_ns,
            target,
        )

    def paths() -> tuple[Path, ...]:
        if not installation.package_managed:
            return (installation.worker_entry.resolve(strict=True),)
        runtime = _runtime_package_files(
            package_root,
            installation.worker_entry,
            include_test=include_test,
        )
        return (package_root / "package.json", package_root / "dist", *runtime)

    def metadata(runtime_paths: tuple[Path, ...]) -> dict[str, tuple[int, ...]]:
        result: dict[str, tuple[int, ...]] = {}
        for path in runtime_paths:
            try:
                physical = path.resolve(strict=True)
                stat_result = physical.stat()
            except OSError as exc:
                raise TypeScriptWorkerError(
                    f"Could not inspect @usejaunt/ts command runtime at {path}: {exc}"
                ) from exc
            relative = (
                physical.relative_to(package_root).as_posix()
                if physical == package_root or package_root in physical.parents
                else "worker-override"
            )
            result[relative] = (
                stat_result.st_dev,
                stat_result.st_ino,
                stat_result.st_mode,
                stat_result.st_size,
                stat_result.st_mtime_ns,
                stat_result.st_ctime_ns,
            )
        return result

    before_paths = paths()
    before_package_entry = package_entry_metadata()
    before_metadata = metadata(before_paths)
    content_identity = worker_runtime_identity(installation, include_test=include_test)
    after_paths = paths()
    after_package_entry = package_entry_metadata()
    after_metadata = metadata(after_paths)
    if (
        before_paths != after_paths
        or before_package_entry != after_package_entry
        or before_metadata != after_metadata
    ):
        raise TypeScriptWorkerError(
            "@usejaunt/ts command runtime changed while its session identity was read"
        )
    encoded = json.dumps(
        {
            "format": "jaunt-ts-command-runtime/2",
            "contentIdentity": content_identity,
            "packageEntry": before_package_entry,
            "files": before_metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def worker_generation_fingerprint(base: str, worker_identity: str) -> str:
    """Bind the model/tool fingerprint to the worker that validates its output."""

    encoded = json.dumps(
        {
            "format": "jaunt-ts-generation-worker/1",
            "generationFingerprint": base or "unspecified",
            "workerRuntimeIdentity": worker_identity,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _contained(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve()
    root = root.resolve()
    if resolved != root and root not in resolved.parents:
        raise TypeScriptWorkerError(f"{label} escapes the project root: {resolved}")
    return resolved


def _lexically_contained(path: Path, root: Path, *, label: str) -> Path:
    """Confine a package-manager entry without rejecting its physical store.

    npm and pnpm expose dependencies through a workspace-local ``node_modules``
    path that may be a symlink into a content-addressed store outside the repo.
    Only tooling resolution uses this lexical boundary; application and artifact
    paths continue to use ``_contained`` and therefore follow symlinks.
    """

    absolute = Path(os.path.abspath(path))
    boundary = Path(os.path.abspath(root))
    if absolute != boundary and boundary not in absolute.parents:
        raise TypeScriptWorkerError(f"{label} escapes the project root: {absolute}")
    return absolute


def _search_node_modules(owner: Path, root: Path, relative: Path) -> Path | None:
    current = owner.resolve()
    root = root.resolve()
    while True:
        candidate = current / "node_modules" / relative
        if candidate.is_file():
            return candidate
        if current == root:
            return None
        if root not in current.parents:
            return None
        current = current.parent


def _read_package_json(path: Path, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise TypeScriptWorkerError(f"Missing {label}: {path}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TypeScriptWorkerError(f"Could not read {label} at {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise TypeScriptWorkerError(f"Invalid {label}: expected a JSON object at {path}")
    return value


def _export_target(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if not isinstance(value, Mapping):
        return None
    raw = {str(key): item for key, item in value.items()}
    for key in ("import", "default", "node"):
        target = raw.get(key)
        if isinstance(target, str):
            return target
    return None


def _validate_typescript_package(compiler: Path) -> None:
    package_path = compiler.parent.parent / "package.json"
    package = _read_package_json(package_path, label="TypeScript package.json")
    if package.get("name") != "typescript":
        raise TypeScriptWorkerError(
            f"Resolved TypeScript compiler is not owned by the 'typescript' package: {package_path}"
        )
    version = package.get("version")
    if not isinstance(version, str):
        raise TypeScriptWorkerError(f"TypeScript package has no string version: {package_path}")
    try:
        major, minor = (int(part) for part in version.split(".", 2)[:2])
    except (TypeError, ValueError) as exc:
        raise TypeScriptWorkerError(
            f"TypeScript package has an invalid version {version!r}: {package_path}"
        ) from exc
    if major >= 7 or major < 5 or (major == 5 and minor < 8):
        raise TypeScriptWorkerError(f"TypeScript {version} is outside the supported >=5.8 <7 range")


def _validate_worker_package(package: Mapping[str, Any], package_path: Path) -> None:
    if package.get("name") != "@usejaunt/ts":
        raise TypeScriptWorkerError(
            f"Resolved worker is not the @usejaunt/ts package: {package_path}"
        )
    version = package.get("version")
    if not isinstance(version, str) or not version.strip():
        raise TypeScriptWorkerError(f"@usejaunt/ts package has no string version: {package_path}")


def _override_package_root(worker_entry: Path) -> tuple[Path, bool]:
    """Find the package root for an explicit worker entry when one is available."""

    current = worker_entry.parent
    while True:
        manifest = current / "package.json"
        try:
            package = json.loads(manifest.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError):
            package = None
        if isinstance(package, Mapping) and package.get("name") == "@usejaunt/ts":
            _validate_worker_package(package, manifest)
            return current, True
        if current.parent == current:
            return worker_entry.parent, False
        current = current.parent


def resolve_worker_installation(
    root: Path,
    target: TypeScriptTargetConfig,
    *,
    environ: Mapping[str, str] | None = None,
) -> WorkerInstallation:
    """Resolve Node, worker, and compiler from the configured ``tool_owner``.

    The owning package must directly declare both tooling dependencies. Physical
    packages may be hoisted to an ancestor inside the Jaunt root.
    """

    env = os.environ if environ is None else environ
    root = root.resolve()
    configured_owner = Path(target.tool_owner)
    owner = configured_owner if configured_owner.is_absolute() else root / configured_owner
    owner = _contained(owner, root, label="target.ts.tool_owner")
    if not owner.is_dir():
        raise TypeScriptWorkerError(f"target.ts.tool_owner is not a directory: {owner}")

    owner_package = _read_package_json(owner / "package.json", label="tool-owner package.json")
    dev_dependencies = owner_package.get("devDependencies", {})
    declared = (
        {str(name) for name in dev_dependencies} if isinstance(dev_dependencies, Mapping) else set()
    )
    missing = [name for name in ("@usejaunt/ts", "typescript") if name not in declared]
    if missing:
        raise TypeScriptWorkerError(
            f"{owner / 'package.json'} must directly declare devDependencies: " + ", ".join(missing)
        )

    node = shutil.which("node", path=env.get("PATH", ""))
    if node is None:
        raise TypeScriptWorkerError(
            "Node.js is required for the TypeScript target but was not found"
        )

    compiler = _search_node_modules(owner, root, Path("typescript/lib/typescript.js"))
    if compiler is None:
        raise TypeScriptWorkerError(
            f"Could not resolve project-local TypeScript from {owner}; install dependencies first"
        )
    compiler = _lexically_contained(compiler, root, label="TypeScript compiler")
    _validate_typescript_package(compiler)

    override = env.get("JAUNT_TS_WORKER", "").strip()
    if override:
        worker_entry = Path(override).expanduser().resolve()
        if not worker_entry.is_file():
            raise TypeScriptWorkerError(f"JAUNT_TS_WORKER does not name a file: {worker_entry}")
        package_root, package_managed = _override_package_root(worker_entry)
    else:
        package_json = _search_node_modules(owner, root, Path("@usejaunt/ts/package.json"))
        if package_json is None:
            raise TypeScriptWorkerError(
                f"Could not resolve project-local @usejaunt/ts from {owner}; "
                "install dependencies first"
            )
        package_json = _lexically_contained(package_json, root, label="@usejaunt/ts package")
        package_root = package_json.parent
        package = _read_package_json(package_json, label="@usejaunt/ts package.json")
        _validate_worker_package(package, package_json)
        exports = package.get("exports")
        worker_export = exports.get("./worker") if isinstance(exports, Mapping) else None
        worker_target = _export_target(worker_export)
        if worker_target is None:
            raise TypeScriptWorkerError(
                f"Installed @usejaunt/ts at {package_root} does not export './worker'"
            )
        worker_entry = package_root / worker_target
        physical_root = package_root.resolve()
        physical_entry = worker_entry.resolve()
        if physical_root != physical_entry and physical_root not in physical_entry.parents:
            raise TypeScriptWorkerError("@usejaunt/ts worker export escapes its package")
        if not worker_entry.is_file():
            raise TypeScriptWorkerError(f"@usejaunt/ts worker entry does not exist: {worker_entry}")
        package_managed = True

    return WorkerInstallation(
        node=node,
        worker_entry=worker_entry,
        compiler_module_path=compiler,
        package_root=package_root,
        tool_owner=owner,
        package_managed=package_managed,
    )


def worker_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a minimal environment with Node injection variables removed."""

    source = os.environ if environ is None else environ
    result = {key: value for key, value in source.items() if key in _ENV_ALLOWLIST}
    result["JAUNT_TS_PROTOCOL"] = PROTOCOL_VERSION
    result["JAUNT_TS_PHASE_TELEMETRY"] = "1"
    return result


class WorkerClient:
    """Concurrent request/response client for one analyzer subprocess."""

    def __init__(
        self,
        *,
        root: Path,
        installation: WorkerInstallation,
        request_timeout: float = _DEFAULT_TIMEOUT,
        startup_timeout: float = _DEFAULT_STARTUP_TIMEOUT,
        max_message_bytes: int = _DEFAULT_MAX_MESSAGE_BYTES,
        stderr_limit: int = _DEFAULT_STDERR_BYTES,
        environ: Mapping[str, str] | None = None,
        heap_mb: int | None = None,
    ) -> None:
        self.root = root.resolve()
        self.installation = installation
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.max_message_bytes = max_message_bytes
        self.stderr_limit = stderr_limit
        self._environment = worker_environment(environ)
        self.heap_mb = heap_mb
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._pending: dict[str, asyncio.Future[ProtocolResponse]] = {}
        self._notifications: set[str] = set()
        self._write_lock = asyncio.Lock()
        self._restart_lock = asyncio.Lock()
        self._request_number = 0
        self._process_generation = 0
        self._initialize_params: InitializeParams | None = None
        self._worker_runtime_identity: str | None = None
        self._compiler_runtime_session_identity: str | None = None
        self._full_runtime_session_identity: str | None = None
        self._package_runtime_session_identities: dict[str, tuple[Path, str | None, str]] = {}
        self._package_resolution_pins: dict[str, _PackageResolutionPin] = {}
        self._absent_package_resolution_pins: dict[str, _AbsentPackageResolutionPin] = {}
        self._runtime_identity_sealed = False
        self._stderr = bytearray()
        self._closed = False

    @property
    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def __aenter__(self) -> WorkerClient:
        self.reset_full_runtime_identity()
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        try:
            if exc_type is None and not self._runtime_identity_sealed:
                self.verify_runtime_identity()
        finally:
            await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        if self._closed:
            raise TypeScriptWorkerError("TypeScript worker client is closed")
        self.verify_runtime_identity()
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        node_args = [f"--max-old-space-size={self.heap_mb}"] if self.heap_mb is not None else []
        self._process = await asyncio.create_subprocess_exec(
            self.installation.node,
            *node_args,
            str(self.installation.worker_entry),
            cwd=str(self.root),
            env=self._environment,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=self.max_message_bytes + 1,
            **kwargs,
        )
        self._process_generation += 1
        self._reader_task = asyncio.create_task(self._read_responses())
        self._stderr_task = asyncio.create_task(self._read_stderr())

    async def initialize(self, params: InitializeParams) -> InitializeResult:
        await self.start()
        worker_identity = self.verify_runtime_identity()
        params = replace(
            params,
            generation_fingerprint=worker_generation_fingerprint(
                params.generation_fingerprint,
                worker_identity,
            ),
        )
        result = await self.request(
            "initialize",
            params.to_wire(),
            timeout=self.startup_timeout,
        )
        initialized = InitializeResult.from_wire(result)
        if initialized.protocol != PROTOCOL_VERSION:
            await self._terminate()
            raise WorkerProtocolError(
                f"TypeScript worker protocol mismatch: expected {PROTOCOL_VERSION}, "
                f"got {initialized.protocol}"
            )
        try:
            validate_worker_capabilities(initialized)
        except WorkerProtocolError:
            await self._terminate()
            raise
        self._initialize_params = params
        return initialized

    def verify_runtime_identity(self) -> str:
        """Pin one immutable worker runtime to the lifetime of this client."""

        try:
            current = worker_runtime_identity(self.installation)
        except TypeScriptWorkerError as exc:
            if self._worker_runtime_identity is None:
                raise
            raise WorkerToolchainChangedError(
                "The project-local @usejaunt/ts runtime became unreadable while "
                "the analyzer session was active. Rerun after the toolchain is stable; "
                "Jaunt will not report this session as successful."
            ) from exc
        if self._worker_runtime_identity is None:
            self._worker_runtime_identity = current
        elif current != self._worker_runtime_identity:
            raise WorkerToolchainChangedError(
                "The project-local @usejaunt/ts or TypeScript runtime changed while the analyzer "
                "session was active. Rerun after the toolchain is stable; Jaunt will "
                "not report this session as successful."
            )
        try:
            compiler_current = compiler_session_identity(self.installation)
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                "The project-local TypeScript compiler became unreadable while the analyzer "
                "session was active. Rerun after the toolchain is stable; Jaunt will not "
                "report this session as successful."
            ) from exc
        if self._compiler_runtime_session_identity is None:
            self._compiler_runtime_session_identity = compiler_current
        elif compiler_current != self._compiler_runtime_session_identity:
            raise WorkerToolchainChangedError(
                "The project-local TypeScript compiler filesystem epoch changed while the "
                "analyzer session was active. Rerun after the toolchain is stable; Jaunt will "
                "not report this session as successful."
            )
        if self._full_runtime_session_identity is not None:
            try:
                full_current = toolchain_session_identity(
                    self.installation,
                    include_test=True,
                )
            except TypeScriptWorkerError as exc:
                raise WorkerToolchainChangedError(
                    "The project-local @usejaunt/ts full command runtime became unreadable "
                    "after protected test validation. Rerun after the toolchain is stable; "
                    "Jaunt will not report this session as successful."
                ) from exc
            if full_current != self._full_runtime_session_identity:
                raise WorkerToolchainChangedError(
                    "The project-local @usejaunt/ts full command runtime changed after "
                    "protected test validation. Rerun after the toolchain is stable; Jaunt "
                    "will not report this session as successful."
                )
        for label, (
            package_root,
            expected_name,
            expected,
        ) in self._package_runtime_session_identities.items():
            try:
                package_current = runtime_package_session_identity(
                    package_root,
                    expected_name=expected_name,
                )
            except TypeScriptWorkerError as exc:
                raise WorkerToolchainChangedError(
                    f"The pinned {label} runtime became unreadable during this command. "
                    "Rerun after the toolchain is stable; Jaunt will not report this session "
                    "as successful."
                ) from exc
            if package_current != expected:
                raise WorkerToolchainChangedError(
                    f"The pinned {label} runtime changed during this command. Rerun after the "
                    "toolchain is stable; Jaunt will not report this session as successful."
                )
        for label, pin in self._package_resolution_pins.items():
            try:
                before = resolve_node_package(
                    pin.start,
                    pin.package,
                    boundary=pin.boundary,
                    module_path=pin.module_path,
                )
                if before is None:
                    raise TypeScriptWorkerError(
                        f"The pinned package {pin.package!r} is no longer resolvable"
                    )
                resolved_root = Path(os.path.abspath(before))
                package_current = runtime_package_session_identity(
                    resolved_root,
                    expected_name=pin.expected_name,
                )
                after = resolve_node_package(
                    pin.start,
                    pin.package,
                    boundary=pin.boundary,
                    module_path=pin.module_path,
                )
            except TypeScriptWorkerError as exc:
                raise WorkerToolchainChangedError(
                    f"The pinned {label} resolution became unreadable during this command. "
                    "Rerun after the toolchain is stable; Jaunt will not report this session "
                    "as successful."
                ) from exc
            if (
                after is None
                or resolved_root != pin.resolved_root
                or Path(os.path.abspath(after)) != pin.resolved_root
                or package_current != pin.session_identity
            ):
                raise WorkerToolchainChangedError(
                    f"The pinned {label} resolution topology or runtime changed during this "
                    "command. Rerun after the toolchain is stable; Jaunt will not report this "
                    "session as successful."
                )
        for label, pin in self._absent_package_resolution_pins.items():
            try:
                resolved = resolve_node_package(
                    pin.start,
                    pin.package,
                    boundary=pin.boundary,
                    module_path=pin.module_path,
                )
            except TypeScriptWorkerError as exc:
                raise WorkerToolchainChangedError(
                    f"The pinned {label} resolution became unreadable during this command. "
                    "Rerun after the toolchain is stable; Jaunt will not report this session "
                    "as successful."
                ) from exc
            if resolved is not None:
                raise WorkerToolchainChangedError(
                    f"The pinned {label} resolution topology changed during this command. "
                    "Rerun after the toolchain is stable; Jaunt will not report this session "
                    "as successful."
                )
        return current

    def pin_package_resolution_identity(
        self,
        label: str,
        start: Path,
        package: str,
        *,
        boundary: Path | None = None,
        module_path: bool = False,
        expected_name: str | None = None,
    ) -> str:
        """Pin a package's selected Node search result and runtime epoch."""

        lexical_start = Path(os.path.abspath(start))
        lexical_boundary = Path(os.path.abspath(boundary)) if boundary is not None else None
        try:
            before = resolve_node_package(
                lexical_start,
                package,
                boundary=lexical_boundary,
                module_path=module_path,
            )
            if before is None:
                raise TypeScriptWorkerError(f"Package {package!r} is not resolvable from {start}")
            resolved_root = Path(os.path.abspath(before))
            current = runtime_package_session_identity(
                resolved_root,
                expected_name=expected_name,
            )
            after = resolve_node_package(
                lexical_start,
                package,
                boundary=lexical_boundary,
                module_path=module_path,
            )
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                f"The {label} resolution could not be pinned for this command. Rerun after "
                "the toolchain is stable."
            ) from exc
        if after is None or Path(os.path.abspath(after)) != resolved_root:
            raise WorkerToolchainChangedError(
                f"The {label} resolution topology changed while it was pinned. Rerun after "
                "the toolchain is stable."
            )
        pin = _PackageResolutionPin(
            start=lexical_start,
            boundary=lexical_boundary,
            package=package,
            module_path=module_path,
            expected_name=expected_name,
            resolved_root=resolved_root,
            session_identity=current,
        )
        previous = self._package_resolution_pins.get(label)
        if previous is None:
            self._package_resolution_pins[label] = pin
        elif previous != pin:
            raise WorkerToolchainChangedError(
                f"The {label} resolution topology or runtime changed during this command. "
                "Rerun after the toolchain is stable."
            )
        return current

    def pin_package_runtime_identity(
        self,
        label: str,
        package_root: Path,
        *,
        expected_name: str | None = None,
    ) -> str:
        """Pin a separately resolved runner package to this command's epoch."""

        lexical_root = Path(os.path.abspath(package_root))
        try:
            current = runtime_package_session_identity(
                lexical_root,
                expected_name=expected_name,
            )
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                f"The {label} runtime could not be pinned for this command. Rerun after the "
                "toolchain is stable."
            ) from exc
        previous = self._package_runtime_session_identities.get(label)
        if previous is None:
            self._package_runtime_session_identities[label] = (
                lexical_root,
                expected_name,
                current,
            )
        elif previous != (lexical_root, expected_name, current):
            raise WorkerToolchainChangedError(
                f"The {label} runtime or its resolved owner changed during this command. "
                "Rerun after the toolchain is stable."
            )
        return current

    def _pin_absent_package_resolution(
        self,
        label: str,
        start: Path,
        package: str,
        *,
        boundary: Path | None,
        module_path: bool,
    ) -> None:
        """Pin an unresolved optional dependency so a late install is detected."""

        lexical_start = Path(os.path.abspath(start))
        lexical_boundary = Path(os.path.abspath(boundary)) if boundary is not None else None
        pin = _AbsentPackageResolutionPin(
            start=lexical_start,
            boundary=lexical_boundary,
            package=package,
            module_path=module_path,
        )
        try:
            current = resolve_node_package(
                lexical_start,
                package,
                boundary=lexical_boundary,
                module_path=module_path,
            )
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                f"The {label} resolution could not be pinned for this command. Rerun after "
                "the toolchain is stable."
            ) from exc
        if current is not None:
            raise WorkerToolchainChangedError(
                f"The {label} resolution topology changed while it was pinned. Rerun after "
                "the toolchain is stable."
            )
        previous = self._absent_package_resolution_pins.get(label)
        if previous is None:
            self._absent_package_resolution_pins[label] = pin
        elif previous != pin:
            raise WorkerToolchainChangedError(
                f"The {label} resolution topology changed during this command. Rerun after "
                "the toolchain is stable."
            )

    def pin_package_resolution_closure(
        self,
        label: str,
        start: Path,
        package: str,
        *,
        boundary: Path | None = None,
        module_path: bool = False,
        expected_name: str | None = None,
    ) -> str:
        """Pin one resolved package and its transitive runtime dependency graph."""

        root_identity = self.pin_package_resolution_identity(
            label,
            start,
            package,
            boundary=boundary,
            module_path=module_path,
            expected_name=expected_name,
        )
        root_pin = self._package_resolution_pins[label]
        try:
            closure = _runtime_package_resolution_closure(
                root_pin.resolved_root,
                root_label=package,
            )
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                f"The {label} dependency closure could not be pinned for this command. "
                "Rerun after the toolchain is stable."
            ) from exc
        for edge_number, edge in enumerate(closure, start=1):
            edge_label = (
                f"{label} runtime dependency {edge_number} ({edge.package} from {edge.importer})"
            )
            if edge.resolved_root is None:
                self._pin_absent_package_resolution(
                    edge_label,
                    edge.importer,
                    edge.package,
                    boundary=None,
                    module_path=True,
                )
                continue
            self.pin_package_resolution_identity(
                edge_label,
                edge.importer,
                edge.package,
                module_path=True,
            )
            pinned_root = self._package_resolution_pins[edge_label].resolved_root
            if pinned_root != Path(os.path.abspath(edge.resolved_root)):
                raise WorkerToolchainChangedError(
                    f"The {edge_label} resolution topology changed while the dependency "
                    "closure was pinned. Rerun after the toolchain is stable."
                )
        return root_identity

    def pin_full_runtime_identity(self) -> str:
        """Pin test runner, declarations, and worker files for this command."""

        try:
            current = toolchain_session_identity(self.installation, include_test=True)
        except TypeScriptWorkerError as exc:
            raise WorkerToolchainChangedError(
                "The project-local @usejaunt/ts full command runtime could not be pinned "
                "for protected test validation. Rerun after the toolchain is stable."
            ) from exc
        if self._full_runtime_session_identity is None:
            self._full_runtime_session_identity = current
        elif current != self._full_runtime_session_identity:
            raise WorkerToolchainChangedError(
                "The project-local @usejaunt/ts full command runtime changed during "
                "protected test validation. Rerun after the toolchain is stable."
            )
        return current

    def reset_full_runtime_identity(self) -> None:
        """Begin a new high-level command with no protected-test runtime pin."""

        self._compiler_runtime_session_identity = None
        self._full_runtime_session_identity = None
        self._package_runtime_session_identities.clear()
        self._package_resolution_pins.clear()
        self._absent_package_resolution_pins.clear()

    def seal_runtime_identity(self) -> str:
        """Verify the pin at a rollback boundary and seal this request sequence."""

        current = self.verify_runtime_identity()
        # The first pass reads several independent package pins in sequence. A
        # second complete pass catches a package replaced after its earlier
        # slot was checked, while rollback bytes remain available.
        current = self.verify_runtime_identity()
        self._runtime_identity_sealed = True
        return current

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
        deadline_ms: int | None = None,
    ) -> Mapping[str, Any]:
        self._runtime_identity_sealed = False
        await self.start()
        failed_generation = self._process_generation
        try:
            return await self._request_once(
                method,
                params,
                timeout=timeout,
                deadline_ms=deadline_ms,
            )
        except WorkerCrashedError as error:
            if self._is_out_of_memory(error):
                raise WorkerOutOfMemoryError(self._oom_message(method)) from error
            if method not in _CRASH_REPLAY_METHODS or self._initialize_params is None:
                raise
            await self._restart_and_initialize(failed_generation)
            return await self._request_once(
                method,
                params,
                timeout=timeout,
                deadline_ms=deadline_ms,
            )

    async def _request_once(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
        deadline_ms: int | None = None,
    ) -> Mapping[str, Any]:
        await self.start()
        process = self._process
        if process is None or process.stdin is None:
            raise WorkerCrashedError("TypeScript worker did not start")
        stdin = process.stdin
        if process.returncode is not None:
            raise WorkerCrashedError(self._crash_message(process.returncode))

        effective_timeout = self.request_timeout if timeout is None else timeout
        if effective_timeout <= 0:
            raise ValueError("TypeScript worker request timeout must be positive")
        wire_deadline_ms = deadline_ms
        if wire_deadline_ms is None:
            wire_deadline_ms = max(1, min(3_600_000, int(effective_timeout * 1000)))

        self._request_number += 1
        request_id = str(self._request_number)
        request = ProtocolRequest(
            id=request_id,
            method=method,
            params=params,
            deadline_ms=wire_deadline_ms,
        )
        wire = json.dumps(request.to_wire(), sort_keys=True, separators=(",", ":")).encode()
        if len(wire) > self.max_message_bytes:
            raise WorkerProtocolError(
                f"TypeScript worker request exceeds {self.max_message_bytes} bytes"
            )
        loop = asyncio.get_running_loop()
        future: asyncio.Future[ProtocolResponse] = loop.create_future()
        self._pending[request_id] = future

        async def exchange() -> ProtocolResponse:
            try:
                async with self._write_lock:
                    stdin.write(wire + b"\n")
                    await stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise WorkerCrashedError(self._crash_message(process.returncode)) from exc
            return await future

        try:
            try:
                response = await asyncio.wait_for(exchange(), timeout=effective_timeout)
            except TimeoutError as exc:
                await self._terminate()
                timeout_setting = (
                    "worker_startup_timeout_seconds"
                    if method == "initialize"
                    else "worker_timeout_seconds"
                )
                raise WorkerTimeoutError(
                    f"TypeScript worker request {method!r} timed out after "
                    f"{effective_timeout:.3g}s. Increase "
                    f"[target.ts].{timeout_setting} for a larger project."
                    + (f"\nstderr:\n{self.stderr}" if self.stderr else "")
                ) from exc
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await self._write_notification("cancel", {"requestId": request_id})
                await self._terminate()
                raise
        finally:
            self._pending.pop(request_id, None)

        if not response.ok:
            assert response.error is not None
            raise WorkerRemoteError(
                code=response.error.code,
                message=response.error.message,
                retryable=response.error.retryable,
                diagnostics=response.error.diagnostics,
            )
        return response.result or {}

    async def _restart_and_initialize(self, failed_generation: int) -> None:
        async with self._restart_lock:
            if self._process_generation != failed_generation:
                return
            params = self._initialize_params
            if params is None:
                raise WorkerCrashedError("TypeScript worker crashed before initialization")
            await self._terminate()
            await self.start()
            result = await self._request_once(
                "initialize",
                params.to_wire(),
                timeout=self.startup_timeout,
            )
            initialized = InitializeResult.from_wire(result)
            if initialized.protocol != PROTOCOL_VERSION:
                await self._terminate()
                raise WorkerProtocolError(
                    f"TypeScript worker protocol mismatch after restart: expected "
                    f"{PROTOCOL_VERSION}, got {initialized.protocol}"
                )
            try:
                validate_worker_capabilities(initialized)
            except WorkerProtocolError:
                await self._terminate()
                raise

    async def cancel(self, request_id: str) -> Mapping[str, Any]:
        return await self.request("cancel", {"requestId": request_id})

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        process = self._process
        if process is not None and process.returncode is None:
            with contextlib.suppress(Exception):
                await self.request("shutdown", {}, timeout=min(2.0, self.request_timeout))
        await self._terminate()

    async def _write_notification(self, method: str, params: Mapping[str, Any]) -> None:
        process = self._process
        if process is None or process.stdin is None or process.returncode is not None:
            return
        self._request_number += 1
        request_id = str(self._request_number)
        self._notifications.add(request_id)
        wire = json.dumps(
            ProtocolRequest(id=request_id, method=method, params=params).to_wire(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        async with self._write_lock:
            process.stdin.write(wire + b"\n")
            await process.stdin.drain()

    async def _read_responses(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except ValueError as exc:
                    raise WorkerProtocolError(
                        f"TypeScript worker response exceeds {self.max_message_bytes} bytes"
                    ) from exc
                if not line:
                    break
                if len(line) > self.max_message_bytes:
                    raise WorkerProtocolError(
                        f"TypeScript worker response exceeds {self.max_message_bytes} bytes"
                    )
                try:
                    raw = json.loads(line)
                    response = ProtocolResponse.from_wire(raw)
                except (json.JSONDecodeError, UnicodeError, ProtocolValidationError) as exc:
                    raise WorkerProtocolError(
                        f"Malformed TypeScript worker response: {exc}"
                    ) from exc
                if response.protocol != PROTOCOL_VERSION:
                    raise WorkerProtocolError(
                        f"TypeScript worker protocol mismatch: expected {PROTOCOL_VERSION}, "
                        f"got {response.protocol}"
                    )
                future = self._pending.get(response.id)
                if future is None:
                    if response.id in self._notifications:
                        self._notifications.discard(response.id)
                        continue
                    raise WorkerProtocolError(
                        f"TypeScript worker returned unknown response id {response.id!r}"
                    )
                if future.done():
                    raise WorkerProtocolError(
                        f"TypeScript worker returned duplicate response id {response.id!r}"
                    )
                future.set_result(response)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._fail_pending(exc)
            await self._kill_process()
            return

        returncode = await process.wait()
        stderr_task = self._stderr_task
        if stderr_task is not None and stderr_task is not asyncio.current_task():
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await stderr_task
        if self._pending:
            self._fail_pending(WorkerCrashedError(self._crash_message(returncode)))

    async def _read_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            chunk = await process.stderr.read(4096)
            if not chunk:
                return
            self._stderr.extend(chunk)
            if len(self._stderr) > self.stderr_limit:
                del self._stderr[: len(self._stderr) - self.stderr_limit]

    def _fail_pending(self, exc: BaseException) -> None:
        for future in tuple(self._pending.values()):
            if not future.done():
                future.set_exception(exc)

    def _crash_message(self, returncode: int | None) -> str:
        message = f"TypeScript worker exited unexpectedly (exit code {returncode})"
        if self.stderr:
            message += f"\nstderr:\n{self.stderr}"
        return message

    @staticmethod
    def _is_out_of_memory(error: BaseException) -> bool:
        message = str(error).lower()
        return "fatal error" in message and any(
            marker in message
            for marker in (
                "heap out of memory",
                "reached heap limit",
                "allocation failed - javascript heap",
            )
        )

    def _oom_message(self, method: str) -> str:
        configured = f"{self.heap_mb} MiB" if self.heap_mb is not None else "Node's default"
        return (
            f"TypeScript worker exhausted {configured} heap during {method!r}; "
            "the deterministic request was not replayed. Jaunt batches scoped overlay "
            "validation; if this project's dependency closure still exceeds the default, "
            "set [target.ts].worker_heap_mb to a larger MiB value."
            + (f"\nstderr:\n{self.stderr}" if self.stderr else "")
        )

    async def _terminate(self) -> None:
        await self._kill_process()
        current = asyncio.current_task()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is not None and task is not current:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        self._reader_task = None
        self._stderr_task = None
        self._process = None
        self._notifications.clear()

    async def _kill_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            if os.name == "posix":
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - exercised in platform CI
                process.terminate()
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except (ProcessLookupError, TimeoutError):
            if process.returncode is None:
                if os.name == "posix":
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                else:  # pragma: no cover - exercised in platform CI
                    process.kill()
                with contextlib.suppress(Exception):
                    await process.wait()
