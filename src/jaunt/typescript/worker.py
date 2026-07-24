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
    {"class", "enum", "function", "interface", "module", "namespace", "type"}
)
_DECLARATION_PREFIXES = frozenset({"abstract", "async", "const", "declare", "default", "export"})


def _opens_control_flow_parenthesis(tokens: Sequence[tuple[str, str]]) -> bool:
    """Return whether the next ``(`` opens a statement-leading control head."""

    if not tokens:
        return False
    head = len(tokens) - 1
    if tokens[head] == ("identifier", "await") and head > 0:
        head -= 1
    if tokens[head][0] != "identifier" or tokens[head][1] not in _CONTROL_FLOW_PAREN_HEADS:
        return False
    return head == 0 or tokens[head - 1][1] not in {".", "?."}


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
                return _opens_control_flow_parenthesis(tokens[:index])
    return False


def _colon_opens_statement_block(
    tokens: Sequence[tuple[str, str]], *, enclosing_statement_brace: bool | None
) -> bool:
    """Recognize a label or completed switch clause before a block."""

    if not tokens or tokens[-1][1] != ":":
        return False
    if len(tokens) >= 2 and tokens[-2][0] == "identifier":
        prefix = len(tokens) - 3
        if (
            prefix < 0
            or tokens[prefix][1] in {";", "}"}
            or (tokens[prefix][1] == "{" and enclosing_statement_brace is True)
        ):
            return True
    expected_openings: list[str] = []
    opening_for = {")": "(", "]": "[", "}": "{"}
    clause_index: int | None = None
    for index in range(len(tokens) - 2, -1, -1):
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
    for _kind, value in tokens[clause_index + 1 : -1]:
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

    if kind in {"number", "regex", "string", "template"}:
        return True
    if value in {")", "]", "}", "++", "--"}:
        return True
    if kind != "identifier":
        return False
    return value not in {
        "abstract",
        "async",
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


def _opens_statement_brace(
    tokens: Sequence[tuple[str, str]],
    *,
    enclosing_statement_brace: bool | None,
    previous_closed_control_head: bool,
    previous_closed_statement_brace: bool,
    line_break_before: bool = False,
) -> bool:
    """Return whether the next brace begins a statement/declaration body."""

    if not tokens:
        return True
    kind, value = tokens[-1]
    if value == ")" and previous_closed_control_head:
        return True
    if kind == "identifier" and value in {"catch", "do", "else", "finally", "try"}:
        return True
    if value == ";" or (value == "}" and previous_closed_statement_brace):
        return True
    # Two adjacent opening braces cannot form an object/property expression;
    # the inner brace is a nested statement block (including inside an arrow
    # or function expression body, whose outer close remains expression-like).
    if value == "{":
        return True
    if value == ":" and _colon_opens_statement_block(
        tokens, enclosing_statement_brace=enclosing_statement_brace
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
        prefix = index - 1
        while (
            prefix >= 0
            and tokens[prefix][0] == "identifier"
            and tokens[prefix][1] in _DECLARATION_PREFIXES
        ):
            prefix -= 1
        while prefix >= 1 and tokens[prefix - 1][1] == "@":
            prefix -= 2
        if (
            prefix >= 0
            and tokens[prefix][1] == ")"
            and _closes_control_flow_parenthesis(tokens, prefix)
        ):
            return True
        if (
            prefix >= 0
            and tokens[prefix][1] == ":"
            and _colon_opens_statement_block(
                tokens[: prefix + 1], enclosing_statement_brace=enclosing_statement_brace
            )
        ):
            return True
        return (
            prefix < 0
            or tokens[prefix][1] in {";", "}"}
            or (tokens[prefix][1] == "{" and enclosing_statement_brace is True)
            or (tokens[prefix][0] == "identifier" and tokens[prefix][1] in {"do", "else"})
        )
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
) -> tuple[tuple[str, str], ...]:
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
    deferred_template_expression_tokens: list[tuple[str, str]] = []
    cursor = 0
    previous: tuple[str, str] | None = None
    control_parentheses: list[bool] = []
    statement_braces: list[bool] = []
    previous_closed_control_head = False
    previous_closed_statement_brace = False
    pending_line_break = False

    def append(kind: str, value: str) -> None:
        nonlocal pending_line_break, previous
        nonlocal previous_closed_control_head, previous_closed_statement_brace
        closes_control_head = False
        closes_statement_brace = False
        if kind == "punctuation" and value == "(":
            control_parentheses.append(_opens_control_flow_parenthesis(tokens))
        elif kind == "punctuation" and value == ")":
            closes_control_head = control_parentheses.pop() if control_parentheses else False
        elif kind == "punctuation" and value == "{":
            statement_braces.append(
                _opens_statement_brace(
                    tokens,
                    enclosing_statement_brace=(statement_braces[-1] if statement_braces else None),
                    previous_closed_control_head=previous_closed_control_head,
                    previous_closed_statement_brace=previous_closed_statement_brace,
                    line_break_before=pending_line_break,
                )
            )
        elif kind == "punctuation" and value == "}":
            closes_statement_brace = statement_braces.pop() if statement_braces else False
        previous_closed_control_head = closes_control_head
        previous_closed_statement_brace = closes_statement_brace
        token = (kind, value)
        tokens.append(token)
        previous = token
        pending_line_break = False

    def slash_starts_regex() -> bool:
        if previous is None:
            return True
        kind, value = previous
        if kind in {"number", "regex", "string", "template"}:
            return False
        if kind == "identifier":
            return _identifier_precedes_regex(tokens)
        if value == ")" and previous_closed_control_head:
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
            if character in "\r\n":
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

    def template_expression_end(start: int) -> tuple[int, tuple[tuple[str, str], ...]]:
        index = start
        depth = 0
        expression_previous: tuple[str, str] | None = None
        expression_tokens: list[tuple[str, str]] = []
        expression_control_parentheses: list[bool] = []
        expression_statement_braces: list[bool] = []
        expression_previous_closed_control_head = False
        expression_previous_closed_statement_brace = False
        expression_pending_line_break = False

        def record_expression(kind: str, value: str) -> None:
            nonlocal expression_pending_line_break
            nonlocal expression_previous
            nonlocal expression_previous_closed_control_head
            nonlocal expression_previous_closed_statement_brace
            closes_control_head = False
            closes_statement_brace = False
            if kind == "punctuation" and value == "(":
                expression_control_parentheses.append(
                    _opens_control_flow_parenthesis(expression_tokens)
                )
            elif kind == "punctuation" and value == ")":
                closes_control_head = (
                    expression_control_parentheses.pop()
                    if expression_control_parentheses
                    else False
                )
            elif kind == "punctuation" and value == "{":
                expression_statement_braces.append(
                    _opens_statement_brace(
                        expression_tokens,
                        enclosing_statement_brace=(
                            expression_statement_braces[-1] if expression_statement_braces else None
                        ),
                        previous_closed_control_head=expression_previous_closed_control_head,
                        previous_closed_statement_brace=(
                            expression_previous_closed_statement_brace
                        ),
                        line_break_before=expression_pending_line_break,
                    )
                )
            elif kind == "punctuation" and value == "}":
                closes_statement_brace = (
                    expression_statement_braces.pop() if expression_statement_braces else False
                )
            expression_previous_closed_control_head = closes_control_head
            expression_previous_closed_statement_brace = closes_statement_brace
            expression_previous = (kind, value)
            expression_tokens.append(expression_previous)
            expression_pending_line_break = False

        def expression_slash_starts_regex() -> bool:
            if expression_previous is None:
                return True
            kind, value = expression_previous
            if kind in {"number", "regex", "string", "template"}:
                return False
            if kind == "identifier":
                return _identifier_precedes_regex(expression_tokens)
            if value == ")" and expression_previous_closed_control_head:
                return True
            if value == "}" and expression_previous_closed_statement_brace:
                return True
            return value not in {")", "]", "}", "++", "--"}

        while index < len(source):
            character = source[index]
            if character.isspace():
                expression_pending_line_break = expression_pending_line_break or character in "\r\n"
                index += 1
                continue
            if source.startswith("//", index):
                newline = source.find("\n", index + 2)
                expression_pending_line_break = expression_pending_line_break or newline >= 0
                index = len(source) if newline < 0 else newline + 1
                continue
            if source.startswith("/*", index):
                end = source.find("*/", index + 2)
                if end < 0:
                    raise TypeScriptWorkerError(
                        f"Runtime package source has an unterminated comment: {source_path}"
                    )
                expression_pending_line_break = expression_pending_line_break or any(
                    character in "\r\n" for character in source[index : end + 2]
                )
                index = end + 2
                continue
            if character in {"'", '"'}:
                index, _value = skip_quoted(index, character)
                record_expression("string", "literal")
                continue
            if character == "`":
                index = skip_template(index)
                record_expression("template", "literal")
                continue
            if character == "/" and expression_slash_starts_regex():
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
                    expression = source[start:index]
                    return index + 1, _runtime_javascript_tokens(
                        expression,
                        source_path=source_path,
                        _template_depth=_template_depth + 1,
                    )
                depth -= 1
            pair = source[index : index + 2]
            punctuation = (
                pair
                if pair in {"++", "--", "=>", "?.", "??", "&&", "||", "==", "!="}
                else character
            )
            record_expression("punctuation", punctuation)
            index += len(punctuation)
        raise TypeScriptWorkerError(
            f"Runtime package source has an unterminated template expression: {source_path}"
        )

    def skip_template(start: int) -> int:
        index = start + 1
        while index < len(source):
            if source[index] == "\\":
                index += 2
                continue
            if source.startswith("${", index):
                index, _expression_tokens = template_expression_end(index + 2)
                continue
            if source[index] == "`":
                return index + 1
            index += 1
        raise TypeScriptWorkerError(
            f"Runtime package source has an unterminated template: {source_path}"
        )

    while cursor < len(source):
        character = source[cursor]
        if character.isspace():
            pending_line_break = pending_line_break or character in "\r\n"
            cursor += 1
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            pending_line_break = pending_line_break or newline >= 0
            cursor = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            if end < 0:
                raise TypeScriptWorkerError(
                    f"Runtime package source has an unterminated comment: {source_path}"
                )
            pending_line_break = pending_line_break or any(
                character in "\r\n" for character in source[cursor : end + 2]
            )
            cursor = end + 2
            continue
        if character in {"'", '"'}:
            cursor, value = skip_quoted(cursor, character)
            append("string", value)
            continue
        if character == "`":
            cursor += 1
            start = cursor
            chunks: list[str] = []
            computed = False
            while cursor < len(source):
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source.startswith("${", cursor):
                    computed = True
                    chunks.append(source[start:cursor])
                    cursor, expression_tokens = template_expression_end(cursor + 2)
                    deferred_template_expression_tokens.extend(expression_tokens)
                    start = cursor
                    continue
                if source[cursor] == "`":
                    chunks.append(source[start:cursor])
                    cursor += 1
                    append("computed-template" if computed else "template", "".join(chunks))
                    break
                cursor += 1
            else:
                raise TypeScriptWorkerError(
                    f"Runtime package source has an unterminated template: {source_path}"
                )
            continue
        if character == "/" and slash_starts_regex():
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
        if character.isdigit():
            end = cursor + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                end += 1
            append("number", source[cursor:end])
            cursor = end
            continue
        pair = source[cursor : cursor + 2]
        if pair in {"++", "--", "=>", "?.", "??", "&&", "||", "==", "!="}:
            append("punctuation", pair)
            cursor += 2
        else:
            append("punctuation", character)
            cursor += 1
    tokens.extend(deferred_template_expression_tokens)
    return tuple(tokens)


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
    capabilities: dict[str, str] = {}
    capability_bindings: dict[str, list[tuple[int, str, tuple[int, ...]]]] = {}
    safe_indices: set[int] = set()
    assignments: list[tuple[int, str, int]] = []
    specifiers: set[str] = set()
    loader_maps: set[str] = set()
    scope_paths: list[tuple[int, ...]] = []
    scope_stack: list[int] = []
    scope_control_parentheses: list[bool] = []
    scope_statement_braces: list[bool] = []
    scope_previous_closed_control = False
    scope_previous_closed_statement = False
    for token_index, (_kind, value) in enumerate(tokens):
        scope_paths.append(tuple(scope_stack))
        closes_control = False
        closes_statement = False
        if value == "(":
            scope_control_parentheses.append(_opens_control_flow_parenthesis(tokens[:token_index]))
        elif value == ")":
            closes_control = scope_control_parentheses.pop() if scope_control_parentheses else False
        elif value == "{":
            opens_statement = _opens_statement_brace(
                tokens[:token_index],
                enclosing_statement_brace=(
                    scope_statement_braces[-1] if scope_statement_braces else None
                ),
                previous_closed_control_head=scope_previous_closed_control,
                previous_closed_statement_brace=scope_previous_closed_statement,
            )
            scope_statement_braces.append(opens_statement)
            if opens_statement:
                scope_stack.append(token_index)
        elif value == "}":
            closes_statement = scope_statement_braces.pop() if scope_statement_braces else False
            if closes_statement and scope_stack:
                scope_stack.pop()
        scope_previous_closed_control = closes_control
        scope_previous_closed_statement = closes_statement

    def token_value(index: int) -> str:
        return tokens[index][1] if 0 <= index < len(tokens) else ""

    def matching_close(open_index: int, opening: str = "(", closing: str = ")") -> int | None:
        if token_value(open_index) != opening:
            return None
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

    def assignment_initializer(index: int) -> int | None:
        cursor = index + 1
        if token_value(cursor) == "=":
            return cursor + 1
        if token_value(cursor) != ":":
            return None
        cursor += 1
        closing_for = {"(": ")", "[": "]", "{": "}", "<": ">"}
        expected: list[str] = []
        while cursor < len(tokens):
            value = token_value(cursor)
            if value in closing_for:
                expected.append(closing_for[value])
            elif expected and value == expected[-1]:
                expected.pop()
            elif not expected and value == "=":
                return cursor + 1
            elif not expected and value in {";", ",", ")", "]", "}"}:
                return None
            cursor += 1
        return None

    def static_import_clause(index: int) -> tuple[int, int, str] | None:
        next_index = index + 1
        if next_index >= len(tokens) or token_value(next_index) in {"(", ".", "?."}:
            return None
        if tokens[next_index][0] in {"string", "template"}:
            return next_index, next_index, tokens[next_index][1]
        cursor = next_index
        while cursor < len(tokens) and token_value(cursor) != ";":
            if (
                tokens[cursor] == ("identifier", "from")
                and cursor + 1 < len(tokens)
                and tokens[cursor + 1][0] in {"string", "template"}
            ):
                return next_index, cursor, tokens[cursor + 1][1]
            cursor += 1
        return None

    def set_capability(name: str, capability: str, binding_index: int) -> bool:
        previous = capabilities.get(name)
        if previous is not None and previous != capability:
            raise TypeScriptWorkerError(
                f"Runtime package ambiguously rebinds module-loading capability {name!r}: "
                f"{source_path}"
            )
        safe_indices.add(binding_index)
        binding = (binding_index, capability, scope_paths[binding_index])
        bindings = capability_bindings.setdefault(name, [])
        if binding not in bindings:
            bindings.append(binding)
        if previous is None:
            capabilities[name] = capability
            return True
        return False

    def capability_at(index: int, name: str) -> str | None:
        """Return the nearest proven capability whose lexical brace owns a use."""

        if not 0 <= index < len(scope_paths):
            return None
        reference_scope = scope_paths[index]
        candidates = [
            (len(scope), binding_index, capability)
            for binding_index, capability, scope in capability_bindings.get(name, ())
            if reference_scope[: len(scope)] == scope
        ]
        if not candidates:
            return None
        return max(candidates)[2]

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
        cursor = clause_start
        while cursor < from_index:
            if (
                token_value(cursor) == "*"
                and tokens[cursor + 1 : cursor + 2] == (("identifier", "as"),)
                and cursor + 2 < from_index
                and tokens[cursor + 2][0] == "identifier"
            ):
                set_capability(tokens[cursor + 2][1], "namespace", cursor + 2)
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
                elif imported in {"Module", "default"} and not type_only:
                    set_capability(local, "namespace", local_index)
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
            if imported == "createRequire":
                set_capability(local, "factory", local_index)
            elif imported in {"Module", "default"}:
                set_capability(local, "namespace", local_index)
            cursor += 1

    uninitialized_bindings = {
        tokens[index + 1][1]: index + 1
        for index, token in enumerate(tokens[:-1])
        if token in {("identifier", "let"), ("identifier", "var")}
        and tokens[index + 1][0] == "identifier"
        and token_value(index + 2) in {",", ";", ""}
    }

    # Collect simple assignments, then propagate namespace/factory/loader
    # aliases to a fixed point. This supports forward-hoisted imports and short
    # alias chains while rejecting expression composition below.
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
    changed = True
    while changed:
        changed = False
        for lhs_index, name, initializer in assignments:
            capability = rhs_capability(initializer)
            wrapper_references: set[int] = set()
            if capability is None and token_value(lhs_index - 1) in {"const", "let", "var"}:
                wrapper = bundled_loader_wrapper(initializer)
                if wrapper is not None:
                    end, wrapper_references = wrapper
                    capability = ("loader", end)
            if capability is None:
                continue
            capability_kind, end = capability
            if not safe_rhs_suffix(end):
                raise TypeScriptWorkerError(
                    f"Runtime package ambiguously composes module-loading capability {name!r}: "
                    f"{source_path}"
                )
            changed = set_capability(name, capability_kind, lhs_index) or changed
            safe_indices.update(range(initializer, end))
            safe_indices.update(wrapper_references)
    safe_indices.update(
        binding_index
        for name, binding_index in uninitialized_bindings.items()
        if name in capabilities
    )

    # Any unknown reassignment of a proven capability invalidates the closure.
    for lhs_index, name, initializer in assignments:
        scoped_capability = capability_at(lhs_index, name)
        if scoped_capability is None:
            continue
        capability = rhs_capability(initializer)
        if capability is None or capability[0] != scoped_capability:
            if token_value(lhs_index - 1) in {
                "const",
                "let",
                "static",
                "var",
            } or initializer_is_function(initializer):
                # The token scanner intentionally does not build lexical
                # scopes. A same-spelled inner declaration shadows the outer
                # capability; treating its calls as possible loader calls is a
                # safe over-approximation, while rejecting it would break
                # ordinary bundled code.
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
        if capability is None or token_value(index - 1) in {".", "?."}:
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


def _runtime_module_specifiers(source: str, *, source_path: Path) -> tuple[str, ...]:
    """Extract statically named native ESM and CommonJS runtime loads."""

    tokens = _runtime_javascript_tokens(source, source_path=source_path)
    specifiers: set[str] = set()

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

    for index, (kind, value) in enumerate(tokens):
        if kind != "identifier":
            continue
        previous = tokens[index - 1] if index else None
        if value == "import" and previous != ("punctuation", "."):
            if index + 1 < len(tokens) and tokens[index + 1] == ("punctuation", "("):
                specifier = literal(index + 2)
                if specifier is not None:
                    specifiers.add(specifier)
                continue
            if index + 1 < len(tokens) and tokens[index + 1] == ("identifier", "type"):
                continue
            specifier = literal(index + 1)
            if specifier is not None:
                specifiers.add(specifier)
                continue
            for cursor in range(index + 1, len(tokens) - 1):
                if tokens[cursor] == ("identifier", "from"):
                    specifier = literal(cursor + 1)
                    if specifier is not None:
                        specifiers.add(specifier)
                    break
                if tokens[cursor] == ("punctuation", ";"):
                    break
        elif value == "export" and previous != ("punctuation", "."):
            if index + 1 < len(tokens) and tokens[index + 1] == ("identifier", "type"):
                continue
            for cursor in range(index + 1, len(tokens) - 1):
                if tokens[cursor] == ("identifier", "from"):
                    specifier = literal(cursor + 1)
                    if specifier is not None:
                        specifiers.add(specifier)
                    break
                if tokens[cursor] == ("punctuation", ";"):
                    break
        elif value == "require":
            call_index = index + 1
            is_native_loader = previous is None or previous != ("punctuation", ".")
            if previous == ("punctuation", "."):
                owner = tokens[index - 2] if index >= 2 else None
                is_native_loader = owner == ("identifier", "module")
            if not is_native_loader:
                continue
            if (
                call_index + 2 < len(tokens)
                and tokens[call_index] == ("punctuation", ".")
                and tokens[call_index + 1] == ("identifier", "resolve")
            ):
                call_index += 2
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
