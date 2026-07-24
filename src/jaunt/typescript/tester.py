"""TypeScript test generation and disposable Vitest runner orchestration."""

from __future__ import annotations

import asyncio
import ast
import base64
import binascii
import contextlib
import hashlib
import inspect
import json
import math
import os
import posixpath
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, NoReturn, cast
from urllib.parse import unquote

from jaunt.config import JauntConfig
from jaunt.cache import ResponseCache
from jaunt.cost import CostTracker
from jaunt.errors import (
    JauntBudgetExceededError,
    JauntConfigError,
    JauntGenerationError,
    JauntQuotaGenerationError,
)
from jaunt.generate.base import GenerationRequest, GenerationResult, GeneratorBackend, TokenUsage
from jaunt.generate.request_cache import (
    discard_cached_generation,
    generate_request_cached,
    store_generation_result,
)
from jaunt.journal import JournalEvent, append_events
from jaunt.skill_seed import skills_fingerprint
from jaunt.targets.base import TargetBuildReport, TargetDiagnostic, TargetTestReport
from jaunt.typescript.builder import (
    MISSING_INPUT,
    WorkerLike,
    WorkerFactory,
    _CommittedBatteryInfrastructureError,
    _PinnedDirectory,
    _PinnedWorkspace,
    _assert_inputs_unchanged,
    _default_backend,
    _input_hashes,
    _module_id,
    _model_contract,
    _path_hash,
    _prompt_text,
    _progress_advance,
    _progress_finish,
    _progress_phase,
    _progress_reset,
    _acquire_transaction_lease,
    _recover_atomic_write_manifests,
    _retire_transaction_manifest,
    _safe_path,
    _seal_worker_runtime_identity,
    _sha256,
    _split_context_source,
    _target,
    _verify_worker_runtime_identity,
    _write_transaction_manifest,
    _Write,
    analyze,
    atomic_write_manifest,
    run_build,
    run_build_in_session,
    worker_session,
)
from jaunt.typescript.properties import (
    PROPERTY_RENDERER_SCHEME,
    attach_property_block,
    parse_property_cases,
    render_property_block,
)
from jaunt.typescript.provenance import (
    canonical_managed_body,
    parse_managed_document,
    render_managed_document,
)
from jaunt.typescript.reuse import (
    expected_target_api_record,
    proven_previous_target_api_digests,
    target_api_digest,
)
from jaunt.typescript.worker import (
    _ordered_json_identity,
    _runtime_package_import_match,
    _runtime_package_identity_files,
    _runtime_package_owner,
    _runtime_package_resolution_closure,
    _unsafe_runtime_package_import_fragment,
    compiler_runtime_identity,
    resolve_node_package,
    runtime_package_identity,
    TypeScriptWorkerError,
    WorkerInstallation,
    WorkerToolchainChangedError,
    worker_environment,
)

_DEFAULT_RUNNER_TIMEOUT = 300.0
_RUNNER_PROTOCOL = "jaunt-ts-test-runner/1"
_RUNNER_ENTRY = "dist/test/runner.js"
_RUNNER_REQUIRED_FILES = (
    _RUNNER_ENTRY,
    "dist/test/permission_guard.cjs",
    "dist/test/reporter.js",
    "dist/test/heldout.js",
)
_TEST_SPEC_RE = re.compile(r"\.jaunt-test\.(?:ts|tsx)$")
_GENERATED_TEST_HEADER = "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`."
_TEST_PROVENANCE_FIELDS = (
    "test_spec_digest",
    "target_api_digest",
    "imported_type_context_fingerprint",
    "fixture_fingerprint",
    "vitest_fingerprint",
    "fast_check_fingerprint",
    "runner_fingerprint",
    "prompt_fingerprint",
    "policy_fingerprint",
    "skills_fingerprint",
    "battery_fingerprint",
    "body_digest",
)
_TEST_REHEADER_FINGERPRINTS = frozenset({"runner_fingerprint", "vitest_fingerprint"})
_TEST_IMPORT_POLICY = "static-esm-only-resolved-boundary-v3"
_REJECTED_TEST_DIR = Path(".jaunt/typescript/rejected-tests")
_REJECTED_TEST_STEM_MAX_CHARS = 96
_REJECTED_TEST_SEMANTIC_FIELDS = ("test_spec_digest", "target_api_digest")
_REJECTED_TEST_OPTIONAL_SEMANTIC_FIELDS = (
    "imported_type_context_fingerprint",
    "fixture_fingerprint",
)
_IMPORTED_TYPE_SOURCE_RE = re.compile(
    r"^// <jaunt:imported-type-source (?P<meta>\{[^\r\n]+\})>\r?\n"
    r"(?P<source>.*?)"
    r"^// </jaunt:imported-type-source>$",
    re.MULTILINE | re.DOTALL,
)
_IMPORTED_TYPE_SOURCE_V2_RE = re.compile(
    r"^// jaunt:imported-type-record=(?P<payload>[A-Za-z0-9+/]+={0,2})$",
    re.MULTILINE,
)
_IMPORTED_TYPE_CONTEXT_LIMIT = 64 * 1024


def _test_output(
    path: str,
    generated_dir: str = "__generated__",
    tier: str = "example",
) -> str:
    source = Path(path)
    stem = _TEST_SPEC_RE.sub("", source.name)
    extension = ".tsx" if source.suffix == ".tsx" else ".ts"
    return (source.parent / generated_dir / f"{stem}.{tier}.test{extension}").as_posix()


def _runtime_import_specifier(target_path: str, facade_path: str) -> str:
    """Return the emitted-JavaScript specifier a generated test must import."""

    if not facade_path:
        return ""
    emitted_facade = str(Path(facade_path).with_suffix(".js")).replace("\\", "/")
    relative = posixpath.relpath(emitted_facade, posixpath.dirname(target_path))
    return relative if relative.startswith(".") else f"./{relative}"


def _canonical_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        default=str,
    ).encode("utf-8")
    return _sha256(encoded)


def _semantic_test_spec_digest(source: str) -> str:
    """Hash TypeScript test intent while ignoring formatting-only trivia.

    Test intent commonly lives in comments, so comments remain semantic inputs.
    The lightweight lexer preserves string/template/comment content but removes
    whitespace between TypeScript tokens. The authored test-spec grammar is
    deliberately narrow, which keeps this canonicalization deterministic without
    executing or importing the project-local compiler.
    """

    source = source.replace("\r\n", "\n").replace("\r", "\n")
    tokens: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        character = source[index]
        if character.isspace():
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = length if end < 0 else end
            text = " ".join(source[index + 2 : end].split())
            tokens.append(f"line-comment:{text}")
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = length - 2 if end < 0 else end
            text = " ".join(source[index + 2 : end].split())
            tokens.append(f"block-comment:{text}")
            index = min(length, end + 2)
            continue
        if character in {"'", '"', "`"}:
            quote = character
            end = index + 1
            escaped = False
            while end < length:
                current = source[end]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == quote:
                    end += 1
                    break
                end += 1
            tokens.append(f"literal:{source[index:end]}")
            index = end
            continue
        if character.isalnum() or character in {"_", "$"}:
            end = index + 1
            while end < length and (source[end].isalnum() or source[end] in {"_", "$"}):
                end += 1
            tokens.append(f"word:{source[index:end]}")
            index = end
            continue
        tokens.append(f"punct:{character}")
        index += 1
    return _canonical_digest(tokens)


def _read_package_version(search_roots: Sequence[Path], package: str) -> str:
    package_path = Path(*package.split("/"))
    for base in search_roots:
        current = base.resolve()
        while True:
            candidate = current / "node_modules" / package_path / "package.json"
            try:
                parsed = json.loads(candidate.read_text(encoding="utf-8"))
            except (FileNotFoundError, UnicodeError, json.JSONDecodeError, OSError):
                parsed = None
            if isinstance(parsed, Mapping) and isinstance(parsed.get("version"), str):
                return str(parsed["version"])
            if current.parent == current:
                break
            current = current.parent
    return "unresolved"


def _test_package_owner(
    root: Path,
    workspace: Mapping[str, Any],
    project: str,
) -> Path:
    """Resolve the package that owns one worker-resolved test project."""

    raw_projects = workspace.get("projects", [])
    if isinstance(raw_projects, list):
        for item in raw_projects:
            if not isinstance(item, Mapping):
                continue
            identifier = item.get("id", item.get("configPath"))
            if identifier != project or not isinstance(item.get("packageOwner"), str):
                continue
            return _safe_path(root, str(item["packageOwner"]))

    current = _safe_path(root, project).parent
    while True:
        if (current / "package.json").is_file():
            return current
        if current == root:
            break
        current = current.parent
    raise JauntConfigError(
        f"TypeScript test project {project!r} has no owning package.json inside the Jaunt root"
    )


def _declared_dev_dependencies(owner: Path) -> Mapping[str, object]:
    manifest_path = owner / "package.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JauntConfigError(f"Invalid test-owner package manifest: {manifest_path}") from exc
    dependencies = manifest.get("devDependencies") if isinstance(manifest, Mapping) else None
    return dependencies if isinstance(dependencies, Mapping) else {}


def _installed_test_dependency(root: Path, owner: Path, package: str) -> Path | None:
    """Resolve a test tool from its actual package owner, preserving symlinks."""

    try:
        return resolve_node_package(owner, package, boundary=root)
    except TypeScriptWorkerError as exc:
        raise JauntConfigError(
            f"Could not resolve {package!r} from test package owner {owner}"
        ) from exc


def _module_resolved_test_dependency(module_path: Path, package: str) -> Path | None:
    """Resolve a bare package from the physical module location Node executes.

    Jaunt strips ``NODE_OPTIONS``, so Node does not preserve package symlinks.
    Starting at the runner's physical path therefore mirrors ESM's parent
    ``node_modules`` search and, importantly, enters pnpm's peer-context tree.
    The returned candidate stays lexical within that physical tree so a peer
    symlink retarget changes its command-local filesystem identity.
    """

    try:
        return resolve_node_package(module_path, package, module_path=True)
    except TypeScriptWorkerError as exc:
        raise JauntConfigError(f"Could not resolve protected test runner: {module_path}") from exc


def _runner_test_dependency(client: object, package: str) -> Path | None:
    try:
        runner = _runner_path(client)
    except RuntimeError:
        return None
    return _module_resolved_test_dependency(runner, package)


def _installed_test_dependency_version(root: Path, owner: Path, package: str) -> str | None:
    package_root = _installed_test_dependency(root, owner, package)
    if package_root is not None:
        try:
            parsed = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping) and isinstance(parsed.get("version"), str):
            return str(parsed["version"])
    return None


def _test_dependency_runtime_identity(root: Path, owner: Path, package: str) -> str:
    package_root = _installed_test_dependency(root, owner, package)
    if package_root is None:
        return "unresolved"
    return runtime_package_identity(package_root, expected_name=package)


def _pin_test_dependency_runtimes(
    client: object,
    root: Path,
    workspace: Mapping[str, Any],
    grouped: Mapping[str, Sequence[str]],
) -> None:
    """Pin each Vitest resolution topology through the artifact commit."""

    pin_closure = getattr(client, "pin_package_resolution_closure", None)
    pin_resolution = getattr(client, "pin_package_resolution_identity", None)
    pin_runtime = getattr(client, "pin_package_runtime_identity", None)
    if not callable(pin_closure) and not callable(pin_resolution) and not callable(pin_runtime):
        return
    try:
        runner = _runner_path(client)
    except RuntimeError as exc:
        raise JauntConfigError(
            "The protected TypeScript runner cannot resolve its Vitest runtime"
        ) from exc
    runner_package = _module_resolved_test_dependency(runner, "vitest")
    if runner_package is None:
        raise JauntConfigError("The protected TypeScript runner cannot resolve its Vitest runtime")
    if callable(pin_closure):
        pin_closure(
            "Vitest package resolved by the protected runner",
            runner,
            "vitest",
            module_path=True,
            expected_name="vitest",
        )
    elif callable(pin_resolution):
        pin_resolution(
            "Vitest package resolved by the protected runner",
            runner,
            "vitest",
            module_path=True,
            expected_name="vitest",
        )
    else:
        assert callable(pin_runtime)
        pin_runtime(
            "Vitest package resolved by the protected runner",
            runner_package,
            expected_name="vitest",
        )
    for project in grouped:
        owner = _test_package_owner(root, workspace, project)
        package_root = _installed_test_dependency(root, owner, "vitest")
        if package_root is None:
            relative_owner = owner.relative_to(root.resolve()).as_posix() or "."
            raise JauntConfigError(
                f"TypeScript test owner {relative_owner!r} does not resolve installed vitest"
            )
        relative_owner = owner.relative_to(root.resolve()).as_posix() or "."
        if callable(pin_closure):
            pin_closure(
                f"Vitest package for {relative_owner}",
                owner,
                "vitest",
                boundary=root,
                expected_name="vitest",
            )
        elif callable(pin_resolution):
            pin_resolution(
                f"Vitest package for {relative_owner}",
                owner,
                "vitest",
                boundary=root,
                expected_name="vitest",
            )
        else:
            assert callable(pin_runtime)
            pin_runtime(
                f"Vitest package for {relative_owner}",
                package_root,
                expected_name="vitest",
            )


def _supported_test_dependency(package: str, version: str) -> bool:
    match = re.match(r"^(\d+)\.(\d+)(?:\.|$)", version)
    if match is None:
        return False
    major = int(match.group(1))
    if package == "vitest":
        return 3 <= major < 5
    if package == "fast-check":
        return major == 4
    return True


def _validate_test_owner_dependencies(
    root: Path,
    workspace: Mapping[str, Any],
    grouped: Mapping[str, Sequence[str]],
    *,
    overlays: Mapping[str, str] | None = None,
    require_fast_check: bool = False,
) -> None:
    """Require test tooling at each package boundary, not merely in a hoisted tree."""

    overlays = overlays or {}
    for project, files in grouped.items():
        owner = _test_package_owner(root, workspace, project)
        declared = _declared_dev_dependencies(owner)
        property_sources: list[str] = []
        for path in files:
            if path in overlays:
                property_sources.append(overlays[path])
                continue
            candidate = _safe_path(root, path)
            if candidate.is_file():
                property_sources.append(candidate.read_text(encoding="utf-8"))
        required = {"vitest"}
        if require_fast_check or any(
            re.search(r"(?:from\s+|import\s*\()[\"']fast-check[\"']", source)
            for source in property_sources
        ):
            required.add("fast-check")
        missing = sorted(name for name in required if name not in declared)
        if missing:
            relative_owner = owner.relative_to(root).as_posix() or "."
            packages = " ".join(missing)
            raise JauntConfigError(
                f"TypeScript test owner {relative_owner!r} must directly declare "
                f"devDependencies: {', '.join(missing)}. Run `npm install -D {packages}` "
                f"in {relative_owner}."
            )
        for package in sorted(required):
            version = _installed_test_dependency_version(root, owner, package)
            relative_owner = owner.relative_to(root).as_posix() or "."
            if version is None:
                raise JauntConfigError(
                    f"TypeScript test owner {relative_owner!r} declares {package} but it is not "
                    f"installed in that package tree. Run `npm install` in {relative_owner}."
                )
            if not _supported_test_dependency(package, version):
                supported = ">=3 <5" if package == "vitest" else ">=4 <5"
                raise JauntConfigError(
                    f"TypeScript test owner {relative_owner!r} resolves unsupported "
                    f"{package} {version}; install {package}@'{supported}'."
                )


def _tool_search_roots(root: Path, client: object) -> tuple[Path, ...]:
    installation = getattr(client, "installation", None)
    values = (
        getattr(installation, "tool_owner", None),
        getattr(installation, "package_root", None),
        root,
    )
    return tuple(dict.fromkeys(path for path in values if isinstance(path, Path)))


_LOCAL_CONFIG_EXTENSIONS = (
    ".ts",
    ".tsx",
    ".mts",
    ".cts",
    ".js",
    ".mjs",
    ".cjs",
    ".json",
)
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

    # A case expression can contain nested object literals and ternaries. Find
    # the clause head at this delimiter's level, then require every preceding
    # top-level ``?`` to have consumed a ``:`` before treating this colon as
    # the case delimiter rather than as part of the expression.
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

    # Function and class expressions are values (and can therefore be a
    # division operand); only declaration bodies create a statement boundary.
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


def _typescript_lexical_regions(
    source: str,
) -> tuple[tuple[tuple[int, int], ...], tuple[tuple[int, int, str], ...]]:
    """Locate comments and string-like literals without executing a config."""

    comments: list[tuple[int, int]] = []
    strings: list[tuple[int, int, str]] = []
    index = 0
    previous_kind = ""
    previous_value = ""
    significant_tokens: list[tuple[str, str]] = []
    control_parentheses: list[bool] = []
    statement_braces: list[bool] = []
    previous_closed_control_head = False
    previous_closed_statement_brace = False
    pending_line_break = False

    def record(kind: str, value: str) -> None:
        nonlocal previous_closed_control_head, previous_closed_statement_brace
        nonlocal pending_line_break, previous_kind, previous_value
        closes_control_head = False
        closes_statement_brace = False
        if kind == "punctuation" and value == "(":
            control_parentheses.append(_opens_control_flow_parenthesis(significant_tokens))
        elif kind == "punctuation" and value == ")":
            closes_control_head = control_parentheses.pop() if control_parentheses else False
        elif kind == "punctuation" and value == "{":
            statement_braces.append(
                _opens_statement_brace(
                    significant_tokens,
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
        significant_tokens.append((kind, value))
        previous_kind = kind
        previous_value = value
        pending_line_break = False

    def slash_starts_regex() -> bool:
        if not previous_kind:
            return True
        if previous_kind in {"number", "regex", "string"}:
            return False
        if previous_kind == "identifier":
            return _identifier_precedes_regex(significant_tokens)
        if previous_value == ")" and previous_closed_control_head:
            return True
        if previous_value == "}" and previous_closed_statement_brace:
            return True
        return previous_value not in {")", "]", "}", "++", "--"}

    while index < len(source):
        if source[index].isspace():
            pending_line_break = pending_line_break or source[index] in "\r\n"
            index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            end = len(source) if end < 0 else end
            comments.append((index, end))
            pending_line_break = pending_line_break or end < len(source)
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            end = len(source) if end < 0 else end + 2
            comments.append((index, end))
            pending_line_break = pending_line_break or any(
                character in "\r\n" for character in source[index:end]
            )
            index = end
            continue
        quote = source[index]
        if quote == "/" and slash_starts_regex():
            end = _skip_typescript_regex(source, index)
            if end > index + 1:
                comments.append((index, end))
                index = end
                record("regex", "regex")
                continue
        if quote not in {"'", '"', "`"}:
            if quote.isalpha() or quote in "_$":
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                record("identifier", source[index:end])
                index = end
                continue
            if quote.isdigit():
                end = index + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                    end += 1
                record("number", source[index:end])
                index = end
                continue
            pair = source[index : index + 2]
            record("punctuation", pair if pair in {"++", "--", "?.", "=>"} else quote)
            index += 2 if pair in {"++", "--", "?.", "=>"} else 1
            continue
        start = index
        index += 1
        escaped = False
        while index < len(source):
            character = source[index]
            index += 1
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                break
        strings.append((start, index, quote))
        record("string", "literal")
    return tuple(comments), tuple(strings)


def _inside_region(index: int, regions: Sequence[tuple[int, int]]) -> bool:
    return any(start <= index < end for start, end in regions)


def _root_local_config_import(root: Path, specifier: str) -> bool:
    base = root / specifier.replace("\\", "/")
    candidates = [base]
    if base.suffix.casefold() not in _LOCAL_CONFIG_EXTENSIONS:
        candidates.extend(Path(f"{base}{extension}") for extension in _LOCAL_CONFIG_EXTENSIONS)
    candidates.extend(base / f"index{extension}" for extension in _LOCAL_CONFIG_EXTENSIONS)
    return any(candidate.is_file() for candidate in candidates)


def _named_module_clause_is_type_only(clause: str) -> bool:
    """Return whether every named import/export binding is explicitly type-only."""

    if not clause.startswith("{") or not clause.endswith("}"):
        return False
    bindings = re.sub(r"/\*.*?\*/|//[^\r\n]*", " ", clause[1:-1], flags=re.DOTALL)
    parts = [part.strip() for part in bindings.split(",") if part.strip()]
    return bool(parts) and all(re.match(r"^type\s+(?!as\b)", part) for part in parts)


def _config_syntax_tokens(
    source: str,
    *,
    _template_depth: int = 0,
) -> tuple[tuple[str, str], ...]:
    """Tokenize executable config syntax without evaluating it.

    Comments, regex bodies, and inert template text are ignored. Template
    expressions are scanned recursively because they can execute imports.
    """

    if _template_depth > 64:
        raise JauntConfigError("Vitest configuration has excessively nested templates")

    tokens: list[tuple[str, str]] = []
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
        if kind in {"number", "regex", "string"}:
            return False
        if kind == "identifier":
            return _identifier_precedes_regex(tokens)
        if value == ")" and previous_closed_control_head:
            return True
        if value == "}" and previous_closed_statement_brace:
            return True
        return value not in {")", "]", "}", "++", "--"}

    while cursor < len(source):
        next_cursor = _skip_typescript_trivia(source, cursor)
        if next_cursor != cursor:
            pending_line_break = pending_line_break or any(
                character in "\r\n" for character in source[cursor:next_cursor]
            )
            cursor = next_cursor
            continue
        if cursor >= len(source):
            break
        character = source[cursor]
        if character in {'"', "'"}:
            parsed = _read_typescript_string_literal(source, cursor)
            if parsed is None:
                raise JauntConfigError("Vitest configuration has an invalid string literal")
            value, cursor = parsed
            append("string", value)
            continue
        if character == "`":
            parsed = _read_typescript_string_literal(source, cursor)
            if parsed is not None:
                value, cursor = parsed
                append("string", value)
                continue
            end, expressions = _typescript_template_expressions(source, cursor)
            if end >= len(source) and (not source or source[-1] != "`"):
                raise JauntConfigError("Vitest configuration has an unterminated template")
            for expression in expressions:
                nested = _config_syntax_tokens(
                    expression,
                    _template_depth=_template_depth + 1,
                )
                tokens.extend(nested)
                if nested:
                    previous = nested[-1]
                    previous_closed_control_head = False
                    previous_closed_statement_brace = False
            cursor = end
            continue
        if character == "/" and slash_starts_regex():
            end = _skip_typescript_regex(source, cursor)
            if end > cursor + 1:
                cursor = end
                previous = ("regex", "regex")
                previous_closed_control_head = False
                previous_closed_statement_brace = False
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
        if pair in {"++", "--", "?.", "=>"}:
            append("punctuation", pair)
            cursor += 2
            continue
        append("punctuation", character)
        cursor += 1
    return tuple(tokens)


def _config_module_specifiers(source: str) -> tuple[str, ...]:
    """Return static module specifiers that executable config syntax can load.

    The scanner deliberately recognizes only provenance-backed CommonJS loader
    aliases.  A function merely named ``createRequire`` is not enough: its
    binding must come from Node's ``module`` builtin.  Once a proven loader is
    reassigned or used through an unsupported member, the config is rejected
    instead of risking an incomplete runtime fingerprint.
    """

    tokens = _config_syntax_tokens(source)
    specifiers: set[str] = set()
    node_module_specifiers = {"module", "node:module"}
    module_namespaces: set[str] = set()
    namespace_binding_indices: set[int] = set()
    create_require_factories: set[str] = set()
    factory_binding_indices: set[int] = set()
    loaders: set[str] = {"require"}
    protected_loaders: set[str] = {"require"}
    invalid_namespaces: set[str] = set()
    invalid_factories: set[str] = set()
    invalid_loaders: set[str] = set()
    assignment_rhs_owners: dict[int, str] = {}
    type_annotation_indices: set[int] = set()
    exported_binding_names: set[str] = set()
    export_capability_indices: set[int] = set()

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

    def assignment_initializer(index: int) -> tuple[int, range] | None:
        """Return a direct assignment's RHS and any non-executing type span."""

        cursor = index + 1
        if token_value(cursor) == "=":
            return cursor + 1, range(0)
        if token_value(cursor) != ":":
            return None
        annotation_start = cursor + 1
        cursor = annotation_start
        closing_for = {"(": ")", "[": "]", "{": "}", "<": ">"}
        expected_closings: list[str] = []
        while cursor < len(tokens):
            value = token_value(cursor)
            if value in closing_for:
                expected_closings.append(closing_for[value])
            elif expected_closings and value == expected_closings[-1]:
                expected_closings.pop()
            elif not expected_closings and value == "=":
                return cursor + 1, range(annotation_start, cursor)
            elif not expected_closings and value in {";", ",", ")", "]", "}"}:
                return None
            cursor += 1
        return None

    def annotation_names_node_module(annotation: range) -> bool:
        """Return whether a type span explicitly imports Node's module namespace."""

        return any(
            tokens[cursor : cursor + 4]
            == (
                ("identifier", "import"),
                ("punctuation", "("),
                ("string", specifier),
                ("punctuation", ")"),
            )
            for cursor in annotation
            for specifier in node_module_specifiers
        )

    def register_export_contexts() -> None:
        """Record structural local exports without relying on semicolons or ASI."""

        for export_index, token in enumerate(tokens):
            if token != ("identifier", "export") or token_value(export_index - 1) in {
                ".",
                "?.",
            }:
                continue
            start = export_index + 1
            if token_value(start) == "type":
                continue
            head = token_value(start)
            if head == "default":
                reference = start + 1
                if reference < len(tokens) and tokens[reference][0] == "identifier":
                    export_capability_indices.add(reference)
                elif token_value(reference) == "{":
                    close = matching_close(reference, "{", "}")
                    if close is not None:
                        for cursor in range(reference + 1, close):
                            if tokens[cursor][0] != "identifier":
                                continue
                            property_key = token_value(cursor + 1) == ":" and token_value(
                                cursor - 1
                            ) in {"{", ","}
                            if not property_key:
                                export_capability_indices.add(cursor)
                continue
            if head == "{":
                close = matching_close(start, "{", "}")
                if close is None or token_value(close + 1) == "from":
                    continue
                cursor = start + 1
                while cursor < close:
                    type_only = tokens[cursor] == ("identifier", "type")
                    if type_only:
                        cursor += 1
                    if cursor < close and tokens[cursor][0] == "identifier":
                        if not type_only:
                            export_capability_indices.add(cursor)
                        while cursor < close and token_value(cursor) != ",":
                            cursor += 1
                    cursor += 1
                continue
            if head in {"const", "let", "var"}:
                binding = start + 1
                if binding < len(tokens) and tokens[binding][0] == "identifier":
                    exported_binding_names.add(tokens[binding][1])
                elif token_value(binding) in {"{", "["}:
                    closing = "}" if token_value(binding) == "{" else "]"
                    close = matching_close(binding, token_value(binding), closing)
                    if close is not None:
                        for cursor in range(binding + 1, close):
                            if tokens[cursor][0] != "identifier":
                                continue
                            if token_value(cursor + 1) == ":":
                                continue
                            exported_binding_names.add(tokens[cursor][1])

    def capability_is_exported(index: int) -> bool:
        if index not in export_capability_indices:
            return False
        if token_value(index + 1) in {"(", ".", "?."}:
            # The expression exports the call/member result, not the loader
            # capability itself. Factory calls that return a loader are still
            # rejected by the normal unassigned-result check below.
            return False
        # An object key is not a reference to an in-scope capability with the
        # same spelling. Shorthand and property values remain executable uses.
        return not (token_value(index + 1) == ":" and token_value(index - 1) in {"{", ","})

    def reject_exported_capability(index: int) -> None:
        if capability_is_exported(index):
            raise JauntConfigError(
                "Vitest configuration exports a tracked module-loading capability; "
                "exported helpers cannot be fingerprinted safely"
            )

    def literal_call_argument(open_index: int) -> str:
        argument = open_index + 1
        if (
            argument >= len(tokens)
            or tokens[argument][0] != "string"
            or token_value(argument + 1) != ")"
        ):
            raise JauntConfigError(
                "Vitest configuration uses a computed import/require specifier that cannot "
                "be fingerprinted safely"
            )
        return tokens[argument][1]

    def capability_has_unsafe_suffix(index: int) -> bool:
        if index >= len(tokens):
            return False
        kind, value = tokens[index]
        if kind == "identifier":
            # Other identifiers either begin an ASI-separated statement or
            # are the non-executing TypeScript ``as``/``satisfies`` suffix.
            return value in {"in", "instanceof"}
        return value not in {";", ",", ")", "]", "}"}

    def static_import_clause(index: int) -> tuple[int, int, str] | None:
        """Return ``(clause_start, from_index, specifier)`` for a static import."""

        next_index = index + 1
        if next_index >= len(tokens) or token_value(next_index) in {"(", "."}:
            return None
        if tokens[next_index][0] == "string":
            return next_index, next_index, tokens[next_index][1]
        cursor = next_index
        while cursor < len(tokens) and token_value(cursor) != ";":
            if (
                tokens[cursor] == ("identifier", "from")
                and cursor + 1 < len(tokens)
                and tokens[cursor + 1][0] == "string"
            ):
                return next_index, cursor, tokens[cursor + 1][1]
            cursor += 1
        return None

    def register_node_module_imports() -> None:
        """Record hoisted ESM bindings from the Node module builtin."""

        for index, token in enumerate(tokens):
            if token != ("identifier", "import"):
                continue
            previous = token_value(index - 1)
            if previous in {".", "?."}:
                continue
            clause = static_import_clause(index)
            if clause is None:
                # TypeScript's ``import Module = require("node:module")``.
                if (
                    index + 5 < len(tokens)
                    and tokens[index + 1][0] == "identifier"
                    and token_value(index + 2) == "="
                    and tokens[index + 3] == ("identifier", "require")
                    and token_value(index + 4) == "("
                    and tokens[index + 5][0] == "string"
                    and tokens[index + 5][1] in node_module_specifiers
                ):
                    module_namespaces.add(tokens[index + 1][1])
                    namespace_binding_indices.add(index + 1)
                continue
            clause_start, from_index, specifier = clause
            if specifier not in node_module_specifiers or clause_start == from_index:
                continue
            clause_tokens = tokens[clause_start:from_index]
            if clause_tokens and clause_tokens[0] == ("identifier", "type"):
                continue
            if clause_tokens and clause_tokens[0][0] == "identifier":
                # ``module`` has a default export containing createRequire.
                module_namespaces.add(clause_tokens[0][1])
                namespace_binding_indices.add(clause_start)
            cursor = clause_start
            while cursor < from_index:
                if token_value(cursor) == "*" and tokens[cursor + 1 : cursor + 2] == (
                    ("identifier", "as"),
                ):
                    if cursor + 2 < from_index and tokens[cursor + 2][0] == "identifier":
                        module_namespaces.add(tokens[cursor + 2][1])
                        namespace_binding_indices.add(cursor + 2)
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
                    local = imported
                    local_index = binding
                    if (
                        binding + 2 < close
                        and tokens[binding + 1] == ("identifier", "as")
                        and tokens[binding + 2][0] == "identifier"
                    ):
                        local = tokens[binding + 2][1]
                        binding += 2
                        local_index = binding
                    if imported == "createRequire" and not type_only:
                        create_require_factories.add(local)
                        factory_binding_indices.add(local_index)
                    binding += 1
                cursor = close + 1

    def loader_call_open(index: int) -> int | None:
        cursor = index + 1
        if token_value(cursor) == "?.":
            cursor += 1
        return cursor if token_value(cursor) == "(" else None

    def loader_resolve_open(index: int) -> int | None:
        cursor = index + 1
        if token_value(cursor) in {".", "?."}:
            cursor += 1
            if tokens[cursor : cursor + 1] == (("identifier", "resolve"),):
                cursor += 1
                return cursor if token_value(cursor) == "(" else None
        return None

    def factory_reference_end(index: int) -> int | None:
        if tokens[index][0] != "identifier":
            return None
        name = tokens[index][1]
        if name in invalid_factories:
            raise JauntConfigError(
                f"Vitest configuration reassigns or ambiguously uses createRequire alias {name!r}"
            )
        if name in create_require_factories:
            return index + 1
        if name in invalid_namespaces:
            if token_value(index + 1) in {".", "?."}:
                raise JauntConfigError(
                    f"Vitest configuration reassigns Node module namespace {name!r}"
                )
            return None
        if name in module_namespaces and token_value(index + 1) == "[":
            raise JauntConfigError(
                f"Vitest configuration uses computed access on Node module namespace {name!r}"
            )
        if (
            name in module_namespaces
            and token_value(index + 1) in {".", "?."}
            and tokens[index + 2 : index + 3] == (("identifier", "createRequire"),)
        ):
            return index + 3
        return None

    def node_module_value_end(index: int) -> int | None:
        if index >= len(tokens):
            return None
        if tokens[index][0] == "identifier" and tokens[index][1] in module_namespaces:
            return index + 1
        cursor = index
        if tokens[cursor : cursor + 1] == (("identifier", "await"),):
            cursor += 1
        if tokens[cursor : cursor + 1] == (("identifier", "import"),):
            open_index = cursor + 1
        elif tokens[cursor : cursor + 1] == (("identifier", "require"),):
            if "require" in invalid_loaders:
                raise JauntConfigError("Vitest configuration reassigns the require loader")
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
            or tokens[open_index + 1][0] != "string"
        ):
            return None
        if tokens[open_index + 1][1] not in node_module_specifiers:
            return None
        close = matching_close(open_index)
        return None if close is None else close + 1

    def rhs_capability(index: int) -> tuple[str, int] | None:
        if index >= len(tokens):
            return None
        factory_end = factory_reference_end(index)
        if factory_end is not None:
            if token_value(factory_end) != "(":
                if capability_has_unsafe_suffix(factory_end):
                    raise JauntConfigError(
                        "Vitest configuration ambiguously composes a createRequire factory"
                    )
                return "factory", factory_end
            close = matching_close(factory_end)
            if close is None:
                raise JauntConfigError(
                    "Vitest configuration has an unterminated createRequire call"
                )
            return "loader", close + 1
        namespace_end = node_module_value_end(index)
        if namespace_end is not None:
            return "namespace", namespace_end
        if tokens[index][0] == "identifier":
            name = tokens[index][1]
            if name in invalid_loaders:
                raise JauntConfigError(
                    f"Vitest configuration reassigns or ambiguously uses loader alias {name!r}"
                )
            if name in loaders and token_value(index + 1) not in {"(", ".", "?.", "["}:
                if capability_has_unsafe_suffix(index + 1):
                    raise JauntConfigError(
                        f"Vitest configuration ambiguously composes loader alias {name!r}"
                    )
                return "loader", index + 1
        return None

    def set_capability(name: str, capability: str) -> None:
        if name in exported_binding_names:
            raise JauntConfigError(
                "Vitest configuration exports a tracked module-loading capability; "
                "exported helpers cannot be fingerprinted safely"
            )
        module_namespaces.discard(name)
        create_require_factories.discard(name)
        loaders.discard(name)
        protected_loaders.discard(name)
        invalid_namespaces.discard(name)
        invalid_factories.discard(name)
        invalid_loaders.discard(name)
        if capability == "namespace":
            module_namespaces.add(name)
        elif capability == "factory":
            create_require_factories.add(name)
        else:
            loaders.add(name)
            protected_loaders.add(name)

    def invalidate_capability(name: str) -> None:
        if name in module_namespaces or name in invalid_namespaces:
            module_namespaces.discard(name)
            invalid_namespaces.add(name)
        if name in create_require_factories or name in invalid_factories:
            create_require_factories.discard(name)
            invalid_factories.add(name)
        if name in loaders or name in invalid_loaders:
            loaders.discard(name)
            invalid_loaders.add(name)

    register_export_contexts()
    register_node_module_imports()

    index = 0
    while index < len(tokens):
        kind, value = tokens[index]
        if kind != "identifier":
            index += 1
            continue
        if index in type_annotation_indices:
            index += 1
            continue
        if (
            value == "module"
            and token_value(index + 1) == "["
            and tokens[index + 2 : index + 3] == (("string", "require"),)
            and token_value(index + 3) == "]"
        ):
            raise JauntConfigError(
                "Vitest configuration uses computed access to module.require; "
                "use direct literal module.require calls"
            )
        if value in {"import", "export"}:
            previous = token_value(index - 1)
            if previous in {".", "?."}:
                index += 1
                continue
            next_index = index + 1
            if next_index >= len(tokens):
                break
            next_kind, next_value = tokens[next_index]
            if value == "import" and next_value == ".":
                index += 1
                continue
            if value == "import" and next_value == "(":
                specifiers.add(literal_call_argument(next_index))
                index += 1
                continue
            if value == "import" and next_kind == "string":
                specifiers.add(next_value)
                index += 1
                continue
            if value == "export":
                export_head = next_value
                if export_head == "type":
                    export_head = tokens[next_index + 1][1] if next_index + 1 < len(tokens) else ""
                if export_head not in {"{", "*"}:
                    # `export default`, declarations, and assignments are not
                    # re-export clauses. Any nested dynamic load is still seen
                    # when its own token is visited.
                    index += 1
                    continue

            from_index: int | None = None
            cursor = next_index
            while cursor < len(tokens):
                current_kind, current_value = tokens[cursor]
                if current_value == ";":
                    break
                if (
                    current_kind == "identifier"
                    and current_value == "from"
                    and cursor + 1 < len(tokens)
                    and tokens[cursor + 1][0] == "string"
                ):
                    from_index = cursor
                    break
                cursor += 1
            if from_index is None or from_index + 1 >= len(tokens):
                index += 1
                continue
            _literal_kind, specifier = tokens[from_index + 1]
            clause = tokens[next_index:from_index]
            type_only = bool(clause and clause[0] == ("identifier", "type"))
            if not type_only and clause and clause[0][1] == "{" and clause[-1][1] == "}":
                type_only = _named_module_clause_is_type_only(
                    " ".join(token_value for _token_kind, token_value in clause)
                )
            if not type_only:
                if value == "export" and specifier in node_module_specifiers:
                    raise JauntConfigError(
                        "Vitest configuration re-exports the Node module runtime; "
                        "module-loading capabilities cannot cross captured config files"
                    )
                specifiers.add(specifier)
            index += 1
            continue

        # CommonJS destructuring from the proven Node module builtin.
        if value in {"const", "let", "var"} and token_value(index + 1) == "{":
            close = matching_close(index + 1, "{", "}")
            destructuring = assignment_initializer(close) if close is not None else None
            if close is not None and destructuring is not None:
                initializer, annotation = destructuring
                type_annotation_indices.update(annotation)
                namespace_end = node_module_value_end(initializer)
                if namespace_end is not None:
                    cursor = index + 2
                    while cursor < close:
                        if tokens[cursor][0] != "identifier":
                            cursor += 1
                            continue
                        imported = tokens[cursor][1]
                        local = imported
                        local_index = cursor
                        if token_value(cursor + 1) == ":" and tokens[cursor + 2][0] == "identifier":
                            local = tokens[cursor + 2][1]
                            cursor += 2
                            local_index = cursor
                        if imported == "createRequire":
                            set_capability(local, "factory")
                            factory_binding_indices.add(local_index)
                        cursor += 1
                    index += 1
                    continue
                typed_node_module = annotation_names_node_module(annotation)
                binds_create_require = any(
                    tokens[cursor] == ("identifier", "createRequire")
                    for cursor in range(index + 2, close)
                )
                if typed_node_module and binds_create_require:
                    raise JauntConfigError(
                        "Vitest configuration uses typed createRequire destructuring from an "
                        "unproven Node module value"
                    )
                if (
                    tokens[initializer][0] == "identifier"
                    and (
                        tokens[initializer][1] in loaders
                        or tokens[initializer][1] in invalid_loaders
                    )
                    and token_value(initializer + 1) not in {"(", ".", "?.", "["}
                ):
                    raise JauntConfigError(
                        "Vitest configuration destructures a tracked module loader; use direct "
                        "literal calls so package provenance remains auditable"
                    )

        # Track exact identifier assignments and invalidate proven capabilities
        # when they are overwritten by an unknown value.
        member_assignment = token_value(index - 1) in {".", "?."}
        assignment = None if member_assignment else assignment_initializer(index)
        if assignment is not None:
            initializer, annotation = assignment
            type_annotation_indices.update(annotation)
            assignment_rhs_owners[initializer] = value
            capability = rhs_capability(initializer)
            if capability is not None:
                capability_kind, _end = capability
                set_capability(value, capability_kind)
            else:
                invalidate_capability(value)

        if value in invalid_loaders:
            if loader_call_open(index) is not None or token_value(index + 1) in {".", "?.", "["}:
                raise JauntConfigError(
                    f"Vitest configuration reassigns or ambiguously uses loader alias {value!r}"
                )
            index += 1
            continue

        if value in loaders:
            reject_exported_capability(index)
            previous = token_value(index - 1)
            module_member = (
                index >= 2
                and previous in {".", "?."}
                and tokens[index - 2] == ("identifier", "module")
            )
            if previous in {".", "?."} and not module_member:
                index += 1
                continue
            grouped_reference = (
                previous == "("
                and token_value(index + 1) == ")"
                and (
                    index < 2
                    or tokens[index - 2][0] != "identifier"
                    or token_value(index - 2) in {"return", "throw", "yield"}
                )
            )
            if grouped_reference:
                raise JauntConfigError(
                    f"Vitest configuration parenthesizes loader alias {value!r}; "
                    "use a direct literal call"
                )
            open_index = loader_call_open(index)
            resolve_open = loader_resolve_open(index)
            if resolve_open is not None:
                specifiers.add(literal_call_argument(resolve_open))
            elif open_index is not None:
                specifiers.add(literal_call_argument(open_index))
            elif token_value(index + 1) in {".", "?.", "["}:
                raise JauntConfigError(
                    f"Vitest configuration uses unsupported member access on loader alias {value!r}"
                )
            elif value in protected_loaders:
                assignment_lhs = assignment is not None
                propagated_rhs = assignment_rhs_owners.get(index) in loaders
                if not assignment_lhs and not propagated_rhs:
                    raise JauntConfigError(
                        f"Vitest configuration passes or ambiguously uses loader alias {value!r}; "
                        "use a direct literal call"
                    )

        factory_end = factory_reference_end(index)
        if factory_end is not None:
            reject_exported_capability(index)
            if token_value(factory_end) == "(":
                factory_close = matching_close(factory_end)
                if factory_close is None:
                    raise JauntConfigError(
                        "Vitest configuration has an unterminated createRequire call"
                    )
                returned_loader = factory_close + 1
                if token_value(returned_loader) == "(":
                    specifiers.add(literal_call_argument(returned_loader))
                elif (
                    token_value(returned_loader) in {".", "?."}
                    and tokens[returned_loader + 1 : returned_loader + 2]
                    == (("identifier", "resolve"),)
                    and token_value(returned_loader + 2) == "("
                ):
                    specifiers.add(literal_call_argument(returned_loader + 2))
                else:
                    assigned_directly = assignment_rhs_owners.get(index) in loaders
                    if capability_has_unsafe_suffix(returned_loader) and assigned_directly:
                        raise JauntConfigError(
                            "Vitest configuration ambiguously composes a createRequire result"
                        )
                    if not assigned_directly:
                        raise JauntConfigError(
                            "Vitest configuration passes or conditionally stores a createRequire "
                            "result; assign it directly before loading static literals"
                        )
            elif index not in factory_binding_indices:
                assignment_lhs = assignment is not None
                propagated_rhs = assignment_rhs_owners.get(index) in create_require_factories
                if not assignment_lhs and not propagated_rhs:
                    raise JauntConfigError(
                        "Vitest configuration passes or ambiguously uses a createRequire "
                        "factory; call or alias it directly"
                    )
        elif value in module_namespaces:
            reject_exported_capability(index)
            assignment_lhs = assignment is not None
            propagated_rhs = assignment_rhs_owners.get(index) in module_namespaces
            property_key = token_value(index + 1) == ":" and token_value(index - 1) in {"{", ","}
            member_access = token_value(index + 1) in {".", "?."}
            if not (
                index in namespace_binding_indices
                or assignment_lhs
                or propagated_rhs
                or property_key
                or member_access
            ):
                raise JauntConfigError(
                    f"Vitest configuration passes or ambiguously uses Node module namespace "
                    f"{value!r}; use createRequire directly"
                )
        index += 1
    return tuple(sorted(specifiers))


def _config_package_imports(source: str) -> tuple[tuple[str, str], ...]:
    """Return bare specifiers and package owners a captured config can execute."""

    imports: set[tuple[str, str]] = set()
    for specifier in _config_module_specifiers(source):
        if specifier.startswith((".", "/", "#", "data:", "file:", "http:", "https:", "node:")):
            continue
        if specifier.startswith("@"):
            parts = specifier.split("/")
            package = "/".join(parts[:2]) if len(parts) >= 2 else specifier
        else:
            package = specifier.split("/", 1)[0]
        if package and package not in _NODE_BUILTIN_PACKAGES:
            imports.add((specifier, package))
    return tuple(sorted(imports))


def _config_package_import_alias(
    root: Path,
    importer: Path,
    specifier: str,
) -> tuple[str, str, tuple[str, ...], tuple[tuple[str, str], ...]]:
    """Resolve one package ``imports`` alias without executing configuration code.

    The return value is the owning manifest path and digest, confined local
    targets relative to the workspace, and external ``(specifier, owner)``
    targets. Every condition and array branch is retained so custom Node
    conditions cannot select bytes outside the captured closure.
    """

    physical_root = root.resolve()
    try:
        current = importer.resolve(strict=True).parent
    except OSError as exc:
        raise JauntConfigError(
            f"Vitest configuration import scope could not be resolved for {importer}"
        ) from exc
    scope: Path | None = None
    while current == physical_root or physical_root in current.parents:
        if (current / "package.json").is_file():
            scope = current
            break
        if current == physical_root:
            break
        current = current.parent
    if scope is None:
        raise JauntConfigError(
            f"Vitest configuration package import {specifier!r} has no owning package.json"
        )
    package_scope = scope
    manifest_path = package_scope / "package.json"
    try:
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JauntConfigError(
            f"Vitest configuration package import {specifier!r} has an unreadable package.json"
        ) from exc
    if not isinstance(manifest, Mapping) or not isinstance(manifest.get("imports"), Mapping):
        raise JauntConfigError(
            f"Vitest configuration package import {specifier!r} has no imports mapping"
        )
    imports = cast(Mapping[str, object], manifest["imports"])
    own_name = manifest.get("name") if isinstance(manifest.get("name"), str) else None
    local_targets: set[str] = set()
    external_targets: set[tuple[str, str]] = set()

    def fail(reason: str) -> NoReturn:
        raise JauntConfigError(
            f"Invalid Vitest configuration package import {specifier!r} in "
            f"{manifest_path.relative_to(physical_root)}: {reason}"
        )

    def add_local_target(resolved: str) -> None:
        if not resolved.startswith("./"):
            fail(f"package-local target must start with './': {resolved!r}")
        if _unsafe_runtime_package_import_fragment(resolved[2:]):
            fail(f"unsafe package-relative target {resolved!r}")
        target_path = Path(os.path.abspath(package_scope / resolved))
        if target_path != package_scope and package_scope not in target_path.parents:
            fail(f"package-relative target escapes its package: {resolved!r}")
        try:
            local_targets.add(target_path.relative_to(physical_root).as_posix())
        except ValueError:
            fail(f"package-relative target escapes the workspace: {resolved!r}")

    def resolve_self_target(resolved: str) -> None:
        """Resolve a package self-reference to its exact confined exports branches."""

        assert own_name is not None
        subpath = resolved[len(own_name) :]
        export_key = "." if not subpath else f".{subpath}"
        exports = manifest.get("exports")
        if exports is None:
            fail(f"self target {resolved!r} has no exports mapping")
        wildcard: str | None = None
        target: object = exports
        if isinstance(exports, Mapping) and any(str(key).startswith(".") for key in exports):
            if export_key in exports:
                target = exports[export_key]
            else:
                patterns = [
                    str(key)
                    for key in exports
                    if isinstance(key, str)
                    and key.count("*") == 1
                    and export_key.startswith(key.partition("*")[0])
                    and export_key.endswith(key.partition("*")[2])
                    and len(export_key) >= len(key) - 1
                ]
                patterns.sort(
                    key=lambda key: (
                        -(key.find("*") + 1),
                        -len(key),
                        key,
                    )
                )
                if not patterns:
                    fail(f"unresolved self target {resolved!r}")
                pattern = patterns[0]
                suffix_length = len(pattern) - pattern.find("*") - 1
                wildcard_end = len(export_key) - suffix_length if suffix_length else len(export_key)
                wildcard = export_key[pattern.find("*") : wildcard_end]
                if _unsafe_runtime_package_import_fragment(wildcard):
                    fail(f"unsafe self-target wildcard {wildcard!r}")
                target = exports[pattern]
        elif export_key != ".":
            fail(f"unresolved self target {resolved!r}")

        def visit_export(value: object) -> None:
            if value is None:
                return
            if isinstance(value, str):
                export_target = value
                if wildcard is not None:
                    export_target = value.replace("*", wildcard)
                elif "*" in value:
                    fail(f"self target {value!r} uses a wildcard without a pattern key")
                add_local_target(export_target)
                return
            if isinstance(value, list):
                for entry in value:
                    visit_export(entry)
                return
            if isinstance(value, Mapping):
                for condition, entry in value.items():
                    if not isinstance(condition, str) or (
                        condition.isdigit()
                        and str(int(condition)) == condition
                        and int(condition) < 2**32 - 1
                    ):
                        fail(f"invalid self-export condition key {condition!r}")
                    visit_export(entry)
                return
            fail(f"non-string self target {value!r}")

        visit_export(target)

    def resolve_alias(alias: str, seen: frozenset[str]) -> None:
        if alias in seen:
            fail(f"cyclic alias through {alias!r}")
        if alias == "#" or alias.endswith("/"):
            fail(f"invalid alias {alias!r}")
        match = _runtime_package_import_match(imports, alias)
        if match is None:
            fail(f"unresolved alias {alias!r}")
        assert match is not None
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
                    add_local_target(resolved)
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
                if package == own_name:
                    resolve_self_target(resolved)
                    return
                external_targets.add((resolved, package))
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
    try:
        if manifest_bytes != manifest_path.read_bytes():
            raise JauntGenerationError(
                "TypeScript Vitest package imports changed while their closure was captured: "
                + manifest_path.relative_to(physical_root).as_posix()
            )
    except OSError as exc:
        raise JauntConfigError(
            f"Vitest configuration package import manifest changed or became unreadable: "
            f"{manifest_path}"
        ) from exc
    return (
        manifest_path.relative_to(physical_root).as_posix(),
        _sha256(manifest_bytes),
        tuple(sorted(local_targets)),
        tuple(sorted(external_targets)),
    )


def _config_package_dependencies(
    root: Path,
    relative: str,
    source: str,
) -> tuple[tuple[str, str, Path], ...]:
    """Return direct and package-import external dependencies with resolver origins."""

    importer = _safe_path(root, relative)
    dependencies = {
        (specifier, package, importer) for specifier, package in _config_package_imports(source)
    }
    for specifier in _config_module_specifiers(source):
        if not specifier.startswith("#"):
            continue
        manifest_relative, _digest, _locals, externals = _config_package_import_alias(
            root, importer, specifier
        )
        manifest_path = _safe_path(root, manifest_relative)
        dependencies.update(
            (external_specifier, package, manifest_path)
            for external_specifier, package in externals
        )
    return tuple(sorted(dependencies, key=lambda item: (item[1], item[0], str(item[2]))))


def _config_package_runtime_identities(
    root: Path,
    config_overlays: Mapping[str, str],
) -> Mapping[str, str]:
    """Fingerprint directly imported config packages for cross-command freshness."""

    identities: dict[str, str] = {}
    for relative, source in sorted(config_overlays.items()):
        for specifier, package, resolution_start in _config_package_dependencies(
            root, relative, source
        ):
            try:
                resolved = resolve_node_package(
                    resolution_start,
                    package,
                    boundary=root,
                    module_path=True,
                )
            except TypeScriptWorkerError as exc:
                raise JauntConfigError(
                    f"Vitest configuration package {package!r} imported by {relative} could "
                    "not be resolved safely"
                ) from exc
            if resolved is None:
                if _root_local_config_import(root, specifier):
                    continue
                raise JauntConfigError(
                    f"Vitest configuration package {package!r} imported by {relative} is not "
                    "installed within the workspace"
                )
            identities[f"{relative}:{package}"] = runtime_package_identity(resolved)
            try:
                closure = _runtime_package_resolution_closure(
                    resolved,
                    root_label=package,
                )
            except TypeScriptWorkerError as exc:
                raise JauntConfigError(
                    f"Runtime dependency closure of Vitest config package {package!r} "
                    f"imported by {relative} could not be resolved safely"
                ) from exc
            for edge in closure:
                key = f"{relative}:{edge.label}"
                identities[key] = (
                    MISSING_INPUT
                    if edge.resolved_root is None
                    else runtime_package_identity(edge.resolved_root)
                )
    return identities


def _pin_vitest_config_dependency_runtimes(
    client: object,
    root: Path,
    config_overlays: Mapping[str, str],
) -> None:
    """Hold every directly imported config package through the command seal."""

    pin_closure = getattr(client, "pin_package_resolution_closure", None)
    pin_resolution = getattr(client, "pin_package_resolution_identity", None)
    pin_runtime = getattr(client, "pin_package_runtime_identity", None)
    if not callable(pin_closure) and not callable(pin_resolution) and not callable(pin_runtime):
        return
    for relative, source in sorted(config_overlays.items()):
        pinned_packages: set[tuple[str, Path]] = set()
        for specifier, package, resolution_start in _config_package_dependencies(
            root, relative, source
        ):
            pin_key = (package, resolution_start)
            if pin_key in pinned_packages:
                continue
            resolved = resolve_node_package(
                resolution_start,
                package,
                boundary=root,
                module_path=True,
            )
            if resolved is None:
                if _root_local_config_import(root, specifier):
                    continue
                raise JauntConfigError(
                    f"Vitest configuration package {package!r} imported by {relative} is not "
                    "installed within the workspace"
                )
            pinned_packages.add(pin_key)
            label = f"Vitest config dependency {package} from {relative}"
            if callable(pin_closure):
                pin_closure(
                    label,
                    resolution_start,
                    package,
                    boundary=root,
                    module_path=True,
                )
                continue
            if callable(pin_resolution):
                pin_resolution(
                    label,
                    resolution_start,
                    package,
                    boundary=root,
                    module_path=True,
                )
                continue
            assert callable(pin_runtime)
            pin_runtime(label, resolved)


def _looks_like_local_config_path(value: str) -> bool:
    return (
        not value.startswith(("#", "data:", "http:", "https:", "node:"))
        and not any(character in value for character in "*?[")
        and (
            value.startswith(("./", "../"))
            or "/" in value
            or "\\" in value
            or value.casefold() in _LOCAL_CONFIG_EXTENSIONS
            or Path(value).suffix.casefold() in _LOCAL_CONFIG_EXTENSIONS
        )
    )


def _is_vitest_path_field_value(source: str, literal_start: int) -> bool:
    """Return whether a literal sits in a supported Vitest executable-path field."""

    prefix = source[:literal_start]
    return (
        re.search(
            r"\b(?:setupFiles|globalSetup)\s*:\s*(?:\[[^\]]*)?$",
            prefix,
            re.DOTALL,
        )
        is not None
    )


def _vitest_non_path_metadata_spans(source: str) -> tuple[tuple[int, int], ...]:
    """Locate direct ``name`` and ``define``/``env`` metadata values.

    These values may contain path-shaped strings or pure ``node:path`` calls,
    but Vitest never executes them as setup modules.  We identify complete
    property-value expressions so array-valued metadata is covered without
    weakening handling of sibling executable-path fields.
    """

    comments, strings = _typescript_lexical_regions(source)
    masked_characters = list(source)
    for start, end in (*comments, *((start, end) for start, end, _quote in strings)):
        for index in range(start, end):
            if masked_characters[index] not in {"\r", "\n"}:
                masked_characters[index] = " "
    masked = "".join(masked_characters)

    opening_for = {")": "(", "]": "[", "}": "{"}
    stack: list[tuple[str, int]] = []
    brace_pairs: dict[int, int] = {}
    for index, character in enumerate(masked):
        if character in "([{":
            stack.append((character, index))
            continue
        expected = opening_for.get(character)
        if expected is None or not stack or stack[-1][0] != expected:
            continue
        opening, opening_index = stack.pop()
        if opening == "{":
            brace_pairs[opening_index] = index

    def object_properties(open_index: int, close_index: int) -> tuple[tuple[str, int, int], ...]:
        properties: list[tuple[str, int, int]] = []
        cursor = open_index + 1
        while cursor < close_index:
            while cursor < close_index and (masked[cursor].isspace() or masked[cursor] in ",;"):
                cursor += 1
            property_start = cursor
            nested: list[str] = []
            colon: int | None = None
            while cursor < close_index:
                character = masked[cursor]
                if character in "([{":
                    nested.append(character)
                elif character in opening_for:
                    if nested and nested[-1] == opening_for[character]:
                        nested.pop()
                elif not nested and character == ":":
                    colon = cursor
                    break
                elif not nested and character in ",;":
                    cursor += 1
                    break
                cursor += 1
            if colon is None:
                continue
            key = masked[property_start:colon].strip()
            value_start = colon + 1
            while value_start < close_index and source[value_start].isspace():
                value_start += 1
            cursor = value_start
            nested = []
            while cursor < close_index:
                character = masked[cursor]
                if character in "([{":
                    nested.append(character)
                elif character in opening_for:
                    if nested and nested[-1] == opening_for[character]:
                        nested.pop()
                elif not nested and character in ",;":
                    break
                cursor += 1
            if re.fullmatch(r"[A-Za-z_$][\w$]*", key):
                value_end = cursor
                properties.append((key, value_start, value_end))
            if cursor < close_index:
                cursor += 1
        return tuple(properties)

    spans: set[tuple[int, int]] = set()
    for open_index, close_index in brace_pairs.items():
        for key, value_start, value_end in object_properties(open_index, close_index):
            if key == "name":
                spans.add((value_start, value_end))
                continue
            if key not in {"define", "env"} or masked[value_start : value_start + 1] != "{":
                continue
            metadata_close = brace_pairs.get(value_start)
            if metadata_close is None or metadata_close > value_end:
                continue
            for _metadata_key, metadata_start, metadata_end in object_properties(
                value_start, metadata_close
            ):
                spans.add((metadata_start, metadata_end))
    return tuple(sorted(spans))


_SAFE_METADATA_INTERPOLATION = re.compile(
    r"\s*[A-Za-z_$][\w$]*(?:(?:\?\.|\.)[A-Za-z_$][\w$]*)*\s*\Z"
)


def _metadata_template_interpolations_are_side_effect_free(value: str) -> bool:
    """Accept metadata interpolation only when every expression is a property read."""

    expressions = tuple(re.finditer(r"\$\{([^{}]*)\}", value, re.DOTALL))
    if not expressions:
        return False
    remainder = re.sub(r"\$\{[^{}]*\}", "", value, flags=re.DOTALL)
    return "${" not in remainder and all(
        _SAFE_METADATA_INTERPOLATION.fullmatch(match.group(1)) is not None for match in expressions
    )


def _static_config_path_calls(
    source: str,
    *,
    config_directory: str = "",
    metadata_spans: Sequence[tuple[int, int]] = (),
) -> tuple[tuple[str, ...], tuple[tuple[int, int], ...]]:
    """Reduce imported ``node:path`` join/resolve calls or reject dynamic ones."""

    direct: dict[str, str] = {}
    namespaces: set[str] = set()
    path_module = r"[\"'](?:node:)?path[\"']"
    comments, strings = _typescript_lexical_regions(source)
    noncode = (*comments, *((start, end) for start, end, _quote in strings))

    def add_named_bindings(bindings: str, *, alias_separator: str) -> None:
        for raw_binding in bindings.split(","):
            parts = re.split(alias_separator, raw_binding.strip())
            imported = parts[0].strip()
            local = parts[-1].strip()
            if imported in {"join", "resolve"} and re.fullmatch(r"[A-Za-z_$][\w$]*", local):
                direct[local] = imported
            elif imported in {"posix", "win32"} and re.fullmatch(r"[A-Za-z_$][\w$]*", local):
                namespaces.add(local)

    for match in re.finditer(
        rf"\bimport\s+(?:(?P<default>[A-Za-z_$][\w$]*)\s*,\s*)?"
        rf"\{{(?P<bindings>[^}}]+)\}}\s*from\s*{path_module}",
        source,
        re.DOTALL,
    ):
        if _inside_region(match.start(), noncode):
            continue
        add_named_bindings(match.group("bindings"), alias_separator=r"\s+as\s+")
        if match.group("default"):
            namespaces.add(match.group("default"))
    for match in re.finditer(
        rf"\bimport\s+\*\s+as\s+(?P<name>[A-Za-z_$][\w$]*)\s+from\s*{path_module}",
        source,
    ):
        if not _inside_region(match.start(), noncode):
            namespaces.add(match.group("name"))
    for match in re.finditer(
        rf"\bimport\s+(?P<name>[A-Za-z_$][\w$]*)\s+from\s*{path_module}",
        source,
    ):
        if not _inside_region(match.start(), noncode):
            namespaces.add(match.group("name"))

    for match in re.finditer(
        rf"\b(?:const|let|var)\s+(?P<binding>\{{[^}}]+\}}|[A-Za-z_$][\w$]*)\s*=\s*"
        rf"require\(\s*{path_module}\s*\)",
        source,
    ):
        if _inside_region(match.start(), noncode):
            continue
        binding = match.group("binding")
        if binding.startswith("{"):
            add_named_bindings(binding[1:-1], alias_separator=r"\s*:\s*")
        else:
            namespaces.add(binding)
    for match in re.finditer(
        rf"\b(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*"
        rf"require\(\s*{path_module}\s*\)\s*\.\s*"
        rf"(?P<member>join|resolve|posix|win32)\b",
        source,
    ):
        if not _inside_region(match.start(), noncode):
            member = match.group("member")
            if member in {"join", "resolve"}:
                direct[match.group("name")] = member
            else:
                namespaces.add(match.group("name"))
    for match in re.finditer(
        rf"\bimport\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*require\(\s*{path_module}\s*\)",
        source,
    ):
        if not _inside_region(match.start(), noncode):
            namespaces.add(match.group("name"))

    callees: dict[str, str] = dict(direct)
    for namespace in namespaces:
        callees[f"{namespace}.join"] = "join"
        callees[f"{namespace}.resolve"] = "resolve"
        callees[f"{namespace}.posix.join"] = "join"
        callees[f"{namespace}.posix.resolve"] = "resolve"
        callees[f"{namespace}.win32.join"] = "join"
        callees[f"{namespace}.win32.resolve"] = "resolve"
    if not callees:
        return (), ()

    callee_names: list[str] = list(callees)
    call_pattern = re.compile(
        r"(?<![\w$.])(?P<callee>"
        + "|".join(
            re.escape(callee)
            for callee in sorted(callee_names, key=lambda value: len(value), reverse=True)
        )
        + r")\s*\("
    )
    values: list[str] = []
    spans: list[tuple[int, int]] = []
    for match in call_pattern.finditer(source):
        if _inside_region(match.start(), noncode) or _inside_region(match.start(), metadata_spans):
            continue
        open_index = source.find("(", match.start("callee"))
        index = open_index + 1
        depth = 1
        quote: str | None = None
        escaped = False
        while index < len(source) and depth:
            character = source[index]
            if quote is not None:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = None
            elif character in {"'", '"', "`"}:
                quote = character
            elif character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
            index += 1
        if depth:
            raise JauntConfigError("Vitest configuration has an unterminated path helper call")
        arguments = source[open_index + 1 : index - 1]
        raw_parts: list[str] = []
        part_start = 0
        nested_depth = 0
        argument_quote: str | None = None
        argument_escaped = False
        for argument_index, character in enumerate(arguments):
            if argument_quote is not None:
                if argument_escaped:
                    argument_escaped = False
                elif character == "\\":
                    argument_escaped = True
                elif character == argument_quote:
                    argument_quote = None
                continue
            if character in {"'", '"', "`"}:
                argument_quote = character
            elif character in "([{":
                nested_depth += 1
            elif character in ")]}":
                nested_depth -= 1
            elif character == "," and nested_depth == 0:
                raw_parts.append(arguments[part_start:argument_index].strip())
                part_start = argument_index + 1
        raw_parts.append(arguments[part_start:].strip())
        if not raw_parts or any(not part for part in raw_parts):
            callee = match.group("callee")
            raise JauntConfigError(
                "Vitest configuration uses a computed path that cannot be captured safely: "
                + callee
            )
        parts: list[str] = []
        for raw_part in raw_parts:
            if raw_part in {"__dirname", "import.meta.dirname"}:
                parts.append(config_directory)
                continue
            if raw_part == "process.cwd()":
                parts.append("")
                continue
            if raw_part.startswith("`") and raw_part.endswith("`") and "${" not in raw_part:
                parts.append(raw_part[1:-1])
                continue
            try:
                part = ast.literal_eval(raw_part)
            except (SyntaxError, ValueError) as exc:
                callee = match.group("callee")
                raise JauntConfigError(
                    "Vitest configuration uses a computed path that cannot be captured safely: "
                    + callee
                ) from exc
            if not isinstance(part, str):
                raise JauntConfigError("Vitest configuration path arguments must be strings")
            parts.append(part)
        combined = posixpath.normpath(posixpath.join(*[part.replace("\\", "/") for part in parts]))
        if callees[match.group("callee")] == "resolve" and combined.startswith("/"):
            raise JauntConfigError("Vitest configuration resolves a path outside the workspace")
        values.append(combined)
        spans.append((open_index + 1, index - 1))
    return tuple(values), tuple(spans)


def _local_config_snapshot(
    root: Path,
    initial: str,
    *,
    client: object | None = None,
) -> tuple[Mapping[str, str], Mapping[str, str]]:
    """Capture a confined Vitest config closure as exact hashes and overlays."""

    root = root.resolve()
    pending = [_safe_path(root, initial)]
    seen: set[Path] = set()
    hashes: dict[str, str] = {}
    overlays: dict[str, str] = {}
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        relative = path.relative_to(root).as_posix()
        try:
            physical = path.resolve(strict=True)
            physical.relative_to(root)
            if not physical.is_file():
                raise FileNotFoundError(path)
            source_bytes = physical.read_bytes()
        except (FileNotFoundError, NotADirectoryError):
            previous_digest = hashes.setdefault(relative, MISSING_INPUT)
            if previous_digest != MISSING_INPUT:
                raise JauntGenerationError(
                    "TypeScript Vitest configuration changed while its closure was captured: "
                    + relative
                ) from None
            continue
        except (OSError, ValueError) as exc:
            raise JauntConfigError(
                "Vitest configuration dependency escapes or cannot be read: " + relative
            ) from exc
        source = source_bytes.decode("utf-8")
        digest = _sha256(source_bytes)
        previous_digest = hashes.setdefault(relative, digest)
        if previous_digest != digest:
            raise JauntGenerationError(
                "TypeScript Vitest configuration changed while its closure was captured: "
                + relative
            )
        overlays[relative] = source
        config_directory = path.parent.relative_to(root).as_posix()
        metadata_spans = _vitest_non_path_metadata_spans(source)
        computed_values, computed_spans = _static_config_path_calls(
            source,
            config_directory="" if config_directory == "." else config_directory,
            metadata_spans=metadata_spans,
        )
        executable_specifiers = _config_module_specifiers(source)
        alias_local_values: list[str] = []
        for specifier in executable_specifiers:
            if not specifier.startswith("#"):
                continue
            manifest_relative, manifest_digest, local_targets, _externals = (
                _config_package_import_alias(root, path, specifier)
            )
            previous_manifest_digest = hashes.setdefault(manifest_relative, manifest_digest)
            if previous_manifest_digest != manifest_digest:
                raise JauntGenerationError(
                    "TypeScript Vitest package imports changed while their closure was "
                    f"captured: {manifest_relative}"
                )
            alias_local_values.extend(local_targets)
        comment_regions, string_regions = _typescript_lexical_regions(source)
        quoted_values: list[str] = []
        for start, end, quote in string_regions:
            if _inside_region(start, comment_regions) or any(
                span_start <= start and end <= span_end for span_start, span_end in computed_spans
            ):
                continue
            literal = source[start:end]
            if quote == "`":
                value = literal[1:-1]
                if "${" in value:
                    if (
                        not _is_vitest_path_field_value(source, start)
                        and _inside_region(start, metadata_spans)
                        and _metadata_template_interpolations_are_side_effect_free(value)
                    ):
                        continue
                    raise JauntConfigError(
                        "Vitest configuration uses an interpolated path or unresolved computed "
                        "value that cannot be captured safely"
                    )
                if _inside_region(start, metadata_spans):
                    continue
                quoted_values.append(value)
                continue
            try:
                value = ast.literal_eval(literal)
            except (SyntaxError, ValueError):
                value = literal[1:-1]
            if isinstance(value, str):
                if _inside_region(start, metadata_spans):
                    continue
                left = source[:start].rstrip()
                right = source[end:].lstrip()
                if _looks_like_local_config_path(value) and (
                    left.endswith("+") or right.startswith("+")
                ):
                    raise JauntConfigError(
                        "Vitest configuration uses a concatenated path that cannot be captured "
                        "safely"
                    )
                quoted_values.append(value)
        specifiers = [
            (value, computed)
            for values, computed in (
                (quoted_values, False),
                (executable_specifiers, False),
                (computed_values, True),
                (alias_local_values, True),
            )
            for value in values
            if _looks_like_local_config_path(value) or _root_local_config_import(root, value)
        ]
        for specifier, computed in specifiers:
            normalized = specifier.replace("\\", "/")
            bases = (
                (path.parent / normalized,)
                if not computed and normalized.startswith(("./", "../"))
                else (root / normalized,)
            )
            candidates: list[Path] = []
            for base in bases:
                lexical_base = Path(os.path.abspath(base))
                try:
                    lexical_base.relative_to(root)
                except ValueError as exc:
                    raise JauntConfigError(
                        "Vitest configuration references a path outside the workspace: " + specifier
                    ) from exc
                candidates.append(base)
                if base.suffix.casefold() not in _LOCAL_CONFIG_EXTENSIONS:
                    candidates.extend(
                        Path(f"{base}{extension}") for extension in _LOCAL_CONFIG_EXTENSIONS
                    )
                candidates.extend(
                    base / f"index{extension}" for extension in _LOCAL_CONFIG_EXTENSIONS
                )
            for candidate in dict.fromkeys(candidates):
                lexical_candidate = Path(os.path.abspath(candidate))
                try:
                    relative_lexical = lexical_candidate.relative_to(root).as_posix()
                except ValueError as exc:
                    raise JauntConfigError(
                        "Vitest configuration dependency escapes the workspace: " + specifier
                    ) from exc
                try:
                    physical = candidate.resolve(strict=True)
                except FileNotFoundError:
                    try:
                        unresolved = candidate.resolve(strict=False)
                        unresolved.relative_to(root)
                    except (OSError, ValueError) as exc:
                        raise JauntConfigError(
                            "Vitest configuration dependency escapes the workspace: " + specifier
                        ) from exc
                    previous_digest = hashes.setdefault(relative_lexical, MISSING_INPUT)
                    if previous_digest != MISSING_INPUT:
                        raise JauntGenerationError(
                            "TypeScript Vitest configuration changed while its closure was "
                            f"captured: {relative_lexical}"
                        ) from None
                    continue
                except NotADirectoryError:
                    # An already captured regular-file ancestor makes this index
                    # candidate impossible without changing that ancestor first.
                    continue
                except OSError as exc:
                    raise JauntConfigError(
                        "Vitest configuration dependency could not be resolved: " + specifier
                    ) from exc
                try:
                    relative_candidate = physical.relative_to(root)
                except ValueError as exc:
                    raise JauntConfigError(
                        "Vitest configuration dependency escapes the workspace: " + specifier
                    ) from exc
                confined = _safe_path(root, relative_candidate.as_posix())
                if confined.is_file():
                    pending.append(confined)
    captured = dict(sorted(hashes.items())), dict(sorted(overlays.items()))
    if client is not None:
        _pin_vitest_config_dependency_runtimes(client, root, captured[1])
    return captured


def _local_config_closure(root: Path, initial: str) -> Mapping[str, str]:
    """Hash a Vitest config and statically referenced local setup/config files."""

    return _local_config_snapshot(root, initial)[0]


def _verify_local_config_closure(
    root: Path,
    initial: str,
    expected: Mapping[str, str],
) -> None:
    """Hold Vitest config resolution and exact bytes through publication."""

    if _local_config_closure(root, initial) != expected:
        raise JauntGenerationError(
            "TypeScript Vitest configuration changed during battery validation; "
            "no test artifacts were committed."
        )


def _fixture_fingerprint(request: GenerationRequest) -> str:
    """Hash the exact canonical fixture bytes supplied to one battery request."""

    fixture_path = request.cache_payload.get("fixturePath")
    fixture_source = request.context_files.get("_context/fixtures.ts")
    fixture_digest = request.cache_payload.get("fixtureDigest")
    if (
        isinstance(fixture_path, str)
        and fixture_path
        and isinstance(fixture_source, str)
        and isinstance(fixture_digest, str)
    ):
        exact_digest = _sha256(fixture_source.encode("utf-8"))
        if fixture_digest != exact_digest:
            raise JauntConfigError(
                f"TypeScript fixture bytes changed while preparing {fixture_path}"
            )
        return _canonical_digest({"path": fixture_path, "digest": fixture_digest})
    if fixture_path or fixture_source is not None or fixture_digest:
        raise JauntConfigError("Incomplete TypeScript fixture provenance in battery request")
    return _canonical_digest(None)


def _runner_export_target(value: object) -> str | None:
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


def _stable_runner_digest(path: Path, *, package_root: Path) -> str:
    """Hash one runner input while rejecting replacement and package escape."""

    try:
        physical_root = package_root.resolve(strict=True)
        physical_path = path.resolve(strict=True)
        if physical_path != physical_root and physical_root not in physical_path.parents:
            raise TypeScriptWorkerError(
                f"@usejaunt/ts test-runner runtime file escapes its package: {path}"
            )
        before = physical_path.stat()
        content = physical_path.read_bytes()
        after = physical_path.stat()
    except TypeScriptWorkerError:
        raise
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not read @usejaunt/ts test-runner runtime file at {path}: {exc}"
        ) from exc
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
            f"@usejaunt/ts test-runner runtime changed while its freshness identity was read: "
            f"{path}"
        )
    return _sha256(content)


def _runner_runtime_snapshot(
    package_root: Path,
    *,
    package_managed: bool,
) -> tuple[str | None, str | None, dict[str, str]]:
    """Return a portable identity snapshot for the runnable test boundary."""

    manifest_path = package_root / "package.json"
    try:
        manifest_source = manifest_path.read_bytes()
        manifest = json.loads(manifest_source)
    except FileNotFoundError:
        manifest = None
        manifest_source = None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        if package_managed:
            raise TypeScriptWorkerError(
                f"Could not read @usejaunt/ts package.json at {manifest_path}: {exc}"
            ) from exc
        manifest = None
        manifest_source = None

    package_version: str | None = None
    runner_export: str | None = None
    if isinstance(manifest, Mapping) and manifest.get("name") == "@usejaunt/ts":
        version = manifest.get("version")
        if isinstance(version, str) and version.strip():
            package_version = version
        exports = manifest.get("exports")
        if isinstance(exports, Mapping):
            runner_export = _runner_export_target(exports.get("./test-runner"))

    if not package_managed and package_version is None:
        # Protocol-only fakes and arbitrary worker overrides do not establish a
        # package runtime boundary. The real runner lookup still fails closed if
        # an operation tries to execute without an installed runner.
        return None, None, {}

    if package_managed:
        if package_version is None:
            raise TypeScriptWorkerError(
                f"Invalid @usejaunt/ts package manifest for test runner: {manifest_path}"
            )
        if runner_export != f"./{_RUNNER_ENTRY}":
            raise TypeScriptWorkerError(
                "Installed @usejaunt/ts has an inconsistent './test-runner' export: "
                f"expected './{_RUNNER_ENTRY}', got {runner_export!r}"
            )

    try:
        physical_root = package_root.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve @usejaunt/ts test-runner package at {package_root}: {exc}"
        ) from exc

    def runtime_paths() -> tuple[Path, ...]:
        dist = physical_root / "dist"
        if not dist.is_dir():
            if package_managed:
                raise TypeScriptWorkerError(
                    f"Installed @usejaunt/ts has no runtime directory at {dist}"
                )
            return ()
        paths = {
            path
            for path in _runtime_package_identity_files(physical_root)
            if path.relative_to(physical_root).parts[:1] == ("dist",)
        }
        if any(path != physical_root and physical_root not in path.parents for path in paths):
            raise TypeScriptWorkerError("@usejaunt/ts test-runner runtime file escapes its package")
        missing = [
            relative
            for relative in _RUNNER_REQUIRED_FILES
            if not (physical_root / relative).is_file()
        ]
        if package_managed and missing:
            raise TypeScriptWorkerError(
                "Installed @usejaunt/ts is missing required test-runner runtime file(s): "
                + ", ".join(missing)
            )
        return tuple(sorted(paths, key=lambda path: path.relative_to(physical_root).as_posix()))

    required = runtime_paths()

    def digest_files() -> dict[str, str]:
        files = {
            path.relative_to(physical_root).as_posix(): _stable_runner_digest(
                path, package_root=physical_root
            )
            for path in required
        }
        if isinstance(manifest, Mapping):
            files["package.json"] = _canonical_digest(_ordered_json_identity(manifest))
        return files

    files = digest_files()
    # A second full read catches a runner/support replacement between individual
    # reads, while remaining path-independent for source checkouts and installs.
    if required != runtime_paths() or files != digest_files():
        raise TypeScriptWorkerError(
            "@usejaunt/ts test-runner runtime changed while its freshness identity was read"
        )
    if package_managed:
        try:
            current_manifest = manifest_path.read_bytes()
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not re-read @usejaunt/ts package.json at {manifest_path}: {exc}"
            ) from exc
        if current_manifest != manifest_source:
            raise TypeScriptWorkerError(
                "@usejaunt/ts package.json changed while its test-runner identity was read"
            )
    return package_version, runner_export, files


def _runner_fingerprint(root: Path, client: object, initialized: object) -> str:
    installation = getattr(client, "installation", None)
    package_root = getattr(installation, "package_root", None)
    files: dict[str, str] = {}
    package_version: str | None = None
    runner_export: str | None = None
    if isinstance(package_root, Path):
        package_version, runner_export, files = _runner_runtime_snapshot(
            package_root,
            package_managed=bool(getattr(installation, "package_managed", False)),
        )
    compiler_identity = (
        compiler_runtime_identity(installation)
        if isinstance(installation, WorkerInstallation)
        else None
    )
    roots = _tool_search_roots(root, client)
    return _canonical_digest(
        {
            "protocol": _RUNNER_PROTOCOL,
            "packageVersion": package_version or _read_package_version(roots, "@usejaunt/ts"),
            "testRunnerExport": runner_export,
            "workerVersion": str(getattr(initialized, "worker_version", "unknown")),
            "typescriptVersion": str(getattr(initialized, "typescript_version", "unknown")),
            "typescriptRuntimeIdentity": compiler_identity,
            "files": files,
            "settings": {
                "customReporters": False,
                "run": True,
                "watch": False,
                "explicitInclude": True,
                "capturedOutput": True,
            },
        }
    )


def _verify_runner_runtime_identity(
    root: Path,
    client: object,
    initialized: object,
    expected: str,
) -> None:
    """Reject a runner/held-out runtime replacement after validation began."""

    try:
        current = _runner_fingerprint(root, client, initialized)
    except TypeScriptWorkerError as exc:
        raise WorkerToolchainChangedError(
            "The project-local @usejaunt/ts test runtime became unreadable while "
            "battery validation was active. Rerun after the toolchain is stable; "
            "Jaunt will not commit the candidate batteries."
        ) from exc
    if current != expected:
        raise WorkerToolchainChangedError(
            "The project-local @usejaunt/ts test runtime changed while battery "
            "validation was active. Rerun after the toolchain is stable; Jaunt will "
            "not commit the candidate batteries."
        )


def _pin_test_runtime_identity(client: object) -> None:
    """Extend a real worker session pin across runner and declaration inputs."""

    pin = getattr(client, "pin_full_runtime_identity", None)
    if callable(pin):
        pin()


def _verify_test_runtime_identity(
    root: Path,
    client: WorkerLike,
    initialized: object,
    expected_runner: str,
) -> None:
    _verify_runner_runtime_identity(root, client, initialized, expected_runner)
    _verify_worker_runtime_identity(client)


def _seal_test_runtime_identity(
    root: Path,
    client: WorkerLike,
    initialized: object,
    expected_runner: str,
) -> None:
    # Seal the worker first, then re-read the complete test runtime while the
    # artifact transaction still has its rollback bytes.
    _seal_worker_runtime_identity(client)
    _verify_runner_runtime_identity(root, client, initialized, expected_runner)


def _verify_test_commit_environment(
    root: Path,
    client: WorkerLike,
    initialized: object,
    expected_runner: str,
    *,
    vitest_config: str,
    config_closure: Mapping[str, str],
) -> None:
    if vitest_config:
        _verify_local_config_closure(root, vitest_config, config_closure)
    _verify_test_runtime_identity(root, client, initialized, expected_runner)


def _seal_test_commit_environment(
    root: Path,
    client: WorkerLike,
    initialized: object,
    expected_runner: str,
    *,
    vitest_config: str,
    config_closure: Mapping[str, str],
) -> None:
    _seal_test_runtime_identity(root, client, initialized, expected_runner)
    if vitest_config:
        _verify_local_config_closure(root, vitest_config, config_closure)


def _test_provenance(
    root: Path,
    config: JauntConfig,
    test_spec: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    client: object,
    initialized: object,
    *,
    tier: str,
    builtin_skill_names: Sequence[str] | None = None,
    prepared_request: GenerationRequest | None = None,
    runner_fingerprint: str | None = None,
    workspace: Mapping[str, Any] | None = None,
) -> Mapping[str, str]:
    path = str(test_spec.get("path", ""))
    source = (
        str(test_spec["syntheticSource"])
        if isinstance(test_spec.get("syntheticSource"), str)
        else _safe_path(root, path).read_text(encoding="utf-8")
    )
    selected = _selected_test_modules(test_spec, modules)
    target = _target(config)
    roots = _tool_search_roots(root, client)
    vitest_runtime_identity = _read_package_version(roots, "vitest")
    runner_vitest_runtime_identity = "unresolved"
    runner_vitest_package = _runner_test_dependency(client, "vitest")
    if runner_vitest_package is not None:
        runner_vitest_runtime_identity = runtime_package_identity(
            runner_vitest_package,
            expected_name="vitest",
        )
    if workspace is not None:
        project = test_spec.get("project")
        if not isinstance(project, str):
            project = _owner_project_for_source(root, config, workspace, path)
        owner = _test_package_owner(root, workspace, project)
        vitest_runtime_identity = _test_dependency_runtime_identity(root, owner, "vitest")
    if target.vitest_config:
        config_closure, config_overlays = _local_config_snapshot(root, target.vitest_config)
        config_digest: object = {
            "files": config_closure,
            "packages": _config_package_runtime_identities(root, config_overlays),
        }
    else:
        config_digest = "default"
    # Invocation-only ``--instruction`` values guide a paid repair attempt but
    # are not a committed behavioral input. Canonical config instructions still
    # flow through the default request and therefore remain provenance-bearing.
    request = _test_request(
        root,
        config,
        test_spec,
        modules,
        tier=tier,
        builtin_skill_names=builtin_skill_names,
    )
    fixture_request = prepared_request or request
    imported_type_context = {
        path: source
        for path, source in request.context_files.items()
        if path.startswith("_context/imported-types/")
    }
    values = {
        "test_spec_digest": _semantic_test_spec_digest(source),
        "target_api_digest": target_api_digest(selected),
        **(
            {"imported_type_context_fingerprint": _canonical_digest(imported_type_context)}
            if imported_type_context
            else {}
        ),
        # Fixture source is behavioral generation context. Keep it separate
        # from the reheader-safe Vitest toolchain fingerprint so a fixture
        # change can never stamp an unchanged battery fresh.
        "fixture_fingerprint": _fixture_fingerprint(fixture_request),
        "vitest_fingerprint": _canonical_digest(
            {
                "runner": target.test_runner,
                "configPath": target.vitest_config,
                "configDigest": config_digest,
                "ownerRuntimeIdentity": vitest_runtime_identity,
                "runnerRuntimeIdentity": runner_vitest_runtime_identity,
            }
        ),
        "fast_check_fingerprint": _canonical_digest(
            {
                "rendererScheme": PROPERTY_RENDERER_SCHEME,
                "runs": target.fast_check_runs,
                "seed": request.cache_payload.get("propertySeed"),
                "version": _read_package_version(roots, "fast-check"),
                "renderedBlockDigest": _sha256(
                    str(request.cache_payload.get("propertyBlock", "")).encode("utf-8")
                ),
                **(
                    {"cases": request.cache_payload["propertyCases"]}
                    if request.cache_payload.get("propertyCases")
                    else {}
                ),
            }
        ),
        "runner_fingerprint": runner_fingerprint or _runner_fingerprint(root, client, initialized),
        "prompt_fingerprint": _sha256(request.prompt.encode("utf-8")),
        "policy_fingerprint": _sha256(_TEST_IMPORT_POLICY.encode("utf-8")),
        "skills_fingerprint": skills_fingerprint(
            project_root=root,
            builtin_names=(
                tuple(builtin_skill_names)
                if builtin_skill_names is not None
                else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
            ),
        ),
    }
    return {
        **values,
        "battery_fingerprint": _canonical_digest({"tier": tier, **values}),
    }


def _strip_test_header(source: str) -> str:
    body = source.lstrip("\ufeff")
    parsed = parse_managed_document(body, _GENERATED_TEST_HEADER)
    return canonical_managed_body(body if parsed is None else parsed.body)


def _test_header_metadata(source: str) -> Mapping[str, str] | None:
    parsed = parse_managed_document(source, _GENERATED_TEST_HEADER, allow_bom=True)
    if parsed is None or parsed.malformed:
        return None
    return parsed.fields


def _with_test_header(
    source: str,
    *,
    tier: str,
    source_path: str,
    provenance: Mapping[str, str] | None = None,
) -> str:
    """Add deterministic ownership metadata used by the protected reporter."""

    body = canonical_managed_body(_strip_test_header(source))
    metadata = dict(provenance or {})
    metadata["body_digest"] = _sha256(body.encode("utf-8"))
    fields = (
        ("tier", tier),
        ("source", source_path),
        *((key, metadata[key]) for key in _TEST_PROVENANCE_FIELDS if key in metadata),
    )
    return render_managed_document(_GENERATED_TEST_HEADER, fields, body)


def _rejected_test_paths(
    target_path: str,
    *,
    candidate_digest: str | None = None,
) -> tuple[Path, Path]:
    identity = hashlib.sha256(target_path.encode("utf-8")).hexdigest()[:16]
    stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(target_path).stem).strip("-._")
    stem = stem[:_REJECTED_TEST_STEM_MAX_CHARS].rstrip("-._") or "battery"
    suffix = Path(target_path).suffix if Path(target_path).suffix in {".ts", ".tsx"} else ".ts"
    base = _REJECTED_TEST_DIR / f"{stem}-{identity}"
    content_identity = ""
    if candidate_digest:
        raw_digest = candidate_digest.removeprefix("sha256:")
        bounded_digest = (
            raw_digest.lower()
            if re.fullmatch(r"[0-9a-fA-F]{64}", raw_digest)
            else hashlib.sha256(candidate_digest.encode("utf-8")).hexdigest()
        )
        content_identity = f".{bounded_digest}"
    return (
        base.parent / f"{base.name}{content_identity}.candidate{suffix}",
        base.parent / f"{base.name}.json",
    )


@contextmanager
def _rejected_test_lock(root: Path, target_path: str) -> Iterator[None]:
    """Serialize publication and CAS cleanup of one rejected-battery record."""

    metadata_relative = _rejected_test_paths(target_path)[1]
    lock = _safe_path(root, metadata_relative.with_suffix(".lock").as_posix())
    lock.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    locked = False
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(descriptor).st_size == 0:
                os.write(descriptor, b"\0")
            os.lseek(descriptor, 0, os.SEEK_SET)
            locking = getattr(msvcrt, "locking", None)
            lock_mode = getattr(msvcrt, "LK_LOCK", None)
            if not callable(locking) or not isinstance(lock_mode, int):
                raise RuntimeError("This Python runtime cannot lock rejected-test records")
            locking(descriptor, lock_mode, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX)
        locked = True
        yield
    finally:
        try:
            if locked:
                if os.name == "nt":
                    import msvcrt

                    os.lseek(descriptor, 0, os.SEEK_SET)
                    locking = getattr(msvcrt, "locking", None)
                    unlock_mode = getattr(msvcrt, "LK_UNLCK", None)
                    if not callable(locking) or not isinstance(unlock_mode, int):
                        raise RuntimeError(
                            "This Python runtime cannot unlock rejected-test records"
                        )
                    locking(descriptor, unlock_mode, 1)
                else:
                    import fcntl

                    fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        try:
            with os.fdopen(
                descriptor,
                "w",
                encoding="utf-8",
                newline="\n",
                closefd=False,
            ) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _rejected_test_semantic_identity(provenance: Mapping[str, Any] | None) -> str | None:
    """Identify the authored test contract independently of prompt/tool provenance."""

    if not isinstance(provenance, Mapping):
        return None
    semantic: dict[str, str] = {}
    for field in _REJECTED_TEST_SEMANTIC_FIELDS:
        value = provenance.get(field)
        if not isinstance(value, str) or not value:
            return None
        semantic[field] = value
    for field in _REJECTED_TEST_OPTIONAL_SEMANTIC_FIELDS:
        if field not in provenance:
            continue
        value = provenance.get(field)
        if not isinstance(value, str) or not value:
            return None
        semantic[field] = value
    return _canonical_digest(semantic)


def _rejected_test_payload_identity(payload: Mapping[str, Any]) -> str | None:
    """Read a validated semantic identity, falling back for legacy exact markers."""

    if "expected_provenance" in payload or "semantic_identity" in payload:
        semantic_identity = _rejected_test_semantic_identity(payload.get("expected_provenance"))
        recorded_identity = payload.get("semantic_identity")
        return (
            semantic_identity
            if isinstance(recorded_identity, str) and recorded_identity == semantic_identity
            else None
        )
    fingerprint = payload.get("battery_fingerprint")
    return fingerprint if isinstance(fingerprint, str) else None


def _expected_rejected_test_identity(
    expected_fingerprint: str,
    expected_provenance: Mapping[str, Any] | None,
) -> str:
    """Use semantic identity when available and exact fingerprint for legacy callers."""

    return _rejected_test_semantic_identity(expected_provenance) or expected_fingerprint


def _write_rejected_test_candidate(
    root: Path,
    request: GenerationRequest,
    *,
    source_path: str,
    tier: str,
    fingerprint: str,
    candidate_source: str,
    attempts: int,
    errors: Sequence[str],
    attempt_errors: Sequence[Sequence[str]] = (),
    terminal: bool = True,
    expected_provenance: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    """Persist the exact rejected validator input and bounded local diagnostics."""

    if not candidate_source.strip():
        return None
    candidate_content = candidate_source
    candidate_digest = _sha256(candidate_content.encode("utf-8"))
    candidate_relative, metadata_relative = _rejected_test_paths(
        request.target_path,
        candidate_digest=candidate_digest,
    )
    semantic_identity = _rejected_test_semantic_identity(expected_provenance)
    marker_identity = semantic_identity or fingerprint
    stored_provenance = (
        {
            str(key): value
            for key, value in expected_provenance.items()
            if isinstance(key, str) and isinstance(value, str)
        }
        if expected_provenance is not None
        else None
    )
    metadata = _safe_path(root, metadata_relative.as_posix())
    with _rejected_test_lock(root, request.target_path):
        previous_attempts = 0
        try:
            previous = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            previous = None
        if (
            isinstance(previous, Mapping)
            and _rejected_test_payload_identity(previous) == marker_identity
            and isinstance(previous.get("consecutive_attempts"), int)
        ):
            previous_attempts = max(0, int(previous["consecutive_attempts"]))
        candidate = _safe_path(root, candidate_relative.as_posix())
        _atomic_write_text(candidate, candidate_content)
        _atomic_write_text(
            metadata,
            json.dumps(
                {
                    "schema": 2 if semantic_identity is not None else 1,
                    "target": request.target_path,
                    "source": source_path,
                    "tier": tier,
                    "battery_fingerprint": fingerprint,
                    **(
                        {
                            "semantic_identity": semantic_identity,
                            "expected_provenance": stored_provenance,
                        }
                        if semantic_identity is not None
                        else {}
                    ),
                    "attempts_this_run": attempts,
                    "consecutive_attempts": previous_attempts + max(0, attempts),
                    "terminal": terminal,
                    "errors": list(errors),
                    "attempt_errors": [list(items) for items in attempt_errors],
                    "candidate": candidate_relative.as_posix(),
                    "candidate_digest": candidate_digest,
                },
                sort_keys=True,
                indent=2,
            )
            + "\n",
        )
        if isinstance(previous, Mapping):
            previous_candidate = previous.get("candidate")
            previous_digest = previous.get("candidate_digest")
            if isinstance(previous_candidate, str) and isinstance(previous_digest, str):
                expected_previous = _rejected_test_paths(
                    request.target_path,
                    candidate_digest=previous_digest,
                )[0].as_posix()
                if previous_candidate == expected_previous and previous_candidate != (
                    candidate_relative.as_posix()
                ):
                    with contextlib.suppress(OSError):
                        _safe_path(root, previous_candidate).unlink()
    return candidate_relative.as_posix(), metadata_relative.as_posix()


def _rejected_test_diagnostic(
    root: Path,
    target_path: str,
    *,
    expected_fingerprint: str,
    expected_provenance: Mapping[str, str] | None = None,
) -> Mapping[str, Any] | None:
    """Return a current terminal-generation marker without treating it as freshness proof."""

    metadata_relative = _rejected_test_paths(target_path)[1]
    metadata = _safe_path(root, metadata_relative.as_posix())
    with _rejected_test_lock(root, target_path):
        try:
            payload = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping):
            return None
        recorded_digest = payload.get("candidate_digest")
        candidate_value = payload.get("candidate")
        if not isinstance(recorded_digest, str) or not isinstance(candidate_value, str):
            return None
        candidate_relative = _rejected_test_paths(
            target_path,
            candidate_digest=recorded_digest,
        )[0]
        if candidate_value != candidate_relative.as_posix():
            return None
        candidate = _safe_path(root, candidate_relative.as_posix())
        try:
            candidate_digest = _sha256(candidate.read_bytes())
        except OSError:
            return None
        if (
            payload.get("target") != target_path
            or _rejected_test_payload_identity(payload)
            != _expected_rejected_test_identity(expected_fingerprint, expected_provenance)
            or payload.get("terminal") is not True
            or recorded_digest != candidate_digest
        ):
            return None
        return {**payload, "metadata": metadata_relative.as_posix()}


def _rejected_test_token(
    root: Path,
    target_path: str,
    *,
    expected_fingerprint: str,
    expected_provenance: Mapping[str, str] | None = None,
) -> tuple[str, str, str] | None:
    """Snapshot the exact marker a successful run is authorized to clear."""

    metadata_relative = _rejected_test_paths(target_path)[1]
    metadata = _safe_path(root, metadata_relative.as_posix())
    expected_identity = _expected_rejected_test_identity(
        expected_fingerprint,
        expected_provenance,
    )
    with _rejected_test_lock(root, target_path):
        try:
            metadata_bytes = metadata.read_bytes()
            payload = json.loads(metadata_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        if (
            not isinstance(payload, Mapping)
            or payload.get("target") != target_path
            or _rejected_test_payload_identity(payload) != expected_identity
            or not isinstance(payload.get("candidate_digest"), str)
        ):
            return None
        candidate_digest = str(payload["candidate_digest"])
        candidate_relative = _rejected_test_paths(
            target_path,
            candidate_digest=candidate_digest,
        )[0]
        if payload.get("candidate") != candidate_relative.as_posix():
            return None
        try:
            actual_candidate_digest = _sha256(
                _safe_path(root, candidate_relative.as_posix()).read_bytes()
            )
        except OSError:
            return None
        if actual_candidate_digest != candidate_digest:
            return None
        return _sha256(metadata_bytes), candidate_digest, expected_identity


def _clear_rejected_test_candidate(
    root: Path,
    target_path: str,
    *,
    expected_token: tuple[str, str, str] | None,
) -> bool:
    """Delete only the exact rejected marker observed by this successful run."""

    if expected_token is None:
        return False
    expected_metadata_digest, expected_candidate_digest, expected_identity = expected_token
    metadata_relative = _rejected_test_paths(target_path)[1]
    metadata = _safe_path(root, metadata_relative.as_posix())
    with _rejected_test_lock(root, target_path):
        try:
            metadata_bytes = metadata.read_bytes()
            payload = json.loads(metadata_bytes)
        except (OSError, UnicodeError, json.JSONDecodeError):
            return False
        candidate_relative = _rejected_test_paths(
            target_path,
            candidate_digest=expected_candidate_digest,
        )[0]
        if (
            not isinstance(payload, Mapping)
            or _sha256(metadata_bytes) != expected_metadata_digest
            or payload.get("target") != target_path
            or _rejected_test_payload_identity(payload) != expected_identity
            or payload.get("candidate_digest") != expected_candidate_digest
            or payload.get("candidate") != candidate_relative.as_posix()
        ):
            return False
        candidate = _safe_path(root, candidate_relative.as_posix())
        try:
            if _sha256(candidate.read_bytes()) != expected_candidate_digest:
                return False
        except OSError:
            return False
        metadata.unlink()
        with contextlib.suppress(OSError):
            candidate.unlink()
        return True


def _is_verifiable_api_transition(
    mismatches: set[str],
    *,
    additional_allowed: set[str] | frozenset[str] = frozenset(),
) -> bool:
    """Recognize drift that the safety-first committed-body check can prove."""

    required = {"target_api_digest", "battery_fingerprint"}
    # The prompt embeds target contract/API context, so a real API transition
    # can legitimately change it. Runner/Vitest drift is reheader-safe, but the
    # active runner still safety-typechecks the old body before it can execute.
    allowed = (
        required
        | set(_TEST_REHEADER_FINGERPRINTS)
        | {"prompt_fingerprint"}
        | set(additional_allowed)
    )
    return required.issubset(mismatches) and mismatches.issubset(allowed)


def _test_provenance_mismatches(
    metadata: Mapping[str, str],
    provenance: Mapping[str, str],
) -> set[str]:
    """Compare current provenance without losing removed optional inputs."""

    mismatches = {key for key, value in provenance.items() if metadata.get(key) != value}
    if (
        "imported_type_context_fingerprint" in metadata
        and "imported_type_context_fingerprint" not in provenance
    ):
        mismatches.add("imported_type_context_fingerprint")
    return mismatches


def _read_current_target_artifact_snapshot(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
) -> tuple[frozenset[str], Mapping[str, str]]:
    """Read one lease-protected target snapshot and validate its built proof."""

    from jaunt.typescript.upgrade import compatible_semantic_modules

    compatible = compatible_semantic_modules(
        root,
        tuple(modules),
        allow_environment_drift=True,
    )
    current: set[str] = set()
    observed: dict[str, str] = {}
    for module in modules:
        module_id = module.get("moduleId")
        routes = module.get("routes")
        artifact_paths: dict[str, str] = {}
        for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath"):
            value = module.get(key)
            if not isinstance(value, str) and isinstance(routes, Mapping):
                value = routes.get(key)
            if isinstance(value, str) and value:
                artifact_paths[key] = value

        sidecar_path = artifact_paths.get("sidecarPath")
        contents: dict[str, bytes | None] = {}
        for relative in artifact_paths.values():
            try:
                content = _safe_path(root, relative).read_bytes()
            except FileNotFoundError:
                content = None
            contents[relative] = content
            observed[relative] = _sha256(content) if content is not None else MISSING_INPUT
        sidecar_content = contents.get(sidecar_path) if sidecar_path is not None else None
        try:
            actual_sidecar = (
                json.loads(sidecar_content.decode("utf-8")) if sidecar_content is not None else None
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            actual_sidecar = None
        artifact_hashes = (
            actual_sidecar.get("artifactHashes") if isinstance(actual_sidecar, Mapping) else None
        )
        artifacts_match = isinstance(artifact_hashes, Mapping)
        for key in ("facadePath", "apiMirrorPath", "implementationPath"):
            relative = artifact_paths.get(key)
            recorded = (
                artifact_hashes.get(relative) if isinstance(artifact_hashes, Mapping) else None
            )
            if not isinstance(relative, str) or not isinstance(recorded, str):
                artifacts_match = False
                break
            normalized = recorded if recorded.startswith("sha256:") else f"sha256:{recorded}"
            content = contents.get(relative)
            if content is None or _sha256(content) != normalized:
                artifacts_match = False
                break
        expected_api = expected_target_api_record(module)
        api_path = artifact_paths.get("apiMirrorPath")
        api_source = contents.get(api_path) if api_path is not None else None
        live_api = (
            {
                "moduleId": actual_sidecar.get("moduleId"),
                "apiDigest": actual_sidecar.get("apiDigest"),
                "apiSourceDigest": _sha256(api_source),
            }
            if isinstance(actual_sidecar, Mapping) and api_source is not None
            else None
        )
        if (
            isinstance(module_id, str)
            and module_id in compatible
            and isinstance(actual_sidecar, Mapping)
            and actual_sidecar.get("state") == "built"
            and actual_sidecar.get("moduleId") == module_id
            and artifacts_match
            and expected_api == live_api
        ):
            current.add(module_id)
    return frozenset(current), dict(sorted(observed.items()))


def _current_target_artifact_snapshot(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
    *,
    strict: bool,
) -> tuple[frozenset[str], Mapping[str, str]]:
    """Snapshot target proof and bytes under the global artifact lease."""

    root = root.resolve()
    transaction_directory = root / ".jaunt" / "transactions"
    try:
        workspace = _PinnedWorkspace(root)
        with workspace:
            pinned_directory = workspace.directory(transaction_directory)
            lease = _acquire_transaction_lease(
                transaction_directory,
                blocking=True,
                pinned_directory=pinned_directory,
                authority_directory=workspace.root_directory,
            )
            if lease is None:  # pragma: no cover - blocking acquisition
                raise JauntGenerationError("Could not acquire the TypeScript transaction lease")
            try:
                pending = pinned_directory.iter_names("*.json")
                if pending:
                    if strict:
                        raise JauntGenerationError(
                            "An unresolved TypeScript artifact transaction blocks test "
                            "verification: " + ", ".join(pending)
                        )
                    return frozenset(), {}
                snapshot = _read_current_target_artifact_snapshot(root, modules)
                workspace.verify_namespace()
                return snapshot
            finally:
                lease.release()
    except (JauntConfigError, JauntGenerationError, OSError):
        if strict:
            raise
        return frozenset(), {}


def _current_target_artifact_ids(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
) -> frozenset[str]:
    """Identify targets whose committed implementation and API match analysis."""

    current, _preconditions = _current_target_artifact_snapshot(root, modules, strict=False)
    return current


def _existing_test_battery_action(
    root: Path,
    request: GenerationRequest,
    *,
    tier: str,
    source_path: str,
    provenance: Mapping[str, str],
    force: bool,
    generated_dirs: Sequence[str] = (),
    proven_previous_api_digests: frozenset[str] = frozenset(),
    allow_verified_api_transition: bool = True,
) -> tuple[str, str | None]:
    """Classify an existing managed battery without trusting its header alone.

    A runner or Vitest change cannot alter the authored test body, so those two
    fingerprints may be deterministically reheadered. Every content-bearing
    input, malformed ownership field, or body mismatch goes back through the
    generator. The aggregate battery fingerprint must drift alongside an
    allowed tooling fingerprint; an isolated aggregate mismatch is not a valid
    restamp case.
    """

    if force:
        return "generate", None
    path = _safe_path(root, request.target_path)
    if not path.is_file():
        return "generate", None
    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return "generate", None
    metadata = _test_header_metadata(source)
    body = _strip_test_header(source)
    if (
        metadata is None
        or metadata.get("tier") != tier
        or metadata.get("source") != source_path
        or metadata.get("body_digest") != _sha256(body.encode("utf-8"))
        or _static_test_validation(body, generated_dirs=generated_dirs)
    ):
        return "generate", None

    mismatches = _test_provenance_mismatches(metadata, provenance)
    if not mismatches:
        return "skip", source

    allowed_tooling = set(_TEST_REHEADER_FINGERPRINTS)
    # ``fixture_fingerprint`` was added after committed batteries already
    # existed.  A missing legacy field is migration-safe only when the current
    # request has no fixture at all; there are then no fixture bytes whose
    # behavioral intent could have changed.  Keep every real fixture mismatch
    # content-bearing so adding or editing fixtures still regenerates.
    if "fixture_fingerprint" not in metadata and provenance.get(
        "fixture_fingerprint"
    ) == _canonical_digest(None):
        allowed_tooling.add("fixture_fingerprint")
    # Older batteries predate declaration-only imported-context provenance.
    # Missing evidence is not safe to restamp directly, but it can take the
    # stronger verification path: the unchanged body must pass the current
    # static policy, compile against the current declarations, and then pass
    # protected Vitest execution.  A present fingerprint that changed is real
    # content drift and remains generation-only.
    legacy_imported_context = (
        "imported_type_context_fingerprint" in provenance
        and "imported_type_context_fingerprint" not in metadata
    )
    legacy_verification_allowed = {
        "battery_fingerprint",
        "imported_type_context_fingerprint",
        "prompt_fingerprint",
        "runner_fingerprint",
        "target_api_digest",
        "vitest_fingerprint",
    } | ({"fixture_fingerprint"} if "fixture_fingerprint" in allowed_tooling else set())
    if (
        allow_verified_api_transition
        and legacy_imported_context
        and {"battery_fingerprint", "imported_type_context_fingerprint"}.issubset(mismatches)
        and mismatches.issubset(legacy_verification_allowed)
    ):
        return (
            "verify",
            _with_test_header(
                body,
                tier=tier,
                source_path=source_path,
                provenance=provenance,
            ),
        )
    # A persisted implementation/API transition proves that the target bytes
    # are current, not that an older battery still asserts the right behavior.
    # Reuse it only when this command will execute the resulting battery;
    # ``--no-run`` must regenerate instead of publishing an unexecuted reheader.
    api_proof_matches = (
        allow_verified_api_transition
        and metadata.get("target_api_digest") in proven_previous_api_digests
    )
    if api_proof_matches:
        allowed_tooling.add("target_api_digest")
    allowed = allowed_tooling | {"battery_fingerprint"}
    if (
        not mismatches.intersection(allowed_tooling)
        or "battery_fingerprint" not in mismatches
        or not mismatches.issubset(allowed)
    ):
        if allow_verified_api_transition and _is_verifiable_api_transition(
            mismatches,
            additional_allowed=allowed_tooling,
        ):
            return (
                "verify",
                _with_test_header(
                    body,
                    tier=tier,
                    source_path=source_path,
                    provenance=provenance,
                ),
            )
        return "generate", None
    return (
        "refreeze",
        _with_test_header(
            body,
            tier=tier,
            source_path=source_path,
            provenance=provenance,
        ),
    )


def _skip_typescript_trivia(source: str, start: int) -> int:
    """Skip whitespace and comments without treating their text as executable syntax."""

    cursor = start
    while cursor < len(source):
        if source[cursor].isspace():
            cursor += 1
            continue
        if source.startswith("//", cursor):
            newline = source.find("\n", cursor + 2)
            cursor = len(source) if newline < 0 else newline + 1
            continue
        if source.startswith("/*", cursor):
            end = source.find("*/", cursor + 2)
            cursor = len(source) if end < 0 else end + 2
            continue
        break
    return cursor


def _read_typescript_string_literal(source: str, start: int) -> tuple[str, int] | None:
    """Read one JS/TS quoted literal and decode escapes relevant to a module path."""

    if start >= len(source) or source[start] not in {'"', "'", "`"}:
        return None
    quote = source[start]
    cursor = start + 1
    value: list[str] = []
    simple_escapes = {
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "0": "\0",
    }
    while cursor < len(source):
        character = source[cursor]
        if character == quote:
            return "".join(value), cursor + 1
        if quote == "`" and source.startswith("${", cursor):
            return None
        if character in "\r\n" and quote != "`":
            return None
        if character != "\\":
            value.append(character)
            cursor += 1
            continue
        cursor += 1
        if cursor >= len(source):
            return None
        escaped = source[cursor]
        if escaped == "\r" or escaped == "\n":
            if escaped == "\r" and cursor + 1 < len(source) and source[cursor + 1] == "\n":
                cursor += 1
            cursor += 1
            continue
        if escaped == "x" and re.fullmatch(r"[0-9A-Fa-f]{2}", source[cursor + 1 : cursor + 3]):
            value.append(chr(int(source[cursor + 1 : cursor + 3], 16)))
            cursor += 3
            continue
        if escaped == "u":
            braced = re.match(r"\{([0-9A-Fa-f]{1,6})\}", source[cursor + 1 :])
            if braced is not None:
                codepoint = int(braced.group(1), 16)
                if codepoint <= 0x10FFFF:
                    value.append(chr(codepoint))
                    cursor += len(braced.group(0)) + 1
                    continue
            digits = source[cursor + 1 : cursor + 5]
            if re.fullmatch(r"[0-9A-Fa-f]{4}", digits):
                value.append(chr(int(digits, 16)))
                cursor += 5
                continue
        value.append(simple_escapes.get(escaped, escaped))
        cursor += 1
    return None


def _typescript_template_expressions(source: str, start: int) -> tuple[int, tuple[str, ...]]:
    """Return executable ``${...}`` bodies while ignoring inert template text."""

    def expression_end(expression_start: int) -> tuple[int, tuple[str, ...]]:
        cursor = expression_start
        depth = 1
        nested_expressions: list[str] = []
        expression_tokens: list[tuple[str, str]] = []
        control_parentheses: list[bool] = []
        statement_braces: list[bool] = []
        previous_closed_control_head = False
        previous_closed_statement_brace = False
        pending_line_break = False

        def record(kind: str, value: str) -> None:
            nonlocal pending_line_break
            nonlocal previous_closed_control_head, previous_closed_statement_brace
            closes_control_head = False
            closes_statement_brace = False
            if kind == "punctuation" and value == "(":
                control_parentheses.append(_opens_control_flow_parenthesis(expression_tokens))
            elif kind == "punctuation" and value == ")":
                closes_control_head = control_parentheses.pop() if control_parentheses else False
            elif kind == "punctuation" and value == "{":
                statement_braces.append(
                    _opens_statement_brace(
                        expression_tokens,
                        enclosing_statement_brace=(
                            statement_braces[-1] if statement_braces else None
                        ),
                        previous_closed_control_head=previous_closed_control_head,
                        previous_closed_statement_brace=previous_closed_statement_brace,
                        line_break_before=pending_line_break,
                    )
                )
            elif kind == "punctuation" and value == "}":
                closes_statement_brace = statement_braces.pop() if statement_braces else False
            previous_closed_control_head = closes_control_head
            previous_closed_statement_brace = closes_statement_brace
            expression_tokens.append((kind, value))
            pending_line_break = False

        def slash_starts_regex() -> bool:
            if not expression_tokens:
                return True
            kind, value = expression_tokens[-1]
            if kind in {"number", "regex", "string", "template"}:
                return False
            if kind == "identifier":
                return _identifier_precedes_regex(expression_tokens)
            if value == ")" and previous_closed_control_head:
                return True
            if value == "}" and previous_closed_statement_brace:
                return True
            return value not in {")", "]", "}", "++", "--"}

        while cursor < len(source) and depth:
            next_cursor = _skip_typescript_trivia(source, cursor)
            if next_cursor != cursor:
                pending_line_break = pending_line_break or any(
                    character in "\r\n" for character in source[cursor:next_cursor]
                )
                cursor = next_cursor
                continue
            character = source[cursor]
            if character in {'"', "'"}:
                parsed = _read_typescript_string_literal(source, cursor)
                cursor = parsed[1] if parsed is not None else cursor + 1
                record("string", "literal")
                continue
            if character == "`":
                cursor, nested = _typescript_template_expressions(source, cursor)
                nested_expressions.extend(nested)
                record("template", "literal")
                continue
            if character == "/" and slash_starts_regex():
                end = _skip_typescript_regex(source, cursor)
                if end > cursor + 1:
                    cursor = end
                    record("regex", "regex")
                    continue
            if character.isalpha() or character in "_$":
                end = cursor + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                record("identifier", source[cursor:end])
                cursor = end
                continue
            if character.isdigit():
                end = cursor + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "._"):
                    end += 1
                record("number", source[cursor:end])
                cursor = end
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    nested_expressions.append(source[expression_start:cursor])
                    return cursor + 1, tuple(nested_expressions)
            pair = source[cursor : cursor + 2]
            punctuation = pair if pair in {"++", "--", "=>", "?."} else character
            record("punctuation", punctuation)
            cursor += len(punctuation)
        return len(source), tuple(nested_expressions)

    expressions: list[str] = []
    cursor = start + 1
    while cursor < len(source):
        if source[cursor] == "\\":
            cursor += 2
            continue
        if source[cursor] == "`":
            return cursor + 1, tuple(expressions)
        if not source.startswith("${", cursor):
            cursor += 1
            continue
        expression_start = cursor + 2
        cursor, nested = expression_end(expression_start)
        expressions.extend(nested)
    return len(source), tuple(expressions)


def _skip_typescript_regex(source: str, start: int) -> int:
    """Skip a regex literal so example text cannot masquerade as a module reference."""

    cursor = start + 1
    in_class = False
    while cursor < len(source):
        character = source[cursor]
        if character == "\\":
            cursor += 2
            continue
        if character == "[":
            in_class = True
        elif character == "]":
            in_class = False
        elif character == "/" and not in_class:
            cursor += 1
            while cursor < len(source) and source[cursor].isalpha():
                cursor += 1
            return cursor
        elif character in "\r\n":
            return start + 1
        cursor += 1
    return start + 1


def _static_typescript_module_references(source: str) -> tuple[str, ...]:
    """Collect quoted ESM, dynamic-import, CommonJS, and import-equals specifiers."""

    references: list[str] = []
    cursor = 0
    previous_significant = ""
    previous_identifier_is_member = False
    while cursor < len(source):
        next_cursor = _skip_typescript_trivia(source, cursor)
        if next_cursor != cursor:
            cursor = next_cursor
            continue
        character = source[cursor]
        if character in {'"', "'"}:
            parsed = _read_typescript_string_literal(source, cursor)
            cursor = parsed[1] if parsed is not None else cursor + 1
            previous_significant = "literal"
            previous_identifier_is_member = False
            continue
        if character == "`":
            cursor, expressions = _typescript_template_expressions(source, cursor)
            for expression in expressions:
                references.extend(_static_typescript_module_references(expression))
            previous_significant = "literal"
            previous_identifier_is_member = False
            continue
        if character == "/" and (
            not previous_significant
            or previous_significant in "=(:,[!&|?{;"
            or (
                previous_significant in _REGEX_PREFIX_KEYWORDS and not previous_identifier_is_member
            )
        ):
            cursor = _skip_typescript_regex(source, cursor)
            previous_significant = "literal"
            previous_identifier_is_member = False
            continue
        if character.isalpha() or character in "_$":
            end = cursor + 1
            while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                end += 1
            identifier = source[cursor:end]
            argument = _skip_typescript_trivia(source, end)
            if identifier == "require":
                while source.startswith(("!", ")"), argument):
                    argument = _skip_typescript_trivia(source, argument + 1)
                if source.startswith("?.", argument):
                    argument = _skip_typescript_trivia(source, argument + 2)
                if source.startswith(".", argument):
                    resolved = _skip_typescript_trivia(source, argument + 1)
                    if source.startswith("resolve", resolved):
                        argument = _skip_typescript_trivia(source, resolved + len("resolve"))
                if source.startswith("(", argument):
                    literal_at = _skip_typescript_trivia(source, argument + 1)
                    parsed = _read_typescript_string_literal(source, literal_at)
                    if parsed is not None:
                        references.append(parsed[0])
            elif identifier == "import":
                if source.startswith("(", argument):
                    literal_at = _skip_typescript_trivia(source, argument + 1)
                    parsed = _read_typescript_string_literal(source, literal_at)
                    if parsed is not None:
                        references.append(parsed[0])
                else:
                    parsed = _read_typescript_string_literal(source, argument)
                    if parsed is not None:
                        references.append(parsed[0])
            elif identifier == "from":
                parsed = _read_typescript_string_literal(source, argument)
                if parsed is not None:
                    references.append(parsed[0])
            previous_identifier_is_member = previous_significant == "."
            previous_significant = identifier
            cursor = end
            continue
        previous_significant = character
        previous_identifier_is_member = False
        cursor += 1
    return tuple(dict.fromkeys(references))


def _is_private_test_module_reference(
    specifier: str,
    *,
    generated_dirs: Sequence[str] = (),
) -> tuple[bool, bool]:
    normalized = unquote(specifier).replace("\\", "/").split("?", 1)[0].split("#", 1)[0]
    parts = tuple(part for part in normalized.split("/") if part)
    private_sequences = {
        tuple(part for part in value.replace("\\", "/").strip("/").split("/") if part)
        for value in ("__generated__", *generated_dirs)
    }
    path_like = specifier.startswith((".", "/", "\\", "file:"))
    generated = path_like and any(
        sequence
        and any(
            parts[index : index + len(sequence)] == sequence
            for index in range(len(parts) - len(sequence) + 1)
        )
        for sequence in private_sequences
    )
    filename = parts[-1] if parts else normalized
    spec = (
        re.search(
            r"\.jaunt(?:-test)?(?:\.(?:[cm]?[jt]s|[jt]sx))?$",
            filename,
        )
        is not None
    )
    return spec, generated


def _static_test_validation(
    source: str,
    *,
    generated_dirs: Sequence[str] = (),
) -> list[str]:
    errors: list[str] = []
    references = _static_typescript_module_references(source)
    classifications = tuple(
        _is_private_test_module_reference(item, generated_dirs=generated_dirs)
        for item in references
    )
    if any(spec for spec, _generated in classifications):
        errors.append("generated tests must not import private Jaunt spec inputs")
    if any(generated for _spec, generated in classifications):
        errors.append("generated tests must import the public facade, not private generated files")
    if "@ts-ignore" in source or "@ts-expect-error" in source or "@ts-nocheck" in source:
        errors.append("generated tests must not suppress TypeScript diagnostics")
    return errors


def _fixture_for_path(root: Path, relative: str) -> tuple[str, str, str] | None:
    """Return the nearest canonical fixture module above one test/battery path."""

    root = root.resolve()
    current = _safe_path(root, relative).parent
    while current == root or current.is_relative_to(root):
        matches = [
            path for name in ("fixtures.ts", "fixtures.tsx") if (path := current / name).is_file()
        ]
        if len(matches) > 1:
            location = current.relative_to(root).as_posix() or "."
            raise JauntConfigError(
                f"TypeScript test owner {location} has both fixtures.ts and fixtures.tsx"
            )
        if matches:
            path = matches[0]
            try:
                source_bytes = path.read_bytes()
                source = source_bytes.decode("utf-8")
            except (OSError, UnicodeError) as exc:
                raise JauntConfigError(f"Could not read TypeScript fixture module: {path}") from exc
            return (
                path.relative_to(root).as_posix(),
                source,
                _sha256(source_bytes),
            )
        if current == root:
            break
        current = current.parent
    return None


def _fixture_resolution_preconditions(root: Path, relative: str) -> Mapping[str, str]:
    """Guard every canonical candidate that determines nearest-fixture selection."""

    root = root.resolve()
    current = _safe_path(root, relative).parent
    expected: dict[str, str] = {}
    while current == root or current.is_relative_to(root):
        for name in ("fixtures.ts", "fixtures.tsx"):
            candidate = current / name
            relative_candidate = candidate.relative_to(root).as_posix()
            expected[relative_candidate] = _path_hash(candidate) or MISSING_INPUT
        if any((current / name).is_file() for name in ("fixtures.ts", "fixtures.tsx")):
            break
        if current == root:
            break
        current = current.parent
    return expected


def _fixture_names(*sources: str) -> tuple[str, ...]:
    names: set[str] = set()
    for source in sources:
        for match in re.finditer(r"@fixtures\s+([^\r\n*]+)", source):
            names.update(re.findall(r"[A-Za-z_$][\w$]*", match.group(1)))
    return tuple(sorted(names))


def _async_export_names(*sources: str) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                match.group(1)
                for source in sources
                for match in re.finditer(
                    r"\bexport\s+(?:default\s+)?async\s+function\s+([A-Za-z_$][\w$]*)\b",
                    source,
                )
            }
        )
    )


def _selected_test_modules(
    test_spec: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    raw_targets = test_spec.get("targets", [])
    targets = {str(item) for item in raw_targets} if isinstance(raw_targets, list) else set()
    by_symbol: dict[str, list[Mapping[str, Any]]] = {}
    for module in modules.values():
        symbols = module.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str):
                by_symbol.setdefault(str(symbol["name"]), []).append(module)
    if not targets and len(modules) == 1:
        return list(modules.values())
    selected: list[Mapping[str, Any]] = []
    for target in sorted(targets):
        if target.startswith("ts:"):
            module_id, _, symbol_name = target.partition("#")
            module = modules.get(module_id)
            matches = [module] if module is not None else []
            if matches and symbol_name and module not in by_symbol.get(symbol_name, []):
                matches = []
        else:
            matches = by_symbol.get(target, [])
        if len(matches) != 1:
            qualifier = "ambiguous" if matches else "unknown"
            raise JauntConfigError(
                f"TypeScript test target {target!r} is {qualifier}; "
                "test targets must resolve to one exported magic symbol"
            )
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def _imported_type_context_files(
    modules: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Extract marked worker declarations under one UTF-8 request budget."""

    records_by_identity: dict[tuple[str, str], tuple[int, int, str, str]] = {}
    order = 0
    for module in modules:
        context_source = module.get("contextSource")
        if not isinstance(context_source, str):
            continue
        _authored, block = _split_context_source(context_source)
        if block is None:
            continue
        records: list[tuple[object, object, object]] = []
        for match in _IMPORTED_TYPE_SOURCE_V2_RE.finditer(block.strip()):
            try:
                decoded = base64.b64decode(match.group("payload"), validate=True).decode("utf-8")
                payload = json.loads(decoded)
            except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(payload, Mapping):
                records.append((payload.get("id"), payload.get("priority"), payload.get("source")))
        # Version 1 remains readable for committed sidecars and older workers.
        for match in _IMPORTED_TYPE_SOURCE_RE.finditer(block.strip()):
            try:
                metadata = json.loads(match.group("meta"))
            except json.JSONDecodeError:
                continue
            if not isinstance(metadata, Mapping):
                continue
            records.append((metadata.get("id"), metadata.get("priority"), match.group("source")))
        for source_id, priority, raw_source in records:
            source = raw_source.strip() if isinstance(raw_source, str) else ""
            if (
                not isinstance(source_id, str)
                or priority not in {"requested", "supporting"}
                or not source
            ):
                continue
            identity = (source_id, source)
            rank = 0 if priority == "requested" else 1
            previous = records_by_identity.get(identity)
            if previous is None:
                records_by_identity[identity] = (rank, order, source_id, source)
                order += 1
            elif rank < previous[0]:
                records_by_identity[identity] = (rank, previous[1], source_id, source)

    rendered: list[tuple[str, str]] = []
    for _priority, _order, source_id, source in sorted(records_by_identity.values()):
        original = source_id.removeprefix("workspace:")
        suffix = Path(original).suffix if Path(original).suffix in {".ts", ".tsx"} else ".ts"
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "-", Path(original).stem).strip("-._")
        stem = stem or "types"
        rendered.append(
            (
                f"{stem}{suffix}",
                f"// Resolved type source: {source_id}\n{source.rstrip()}\n",
            )
        )
    total = sum(len(source.encode("utf-8")) for _name, source in rendered)
    omission_template = (
        f"// Jaunt omitted {len(rendered)} imported type-context records to stay within "
        f"{_IMPORTED_TYPE_CONTEXT_LIMIT} UTF-8 bytes.\n"
    )
    available = (
        _IMPORTED_TYPE_CONTEXT_LIMIT
        if total <= _IMPORTED_TYPE_CONTEXT_LIMIT
        else _IMPORTED_TYPE_CONTEXT_LIMIT - len(omission_template.encode("utf-8"))
    )
    files: dict[str, str] = {}
    used = 0
    omitted = 0
    accepted_index = 0
    for name, source in rendered:
        size = len(source.encode("utf-8"))
        if used + size > available:
            omitted += 1
            continue
        files[f"_context/imported-types/{accepted_index:02d}-{name}"] = source
        accepted_index += 1
        used += size
    if omitted:
        files["_context/imported-types/zz-omitted.ts"] = (
            f"// Jaunt omitted {omitted} imported type-context records to stay within "
            f"{_IMPORTED_TYPE_CONTEXT_LIMIT} UTF-8 bytes.\n"
        )
    return files


def _test_request(
    root: Path,
    config: JauntConfig,
    test_spec: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    *,
    tier: str = "example",
    build_instructions: Sequence[str] | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> GenerationRequest:
    path = str(test_spec.get("path", ""))
    if tier not in {"example", "derived"}:
        raise ValueError(f"Unknown TypeScript test tier: {tier}")
    target_path = _test_output(path, _target(config).generated_dir, tier)
    targets = test_spec.get("targets", [])
    target_ids = [str(item) for item in targets] if isinstance(targets, list) else []
    test_spec_source = (
        str(test_spec["syntheticSource"])
        if isinstance(test_spec.get("syntheticSource"), str)
        else _safe_path(root, path).read_text(encoding="utf-8")
    )
    legacy_property_seed = (
        int.from_bytes(
            hashlib.sha256(f"{path}\0{json.dumps(sorted(target_ids))}".encode("utf-8")).digest()[
                :4
            ],
            "big",
        )
        & 0x7FFF_FFFF
    )
    selected = _selected_test_modules(test_spec, modules)
    contract_sources = tuple(str(module.get("specSource", "")) for module in selected)
    fixture_names = _fixture_names(test_spec_source, *contract_sources)
    fixture = _fixture_for_path(root, path)
    if fixture_names and fixture is None:
        raise JauntConfigError(
            f"{path} declares fixtures {', '.join(fixture_names)} but no canonical fixtures.ts "
            "or fixtures.tsx exists at its test owner"
        )
    facade_specifiers: list[tuple[str, str]] = []
    for module in selected:
        route = module.get("routes")
        module_facade = str(
            module.get(
                "facadePath", route.get("facadePath", "") if isinstance(route, Mapping) else ""
            )
        )
        specifier = _runtime_import_specifier(target_path, module_facade)
        facade_specifiers.append((_module_id(module), specifier))
    symbol_candidates: dict[str, set[str]] = {}
    for module, (_module_name, specifier) in zip(selected, facade_specifiers, strict=True):
        symbols = module.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str):
                symbol_candidates.setdefault(str(symbol["name"]), set()).add(specifier)
    symbol_specifiers = {
        name: next(iter(specifiers))
        for name, specifiers in symbol_candidates.items()
        if len(specifiers) == 1
    }
    property_cases = parse_property_cases(
        (test_spec_source, *contract_sources),
        label=f"TypeScript test intent {path}",
        public_symbols=symbol_specifiers,
        fixture_names=fixture_names,
        async_symbols=_async_export_names(*contract_sources),
    )
    property_count = len(property_cases)
    property_seed = property_cases[0].seed if property_cases else legacy_property_seed
    facade_specifier = facade_specifiers[0][1] if facade_specifiers else ""
    system = _prompt_text(config.typescript_prompts.test_system, "test_system.md")
    user = _prompt_text(config.typescript_prompts.test_module, "test_module.md")
    user = (
        user.replace("{{target_path}}", target_path)
        .replace("{{facade_specifier}}", facade_specifier)
        .replace("{{tier}}", tier)
    )
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    if effective_instructions:
        user += "\n\nAdditional project instructions:\n" + "\n".join(
            f"- {instruction}" for instruction in effective_instructions
        )
    if property_count:
        user += (
            "\n\nJaunt has parsed every supported `@prop` bullet into "
            "`_context/properties.json` and will append those property cases after generation. "
            "Do not import fast-check, call `fc.assert`, or write property tests yourself."
        )
    else:
        user += (
            "\n\nFor every fast-check property use literal options "
            f"`{{ seed: {property_seed}, numRuns: {_target(config).fast_check_runs} }}`."
        )
    if len(facade_specifiers) > 1:
        user += (
            "\n\nTarget public facades (import each target only from its listed facade):\n"
            + "\n".join(
                f"- {module_id}: `{specifier}`" for module_id, specifier in facade_specifiers
            )
        )
    context: dict[str, str] = {
        "_context/test-spec.ts": test_spec_source,
        "_context/contract.json": json.dumps(
            _model_contract(selected[0])
            if len(selected) == 1
            else {
                "targets": [
                    {**_model_contract(module), "facadeSpecifier": specifier}
                    for module, (_, specifier) in zip(selected, facade_specifiers, strict=True)
                ]
            },
            sort_keys=True,
            indent=2,
            default=str,
        )
        + "\n",
    }
    if property_cases:
        context["_context/properties.json"] = (
            json.dumps(
                [case.payload() for case in property_cases],
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
    if selected:
        context["_context/spec.ts"] = str(selected[0].get("specSource", ""))
        context["_context/api.ts"] = str(selected[0].get("apiSource", ""))
        for index, module in enumerate(selected[1:], start=1):
            context[f"_context/target_{index}.spec.ts"] = str(module.get("specSource", ""))
            context[f"_context/target_{index}.api.ts"] = str(module.get("apiSource", ""))
    imported_type_context = _imported_type_context_files(selected)
    if imported_type_context:
        context.update(imported_type_context)
        user += (
            "\n\nResolved declarations for workspace-local type-only imports are under "
            "`_context/imported-types/`. Use their exact required fields instead of guessing "
            "fixture shapes."
        )
    fixture_path = ""
    fixture_digest = ""
    fixture_specifier = ""
    if fixture is not None:
        fixture_path, fixture_source, fixture_digest = fixture
        fixture_specifier = _runtime_import_specifier(target_path, fixture_path)
        context["_context/fixtures.ts"] = fixture_source
        user += (
            f"\n\nThe canonical typed fixture surface is `_context/fixtures.ts`. Import its "
            f"extended `test` value from `{fixture_specifier}`."
        )
        if fixture_names:
            user += " Destructure these fixtures in the test callback: " + ", ".join(fixture_names)
    property_block = render_property_block(
        property_cases,
        symbol_specifiers=symbol_specifiers,
        num_runs=_target(config).fast_check_runs,
        fixture_specifier=fixture_specifier if fixture_names else "",
        fixture_names=fixture_names,
    )

    def validate(source_code: str) -> list[str]:
        errors = _static_test_validation(
            source_code,
            generated_dirs=(_target(config).generated_dir,),
        )
        if fixture_names:
            if fixture_specifier not in source_code:
                errors.append(
                    "generated fixture tests must import the extended test from "
                    f"{fixture_specifier}"
                )
            for name in fixture_names:
                if re.search(rf"\{{[^}}]*\b{re.escape(name)}\b[^}}]*\}}", source_code) is None:
                    errors.append(f"generated tests must destructure declared fixture {name}")
        if property_count < 1:
            return errors
        if "__jauntProperty" in source_code:
            errors.append("generated tests must not define Jaunt's reserved property bindings")
        if re.search(r'(?:from\s+|import\s*\()["\']fast-check["\']', source_code):
            errors.append("generated tests must leave deterministic @prop rendering to Jaunt")
        if re.search(r"\bfc\.(?:assert|property|asyncProperty)\b", source_code):
            errors.append("generated tests must leave deterministic @prop rendering to Jaunt")
        return errors

    return GenerationRequest(
        language="ts",
        kind="test",
        target_path=target_path,
        context_files=context,
        prompt=f"{system}\n\n{user}",
        cache_payload={
            "path": path,
            "targets": target_ids,
            "tier": tier,
            "propertySeed": property_seed,
            "fastCheckRuns": _target(config).fast_check_runs,
            "fixturePath": fixture_path,
            "fixtureDigest": fixture_digest,
            "buildInstructions": effective_instructions,
            "propertyCount": property_count,
            "propertyCases": [case.payload() for case in property_cases],
            "propertyBlock": property_block,
        },
        validator=validate,
        project_root=root,
        builtin_skill_names=(
            tuple(builtin_skill_names)
            if builtin_skill_names is not None
            else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
        ),
    )


def _implicit_class_test_specs(
    root: Path,
    config: JauntConfig,
    modules: Mapping[str, Mapping[str, Any]],
    explicit_specs: Sequence[Mapping[str, Any]] = (),
) -> tuple[Mapping[str, Any], ...]:
    """Create deterministic virtual test intents for opted-in magic classes."""

    target = _target(config)
    explicit_targets = {
        str(item)
        for spec in explicit_specs
        for item in (spec.get("targets", []) if isinstance(spec.get("targets"), list) else [])
    }
    roots: list[Path] = []
    for entry in target.test_roots:
        if any(character in entry for character in "*?["):
            roots.extend(path for path in sorted(root.glob(entry)) if path.is_dir())
        else:
            roots.append(_safe_path(root, entry))
    test_root = next(
        (path for path in roots if path.is_dir()), roots[0] if roots else root / "tests"
    )
    relative_root = test_root.relative_to(root)
    records: list[Mapping[str, Any]] = []
    for module_id, module in sorted(modules.items()):
        symbols = module.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        for symbol in symbols:
            if (
                not isinstance(symbol, Mapping)
                or symbol.get("kind") != "class"
                or not isinstance(symbol.get("name"), str)
            ):
                continue
            options = symbol.get("options", {})
            opted_in = target.auto_class_tests or (
                isinstance(options, Mapping) and options.get("test") is True
            )
            stable_id = f"{module_id}#{symbol['name']}"
            if not opted_in or stable_id in explicit_targets:
                continue
            slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", stable_id.removeprefix("ts:"))
            path = (relative_root / f"auto.{slug}.jaunt-test.ts").as_posix()
            records.append(
                {
                    "path": path,
                    "targets": [stable_id],
                    "syntheticSource": (
                        f"// Jaunt implicit class-test intent for {stable_id}.\n"
                        "// Derive public examples only from the class TSDoc and API mirror.\n"
                    ),
                }
            )
    return tuple(records)


def _selected_test_specs(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    *,
    target_ids: Sequence[str] = (),
) -> tuple[Mapping[str, Any], ...]:
    """Return authored and implicit test intents selected by a module target closure."""

    raw_specs = workspace.get("testSpecs", [])
    test_specs: list[Mapping[str, Any]] = (
        [item for item in raw_specs if isinstance(item, Mapping)]
        if isinstance(raw_specs, list)
        else []
    )
    test_specs.extend(
        _implicit_class_test_specs(
            root,
            config,
            modules,
            explicit_specs=test_specs,
        )
    )
    if not target_ids:
        return tuple(test_specs)
    requested = {target.split("#", 1)[0] for target in target_ids}
    selected: list[Mapping[str, Any]] = []
    for item in test_specs:
        raw_targets = item.get("targets", [])
        declared_modules = (
            {
                target.split("#", 1)[0]
                for target in raw_targets
                if isinstance(target, str) and target.startswith("ts:")
            }
            if isinstance(raw_targets, list)
            else set()
        )
        if declared_modules and requested.isdisjoint(declared_modules):
            continue
        if any(_module_id(module) in requested for module in _selected_test_modules(item, modules)):
            selected.append(item)
    return tuple(selected)


def _selected_generated_test_files(
    root: Path,
    config: JauntConfig,
    test_specs: Sequence[Mapping[str, Any]],
    *,
    target_ids: Sequence[str] = (),
) -> tuple[str, ...]:
    """Select committed batteries without leaking unrelated targeted-test owners."""

    if not target_ids:
        return _generated_test_files(root, config)
    target = _target(config)
    selected = {
        _test_output(str(spec.get("path", "")), target.generated_dir, tier)
        for spec in test_specs
        for tier in ("example", "derived")
    }
    return tuple(sorted(path for path in selected if path and _safe_path(root, path).is_file()))


def _test_battery_diagnostics(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    client: object,
    initialized: object,
    *,
    target_ids: Sequence[str] = (),
) -> tuple[TargetDiagnostic, ...]:
    """Verify generated test ownership and semantic provenance without executing it."""

    raw_specs = workspace.get("testSpecs", [])
    test_specs: list[Mapping[str, Any]] = (
        [item for item in raw_specs if isinstance(item, Mapping)]
        if isinstance(raw_specs, list)
        else []
    )
    test_specs.extend(_implicit_class_test_specs(root, config, modules, explicit_specs=test_specs))
    requested = {target.split("#", 1)[0] for target in target_ids}
    diagnostics: list[TargetDiagnostic] = []
    for test_spec in test_specs:
        raw_targets = test_spec.get("targets", [])
        declared_modules = {
            str(item).split("#", 1)[0] for item in raw_targets if isinstance(raw_targets, list)
        }
        if requested and declared_modules and not requested.intersection(declared_modules):
            continue
        selected = _selected_test_modules(test_spec, modules)
        if requested and not any(_module_id(module) in requested for module in selected):
            continue
        source_path = str(test_spec.get("path", ""))
        for tier in ("example", "derived"):
            relative = _test_output(source_path, _target(config).generated_dir, tier)
            path = _safe_path(root, relative)
            expected = _test_provenance(
                root,
                config,
                test_spec,
                modules,
                client,
                initialized,
                tier=tier,
                workspace=workspace,
            )
            if not path.is_file():
                distinct = _rejected_test_diagnostic(
                    root,
                    relative,
                    expected_fingerprint=str(expected["battery_fingerprint"]),
                    expected_provenance=expected,
                )
                diagnostics.append(
                    TargetDiagnostic(
                        code=(
                            "JAUNT_TS_TEST_GENERATION_EXHAUSTED"
                            if distinct is not None
                            else "JAUNT_TS_TEST_BATTERY_MISSING"
                        ),
                        message=(
                            f"The {tier} TypeScript battery for {source_path} "
                            + (
                                f"could not be generated; inspect {distinct['candidate']}; "
                                if distinct is not None
                                else "is missing; "
                            )
                            + "run `jaunt test --language ts`."
                        ),
                        path=relative,
                        data={
                            "scope": "magic",
                            "source": source_path,
                            "tier": tier,
                            **(
                                {
                                    "candidate": distinct["candidate"],
                                    "metadata": _rejected_test_paths(relative)[1].as_posix(),
                                    "consecutive_attempts": distinct.get("consecutive_attempts", 0),
                                }
                                if distinct is not None
                                else {}
                            ),
                        },
                    )
                )
                continue
            source = path.read_text(encoding="utf-8")
            metadata = _test_header_metadata(source)
            policy_errors = _static_test_validation(
                _strip_test_header(source),
                generated_dirs=(_target(config).generated_dir,),
            )
            if policy_errors:
                diagnostics.append(
                    TargetDiagnostic(
                        code="JAUNT_TS_TEST_PRIVATE_IMPORT",
                        message="; ".join(policy_errors),
                        path=relative,
                        data={
                            "scope": "magic",
                            "source": source_path,
                            "tier": tier,
                        },
                    )
                )
                continue
            mismatches: list[str] = []
            if metadata is None:
                mismatches.append("provenance")
            else:
                if metadata.get("tier") != tier:
                    mismatches.append("tier")
                if metadata.get("source") != source_path:
                    mismatches.append("source")
                mismatches.extend(sorted(_test_provenance_mismatches(metadata, expected)))
                rendered_body_digest = _sha256(
                    (_strip_test_header(source).rstrip() + "\n").encode("utf-8")
                )
                if metadata.get("body_digest") != rendered_body_digest:
                    mismatches.append("body_digest")
            if mismatches:
                mismatch_fields = set(mismatches)
                distinct = _rejected_test_diagnostic(
                    root,
                    relative,
                    expected_fingerprint=str(expected["battery_fingerprint"]),
                    expected_provenance=expected,
                )
                diagnostic_code = (
                    "JAUNT_TS_TEST_GENERATION_EXHAUSTED"
                    if distinct is not None
                    else "JAUNT_TS_TEST_BATTERY_STALE"
                )
                detail = (
                    " generation exhausted; inspect " + str(distinct["candidate"])
                    if distinct is not None
                    else " stale"
                )
                migration_safe_fields = (
                    {"fixture_fingerprint"}
                    if metadata is not None
                    and "fixture_fingerprint" not in metadata
                    and expected.get("fixture_fingerprint") == _canonical_digest(None)
                    else set()
                )
                remedy = (
                    "run `jaunt test --language ts --no-build` without `--no-run`, then "
                    "rerun `jaunt check`."
                    if _is_verifiable_api_transition(
                        mismatch_fields,
                        additional_allowed=migration_safe_fields,
                    )
                    else "run `jaunt test --language ts`."
                )
                diagnostics.append(
                    TargetDiagnostic(
                        code=diagnostic_code,
                        message=(
                            f"The {tier} TypeScript battery for {source_path} is{detail} "
                            f"({', '.join(sorted(mismatch_fields))}); {remedy}"
                        ),
                        path=relative,
                        data={
                            "scope": "magic",
                            "source": source_path,
                            "tier": tier,
                            "mismatches": tuple(sorted(mismatch_fields)),
                            **(
                                {
                                    "candidate": distinct["candidate"],
                                    "metadata": _rejected_test_paths(relative)[1].as_posix(),
                                    "consecutive_attempts": distinct.get("consecutive_attempts", 0),
                                }
                                if distinct is not None
                                else {}
                            ),
                        },
                    )
                )
    return tuple(diagnostics)


def _generated_test_files(root: Path, config: JauntConfig) -> tuple[str, ...]:
    target = _target(config)
    files: set[str] = set()
    for entry in target.test_roots:
        if any(char in entry for char in "*?["):
            roots = [path for path in root.glob(entry) if path.is_dir()]
        else:
            candidate = _safe_path(root, entry)
            roots = [candidate] if candidate.is_dir() else []
        for test_root in roots:
            generated = target.generated_dir.strip("/")
            for pattern in (f"**/{generated}/*.test.ts", f"**/{generated}/*.test.tsx"):
                files.update(path.relative_to(root).as_posix() for path in test_root.glob(pattern))
    battery = _safe_path(root, target.contract_battery_dir)
    if battery.is_dir():
        files.update(path.relative_to(root).as_posix() for path in battery.rglob("*.test.ts"))
        files.update(path.relative_to(root).as_posix() for path in battery.rglob("*.test.tsx"))
    return tuple(sorted(files))


def _expanded_test_projects(root: Path, config: JauntConfig) -> tuple[str, ...]:
    target = _target(config)
    entries = target.test_projects or target.projects
    projects: set[str] = set()
    for entry in entries:
        if any(character in entry for character in "*?["):
            projects.update(
                path.relative_to(root).as_posix() for path in root.glob(entry) if path.is_file()
            )
        elif _safe_path(root, entry).is_file():
            projects.add(entry)
    return tuple(sorted(projects))


def _workspace_test_projects(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
) -> tuple[str, ...]:
    configured = set(_expanded_test_projects(root, config))
    raw_projects = workspace.get("projects", [])
    if isinstance(raw_projects, list):
        configured.update(
            str(project.get("id", project.get("configPath")))
            for project in raw_projects
            if isinstance(project, Mapping)
            and isinstance(project.get("id", project.get("configPath")), str)
            and (
                project.get("role") == "test"
                or str(project.get("id", project.get("configPath"))) in configured
            )
        )
    return tuple(sorted(configured))


def _workspace_project_config_paths(workspace: Mapping[str, Any]) -> tuple[str, ...]:
    """Return every worker-resolved project config in stable order."""

    raw_projects = workspace.get("projects", [])
    if not isinstance(raw_projects, list):
        return ()
    return tuple(
        sorted(
            {
                str(project.get("configPath", project.get("id")))
                for project in raw_projects
                if isinstance(project, Mapping)
                and isinstance(project.get("configPath", project.get("id")), str)
            }
        )
    )


def _owner_project_for_source(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    source_path: str,
) -> str:
    projects = _workspace_test_projects(root, config, workspace)
    source = Path(source_path)
    containing: list[tuple[int, str]] = []
    for project in projects:
        directory = Path(project).parent
        try:
            source.relative_to(directory)
        except ValueError:
            continue
        containing.append((len(directory.parts), project))
    if containing:
        depth = max(item[0] for item in containing)
        nearest = sorted(project for item_depth, project in containing if item_depth == depth)
        if len(nearest) == 1:
            return nearest[0]
    if len(projects) == 1:
        return projects[0]
    raise JauntConfigError(
        f"Cannot determine one configured test-project owner for {source_path!r}"
    )


def _contract_test_owners(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
) -> Mapping[str, str]:
    from jaunt.typescript.contracts import _battery_path

    owners: dict[str, str] = {}
    contracts = workspace.get("contracts", [])
    if not isinstance(contracts, list):
        return owners
    for contract in contracts:
        if not isinstance(contract, Mapping) or not isinstance(contract.get("path"), str):
            continue
        source_relative = str(contract["path"])
        source = _safe_path(root, source_relative)
        symbols = contract.get("symbols", [])
        if not isinstance(symbols, list):
            continue
        owner = _owner_project_for_source(root, config, workspace, source_relative)
        for raw_symbol in symbols:
            symbol = (
                str(raw_symbol.get("name")) if isinstance(raw_symbol, Mapping) else str(raw_symbol)
            )
            battery = _battery_path(root, config, source, symbol)
            owners[battery.relative_to(root).as_posix()] = owner
    return owners


def _workspace_test_file_owners(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
) -> Mapping[str, str]:
    owners = dict(_contract_test_owners(root, config, workspace))
    test_specs = workspace.get("testSpecs", [])
    if isinstance(test_specs, list):
        for test_spec in test_specs:
            if not isinstance(test_spec, Mapping):
                continue
            path = test_spec.get("path")
            project = test_spec.get("project")
            if isinstance(path, str) and isinstance(project, str):
                for tier in ("example", "derived"):
                    owners[_test_output(path, _target(config).generated_dir, tier)] = project
    return owners


def _group_test_files(
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    files: Sequence[str],
    *,
    explicit_owners: Mapping[str, str] | None = None,
) -> Mapping[str, tuple[str, ...]]:
    """Assign each explicit test file to exactly one worker-resolved test project."""

    configured = set(_workspace_test_projects(root, config, workspace))
    project_files: dict[str, set[str]] = {project: set() for project in configured}
    raw_projects = workspace.get("projects", [])
    if isinstance(raw_projects, list):
        for project in raw_projects:
            if not isinstance(project, Mapping):
                continue
            project_id = project.get("id", project.get("configPath"))
            if not isinstance(project_id, str):
                continue
            if project.get("role") == "test" or project_id in configured:
                configured.add(project_id)
                roots = project.get("rootFiles", [])
                if isinstance(roots, list):
                    project_files.setdefault(project_id, set()).update(
                        str(path) for path in roots if isinstance(path, str)
                    )
    owners = dict(explicit_owners or {})
    grouped: dict[str, list[str]] = {}
    for file in sorted(set(files)):
        explicit = owners.get(file)
        if explicit is not None:
            if explicit not in configured:
                raise JauntConfigError(
                    f"TypeScript test owner {explicit!r} for {file} is not a configured "
                    "test project"
                )
            matches = [explicit]
        else:
            matches = [project for project, roots in project_files.items() if file in roots]
            if not matches and len(configured) == 1:
                matches = list(configured)
        if len(matches) != 1:
            qualifier = "ambiguous" if matches else "unowned"
            raise JauntConfigError(
                f"TypeScript test file {file!r} is {qualifier} across configured test projects"
            )
        grouped.setdefault(matches[0], []).append(file)
    return {
        project: tuple(sorted(project_files_for_owner))
        for project, project_files_for_owner in sorted(grouped.items())
    }


def _aggregate_runner_batches(
    results: Mapping[str, Mapping[str, Any]],
    *,
    mode: str,
) -> Mapping[str, Any]:
    if not results:
        return {"ok": True, "mode": mode, "skipped": True, "batches": {}}
    tests: list[Any] = []
    diagnostics: list[Any] = []
    failures: list[Any] = []
    stdout: list[str] = []
    stderr: list[str] = []
    for result in results.values():
        for key, destination in (
            ("tests", tests),
            ("diagnostics", diagnostics),
            ("failures", failures),
        ):
            values = result.get(key)
            if isinstance(values, list):
                destination.extend(values)
        captured = result.get("captured")
        if isinstance(captured, Mapping):
            if isinstance(captured.get("stdout"), str):
                stdout.append(str(captured["stdout"]))
            if isinstance(captured.get("stderr"), str):
                stderr.append(str(captured["stderr"]))
    return {
        "ok": all(bool(result.get("ok", False)) for result in results.values()),
        "mode": mode,
        "batches": {project: dict(result) for project, result in results.items()},
        "tests": tests,
        "diagnostics": diagnostics,
        "failures": failures,
        "captured": {"stdout": "".join(stdout), "stderr": "".join(stderr)},
        **(
            {"timedOut": True}
            if any(bool(result.get("timedOut", False)) for result in results.values())
            else {}
        ),
        **(
            {
                "exitCode": max(
                    int(result["exitCode"])
                    for result in results.values()
                    if isinstance(result.get("exitCode"), int)
                    and not isinstance(result.get("exitCode"), bool)
                )
            }
            if any(
                isinstance(result.get("exitCode"), int)
                and not isinstance(result.get("exitCode"), bool)
                for result in results.values()
            )
            else {}
        ),
    }


def _runner_path(client: object) -> Path:
    installation = getattr(client, "installation", None)
    package_root = getattr(installation, "package_root", None)
    if not isinstance(package_root, Path):
        raise RuntimeError("The TypeScript worker installation has no test-runner package root")
    path = package_root / "dist" / "test" / "runner.js"
    if not path.is_file():
        worker_entry = getattr(installation, "worker_entry", None)
        if isinstance(worker_entry, Path) and worker_entry.parent.name == "worker":
            path = worker_entry.parent.parent / "test" / "runner.js"
    if not path.is_file():
        raise RuntimeError(f"Installed @usejaunt/ts has no test runner at {path}")
    return path


class _HeldOutLeakError(RuntimeError):
    """A generic signal that never carries the protected value it detected."""


_RUNNER_CATEGORIES = {
    "assertion",
    "timeout",
    "type",
    "runtime",
    "collection",
    "runner",
    "runner-protocol",
}
_IMPLEMENTATION_REPAIR_CATEGORIES = {"assertion", "type", "runtime"}
_CANDIDATE_REJECTION_CATEGORIES = _IMPLEMENTATION_REPAIR_CATEGORIES | {"collection"}
_RUNNER_DIAGNOSTIC_CODE = re.compile(r"(?:TS\d+|JAUNT_TS_[A-Z0-9_]+)")
_OPAQUE_CASE_ID = re.compile(r"[0-9a-f]{16}")
_MAX_PROTECTED_DIAGNOSTIC_MESSAGE_CHARS = 2_000
_DIAGNOSTIC_TRUNCATION_MARKER = "\n[jaunt: diagnostic truncated]"


def _bounded_runner_diagnostic_message(message: str) -> str:
    if len(message) <= _MAX_PROTECTED_DIAGNOSTIC_MESSAGE_CHARS:
        return message
    limit = _MAX_PROTECTED_DIAGNOSTIC_MESSAGE_CHARS - len(_DIAGNOSTIC_TRUNCATION_MARKER)
    return message[:limit] + _DIAGNOSTIC_TRUNCATION_MARKER


def _safe_runner_case_id(value: object) -> bool:
    return isinstance(value, str) and (
        _OPAQUE_CASE_ID.fullmatch(value) is not None or value == "opaque-runner-failure"
    )


def _safe_runner_category(value: object) -> bool:
    return isinstance(value, str) and value in _RUNNER_CATEGORIES


def _runner_startup_failure(item: object) -> str | None:
    """Return the one message-bearing record the protected runner may expose."""

    if not isinstance(item, Mapping):
        return None
    record = cast(Mapping[object, object], item)
    if set(record) != {"caseId", "category", "message"}:
        return None
    message = record["message"]
    if (
        record["caseId"] != "opaque-runner-failure"
        or record["category"] != "runner"
        or not isinstance(message, str)
        or len(message) > _MAX_PROTECTED_DIAGNOSTIC_MESSAGE_CHARS
    ):
        return None
    return message


def _safe_runner_diagnostic(item: object) -> bool:
    if not isinstance(item, Mapping):
        return False
    record = {str(key): value for key, value in item.items()}
    return bool(
        isinstance(record.get("code"), str)
        and _RUNNER_DIAGNOSTIC_CODE.fullmatch(str(record["code"])) is not None
        and record.get("severity") in {"error", "warning", "info"}
    )


def _redaction_surface_valid(result: Mapping[str, Any]) -> bool:
    if not isinstance(result.get("ok"), bool):
        return False
    exit_code = result.get("exitCode")
    if exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int)):
        return False
    diagnostics = result.get("diagnostics", [])
    if not isinstance(diagnostics, list) or any(
        not _safe_runner_diagnostic(item) for item in diagnostics
    ):
        return False
    failures = result.get("failures", [])
    if not isinstance(failures, list) or any(
        not isinstance(item, Mapping) or not _safe_runner_category(item.get("category"))
        for item in failures
    ):
        return False
    tests = result.get("tests", [])
    if not isinstance(tests, list):
        return False
    for item in tests:
        if not isinstance(item, Mapping):
            return False
        if str(item.get("tier", "derived")) == "example":
            continue
        if "message" in item and "tier" not in item and _runner_startup_failure(item) is None:
            return False
        case_id = item.get("caseId")
        category = item.get("category")
        if case_id is not None and not _safe_runner_case_id(case_id):
            return False
        if category is not None and not _safe_runner_category(category):
            return False
    return True


def _valid_runner_dto(
    result: Mapping[str, Any],
    *,
    expected_mode: str,
    redact_derived: bool,
) -> bool:
    """Validate the disposable runner response before trusting its success bit."""

    required = {"ok", "mode", "diagnostics", "tests", "captured"}
    allowed = required | {"emittedDeclarations", "emittedJavaScript"}
    if set(result) - allowed or not required.issubset(result):
        return False
    if not isinstance(result.get("ok"), bool) or result.get("mode") != expected_mode:
        return False
    diagnostics = result.get("diagnostics")
    tests = result.get("tests")
    captured = result.get("captured")
    if not isinstance(diagnostics, list) or not isinstance(tests, list):
        return False
    if not isinstance(captured, Mapping) or set(captured) != {"stdout", "stderr"}:
        return False
    if any(not isinstance(captured.get(key), str) for key in ("stdout", "stderr")):
        return False
    for emitted_key in ("emittedDeclarations", "emittedJavaScript"):
        emitted = result.get(emitted_key)
        if emitted is not None and (
            not isinstance(emitted, list) or any(not isinstance(path, str) for path in emitted)
        ):
            return False

    diagnostic_allowed = {"code", "severity", "message", "path", "start", "end", "line", "column"}
    for diagnostic in diagnostics:
        if (
            not _safe_runner_diagnostic(diagnostic)
            or not isinstance(diagnostic, Mapping)
            or set(diagnostic) - diagnostic_allowed
            or not isinstance(diagnostic.get("message"), str)
        ):
            return False
        if "path" in diagnostic and not isinstance(diagnostic["path"], str):
            return False
        for key in ("start", "end", "line", "column"):
            value = diagnostic.get(key)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                return False

    test_allowed = {"file", "tier", "status", "caseId", "category", "durationMs", "message"}
    protected_derived_allowed = {"caseId", "category"}
    failed = False
    for item in tests:
        if not isinstance(item, Mapping):
            return False
        keys = set(item)
        if expected_mode == "run" and _runner_startup_failure(item) is not None:
            failed = True
            continue
        if redact_derived and keys == protected_derived_allowed:
            if not _safe_runner_case_id(item.get("caseId")) or not _safe_runner_category(
                item.get("category")
            ):
                return False
            failed = True
            continue
        if keys - test_allowed:
            return False
        if (
            not isinstance(item.get("file"), str)
            or item.get("tier") not in {"example", "derived"}
            or item.get("status") not in {"passed", "failed", "skipped"}
        ):
            return False
        if redact_derived and item.get("tier") == "derived":
            return False
        duration = item.get("durationMs")
        if (
            isinstance(duration, bool)
            or not isinstance(duration, (int, float))
            or not math.isfinite(float(duration))
            or duration < 0
        ):
            return False
        is_failed = item.get("status") == "failed"
        failed = failed or is_failed
        if is_failed:
            if not _safe_runner_case_id(item.get("caseId")) or not _safe_runner_category(
                item.get("category")
            ):
                return False
        elif "caseId" in item or "category" in item or "message" in item:
            return False
        if item.get("tier") == "derived" and redact_derived and "message" in item:
            return False
        if "message" in item and not isinstance(item["message"], str):
            return False

    if expected_mode == "run" and not tests and not redact_derived:
        return False
    if expected_mode == "typecheck" and tests:
        return False
    has_error = any(
        isinstance(item, Mapping) and item.get("severity") == "error" for item in diagnostics
    )
    return result["ok"] is (not failed and not has_error)


def _string_values(value: object) -> set[str]:
    values: set[str] = set()
    pending = [value]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if isinstance(current, str):
            if current:
                values.add(current)
            stripped = current.strip()
            if stripped:
                values.add(stripped)
            continue
        if isinstance(current, Mapping):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            pending.extend(current.values())
            continue
        if isinstance(current, (list, tuple, set, frozenset)):
            identity = id(current)
            if identity in seen:
                continue
            seen.add(identity)
            pending.extend(current)
    return values


def _runner_surfaces(result: Mapping[str, Any]) -> tuple[list[object], list[object]]:
    """Split raw runner data into deliberately public and held-out surfaces."""

    allowed: list[object] = []
    sensitive: list[object] = []
    for key, value in result.items():
        if key in {"ok", "mode", "timedOut", "skipped", "exitCode"}:
            allowed.append(value)
            continue
        if key == "tests" and isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    sensitive.append(item)
                    continue
                if str(item.get("tier", "derived")) == "example":
                    allowed.append(item)
                    continue
                if _runner_startup_failure(item) is not None:
                    allowed.append(item)
                    continue
                public_keys = {"caseId", "category"}
                allowed.append({name: item[name] for name in public_keys if name in item})
                sensitive.append(
                    {
                        name: item[name]
                        for name in item
                        if name not in public_keys and name not in {"tier", "status"}
                    }
                )
            continue
        if key == "diagnostics" and isinstance(value, list):
            public_keys = {"code", "severity", "path", "line", "column"}
            if result.get("mode") == "typecheck":
                public_keys.add("message")
            for item in value:
                if not isinstance(item, Mapping):
                    sensitive.append(item)
                    continue
                allowed.append({name: item[name] for name in public_keys if name in item})
                sensitive.append({name: item[name] for name in item if name not in public_keys})
            continue
        if key == "failures" and isinstance(value, list):
            for item in value:
                if not isinstance(item, Mapping):
                    sensitive.append(item)
                    continue
                allowed.append({"category": item["category"]} if "category" in item else {})
                sensitive.append({name: item[name] for name in item if name != "category"})
            continue
        # Captured streams, warnings, serialized errors, batch internals, and
        # future unknown fields are held out by default.
        sensitive.append(value)
    return allowed, sensitive


def _assert_no_held_out_leak(
    raw: Mapping[str, Any],
    protected: Mapping[str, Any],
) -> None:
    """Verify after projection that no dropped runner detail remains visible."""

    tests = protected.get("tests")
    if isinstance(tests, list):
        for item in tests:
            if not isinstance(item, Mapping):
                raise _HeldOutLeakError(
                    "Protected runner output failed the held-out shape assertion"
                )
            if item.get("tier") == "example":
                continue
            if _runner_startup_failure(item) is not None:
                continue
            if not set(item).issubset({"caseId", "category"}):
                raise _HeldOutLeakError(
                    "Protected runner output failed the held-out shape assertion"
                )

    allowed, sensitive = _runner_surfaces(raw)
    allowed_strings = _string_values(allowed)
    secrets = {
        value
        for value in _string_values(sensitive)
        if len(value) >= 4 and value not in allowed_strings
    }
    rendered = _string_values(protected)
    if any(secret in value for secret in secrets for value in rendered):
        raise _HeldOutLeakError("Protected runner output failed the held-out leak assertion")


def _redacted_runner_failure(result: Mapping[str, Any]) -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "ok": False,
        "failures": [{"category": "runner"}],
        "captured": {"stdout": "", "stderr": ""},
    }
    mode = result.get("mode")
    if mode in {"run", "typecheck"}:
        fallback["mode"] = mode
    if result.get("timedOut") is True:
        fallback["timedOut"] = True
    return fallback


def _redact_runner_result(result: Mapping[str, Any], *, enabled: bool) -> dict[str, Any]:
    if not enabled:
        return dict(result)
    if not _redaction_surface_valid(result):
        return _redacted_runner_failure(result)
    copy: dict[str, Any] = {"ok": result["ok"]}
    for key in ("mode", "timedOut", "skipped", "exitCode"):
        if key in result:
            copy[key] = result[key]
    diagnostics = result.get("diagnostics")
    if isinstance(diagnostics, list):
        protected_diagnostics: list[dict[str, Any]] = []
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, Mapping):
                continue
            protected = {
                key: diagnostic[key]
                for key in ("code", "severity", "path", "line", "column")
                if key in diagnostic
            }
            message = diagnostic.get("message")
            if result.get("mode") == "typecheck" and isinstance(message, str):
                protected["message"] = _bounded_runner_diagnostic_message(message)
            protected_diagnostics.append(protected)
        copy["diagnostics"] = protected_diagnostics
    failures = result.get("failures")
    if isinstance(failures, list):
        copy["failures"] = [
            {"category": str(failure["category"])}
            for failure in failures
            if isinstance(failure, Mapping)
        ]
    tests = result.get("tests")
    if not isinstance(tests, list):
        copy["captured"] = {"stdout": "", "stderr": ""}
    else:
        redacted: list[dict[str, Any]] = []
        for test in tests:
            if not isinstance(test, Mapping):
                continue
            tier = str(test.get("tier", "derived"))
            if tier == "example":
                redacted.append(dict(test))
                continue
            startup_message = _runner_startup_failure(test)
            if startup_message is not None:
                redacted.append(
                    {
                        "caseId": "opaque-runner-failure",
                        "category": "runner",
                        "message": startup_message,
                    }
                )
                continue
            public = {key: str(test[key]) for key in ("caseId", "category") if key in test}
            if public:
                redacted.append(public)
        copy["tests"] = redacted
        copy["captured"] = {"stdout": "", "stderr": ""}
    try:
        _assert_no_held_out_leak(result, copy)
    except _HeldOutLeakError:
        return _redacted_runner_failure(result)
    return copy


def _implementation_repair_feedback(result: Mapping[str, Any]) -> str:
    """Render the strict, prompt-only failure surface for one implementation repair."""

    protected = _redact_runner_result(result, enabled=True)
    tests: list[dict[str, Any]] = []
    raw_tests = protected.get("tests", [])
    if isinstance(raw_tests, list):
        for item in raw_tests:
            if not isinstance(item, Mapping):
                continue
            if item.get("tier") == "example":
                if item.get("status") != "failed":
                    continue
                allowed = (
                    "file",
                    "caseId",
                    "tier",
                    "status",
                    "category",
                    "durationMs",
                    "message",
                )
            else:
                # A derived record is present only when the runner supplied an
                # opaque failure ID and/or a normalized category. Passing
                # derived tests project to no record at all.
                allowed = ("caseId", "category")
                if not any(key in item for key in allowed):
                    continue
            tests.append({key: item[key] for key in allowed if key in item})
    raw_diagnostics = protected.get("diagnostics", [])
    diagnostics = (
        [
            {key: item[key] for key in ("code", "severity") if key in item}
            for item in raw_diagnostics
            if isinstance(item, Mapping)
        ]
        if isinstance(raw_diagnostics, list)
        else []
    )
    failures = protected.get("failures", [])
    payload = {
        "tests": tests,
        "diagnostics": diagnostics,
        "failures": failures if isinstance(failures, list) else [],
        **({"timedOut": True} if protected.get("timedOut") is True else {}),
    }
    return (
        "The committed generated Vitest battery failed against the current implementation. "
        "Perform one bounded implementation-only repair. Keep the authored spec, API, and "
        "tests unchanged; edit only the reserved implementation bindings requested by the "
        "normal build prompt. Example-tier messages are authored contract evidence. "
        "Derived-tier failures are held out, so only their opaque case IDs and normalized "
        "categories are available.\n\nProtected runner result:\n"
        + json.dumps(payload, sort_keys=True, indent=2, ensure_ascii=True)
    )


def _is_reviewable_example_battery(path: str, source: str) -> bool:
    if re.search(r"\.example\.test\.(?:ts|tsx)$", path) is None:
        return False
    metadata = _test_header_metadata(source)
    if metadata is None or metadata.get("tier") != "example":
        return False
    body_digest = metadata.get("body_digest")
    expected = _sha256(_strip_test_header(source).encode("utf-8"))
    return body_digest == expected and (
        isinstance(body_digest, str)
        and re.fullmatch(r"sha256:[0-9a-f]{64}", body_digest) is not None
    )


class _RepairFileTransaction:
    __slots__ = ("_commit", "_publish")

    def __init__(
        self,
        *,
        commit: Callable[[], None],
        publish: Callable[
            [
                Sequence[_Write],
                Mapping[str, str],
                Callable[[], None] | None,
                Callable[[], None] | None,
            ],
            None,
        ],
    ) -> None:
        self._commit = commit
        self._publish = publish

    def publish(
        self,
        writes: Sequence[_Write],
        *,
        expected_inputs: Mapping[str, str],
        pre_commit_guard: Callable[[], None] | None = None,
        commit_seal: Callable[[], None] | None = None,
    ) -> None:
        self._publish(writes, expected_inputs, pre_commit_guard, commit_seal)

    def commit(self) -> None:
        self._commit()


_TEST_REPAIR_SCHEME = "jaunt-ts-test-repair/2"


def _repair_snapshot_bytes(
    snapshot: Mapping[str, Any],
    manifest: Path,
) -> tuple[bytes | None, int | None]:
    encoded = snapshot.get("content")
    mode = snapshot.get("mode")
    if encoded is None and mode is None:
        return None, None
    if not isinstance(encoded, str) or not isinstance(mode, int):
        raise JauntConfigError(f"Invalid snapshot in test-repair marker: {manifest}")
    try:
        return base64.b64decode(encoded, validate=True), mode
    except ValueError as error:
        raise JauntConfigError(f"Invalid backup bytes in test-repair marker: {manifest}") from error


def _replace_repair_file(
    directory: _PinnedDirectory,
    path: Path,
    content: bytes,
    mode: int,
) -> None:
    """Durably replace one repair-owned file with exact saved bytes."""

    descriptor, temporary = directory.create_temp(prefix=f".{path.name}.jaunt-repair-")
    try:
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                if hasattr(os, "fchmod"):
                    os.fchmod(stream.fileno(), mode)
                os.fsync(stream.fileno())
        finally:
            os.close(descriptor)
        directory.replace(temporary, path.name)
        directory.fsync_required()
    finally:
        directory.unlink(temporary, missing_ok=True)


def _restore_repair_snapshot(
    directory: _PinnedDirectory,
    path: Path,
    content: bytes | None,
    mode: int | None,
) -> None:
    if content is None:
        existed = directory.unlink(path.name, missing_ok=True)
        if existed:
            directory.fsync_required()
        return
    _replace_repair_file(
        directory,
        path,
        content,
        mode if mode is not None else 0o644,
    )


def _repair_path_hash(directory: _PinnedDirectory, path: Path) -> str:
    return directory.path_hash(path.name) or MISSING_INPUT


def _recover_pending_test_repairs(root: Path) -> tuple[str, ...]:
    """Restore durable pre-repair bytes left by a terminated Jaunt process."""

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    with _PinnedWorkspace(root) as workspace:
        try:
            transaction_directory = workspace.directory(directory, create=False)
        except FileNotFoundError:
            return ()
        lease = _acquire_transaction_lease(
            directory,
            blocking=True,
            pinned_directory=transaction_directory,
            authority_directory=workspace.root_directory,
        )
        if lease is None:  # pragma: no cover - blocking acquisition
            raise JauntConfigError("Could not acquire the TypeScript transaction recovery lease")
        restored: list[str] = []
        try:
            # The first scan happens only after acquisition: a live writer holds
            # this lease before it creates its marker, so a pre-lease empty scan
            # could otherwise let a worker observe transient repair bytes.
            for manifest_name in transaction_directory.iter_names("test-repair-*.json"):
                manifest = directory / manifest_name
                try:
                    payload = json.loads(
                        transaction_directory.read_bytes(manifest.name).decode("utf-8")
                    )
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise JauntConfigError(
                        f"Invalid TypeScript test-repair marker: {manifest}"
                    ) from error
                if not isinstance(payload, Mapping) or payload.get("scheme") != _TEST_REPAIR_SCHEME:
                    raise JauntConfigError(f"Invalid TypeScript test-repair marker: {manifest}")
                owner_pid = payload.get("ownerPid")
                if not isinstance(owner_pid, int) or owner_pid < 1:
                    raise JauntConfigError(
                        f"TypeScript test-repair marker has no owner PID: {manifest}"
                    )
                # The v2 writer holds the global lease for its full lifetime. Once
                # this process owns that lease there cannot be a conforming live
                # owner, even if a PID was reused or a prior attempt in this same
                # process failed during retirement. Avoid non-portable PID probes.
                snapshots = payload.get("snapshots")
                if not isinstance(snapshots, list):
                    raise JauntConfigError(
                        f"TypeScript test-repair marker has no snapshots: {manifest}"
                    )
                restored_paths: set[str] = set()
                for snapshot in snapshots:
                    if not isinstance(snapshot, Mapping) or not isinstance(
                        snapshot.get("path"), str
                    ):
                        raise JauntConfigError(
                            f"Invalid snapshot in test-repair marker: {manifest}"
                        )
                    relative = str(snapshot["path"])
                    _safe_path(root, relative)
                    path = root / Path(relative)
                    expected_after = snapshot.get("after")
                    if expected_after is None:
                        continue
                    if expected_after != MISSING_INPUT and (
                        not isinstance(expected_after, str)
                        or re.fullmatch(r"sha256:[0-9a-f]{64}", expected_after) is None
                    ):
                        raise JauntConfigError(
                            f"Invalid after hash in test-repair marker: {manifest}"
                        )
                    content, mode = _repair_snapshot_bytes(snapshot, manifest)
                    output_directory = workspace.directory(path.parent)
                    workspace.verify_namespace()
                    # A later writer owns any non-matching state. Recovery is a
                    # compare-and-swap, never an unconditional historical restore.
                    try:
                        current_hash = _repair_path_hash(output_directory, path)
                    except OSError as error:
                        raise JauntConfigError(
                            f"Could not inspect TypeScript test-repair path: {relative}"
                        ) from error
                    if current_hash != expected_after:
                        continue
                    _restore_repair_snapshot(output_directory, path, content, mode)
                    restored_paths.add(relative)
                    restored.append(relative)
                for transaction_name in transaction_directory.iter_names("ts-*.json"):
                    transaction = directory / transaction_name
                    try:
                        value = json.loads(
                            transaction_directory.read_bytes(transaction.name).decode("utf-8")
                        )
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        continue
                    writes = value.get("writes") if isinstance(value, Mapping) else None
                    paths = {
                        str(write.get("path"))
                        for write in writes or []
                        if isinstance(write, Mapping) and isinstance(write.get("path"), str)
                    }
                    if paths and paths <= restored_paths and isinstance(value, Mapping):
                        workspace.verify_namespace()
                        if not _retire_transaction_manifest(
                            transaction,
                            value,
                            pinned_directory=transaction_directory,
                        ):
                            raise JauntConfigError(
                                f"Could not durably retire TypeScript transaction: {transaction}"
                            )
                workspace.verify_namespace()
                if not _retire_transaction_manifest(
                    manifest,
                    payload,
                    pinned_directory=transaction_directory,
                ):
                    raise JauntConfigError(
                        f"Could not durably retire TypeScript test-repair marker: {manifest}"
                    )
            return tuple(sorted(set(restored)))
        finally:
            lease.release()


@contextmanager
def _preserve_managed_files(
    root: Path,
    paths: Sequence[str],
) -> Iterator[_RepairFileTransaction]:
    """Publish or CAS-rollback one bounded repair under the global lease."""

    root = root.resolve()
    originals: dict[Path, tuple[bytes | None, int | None]] = {}
    original_hashes: dict[Path, str] = {}
    expected_after: dict[Path, str] = {}
    committed = False
    directory = root / ".jaunt" / "transactions"
    manifest = directory / f"test-repair-{uuid.uuid4().hex}.json"
    payload: dict[str, Any] = {}
    pinned_workspace = _PinnedWorkspace(root)
    transaction_directory: _PinnedDirectory | None = None

    def pin_for(path: Path) -> _PinnedDirectory:
        return pinned_workspace.directory(path.parent)

    def transaction_pin() -> _PinnedDirectory:
        if transaction_directory is None:  # pragma: no cover - closure ordering
            raise RuntimeError("TypeScript repair transaction directory is not pinned")
        return transaction_directory

    def write_manifest() -> None:
        payload.clear()
        payload.update(
            {
                "scheme": _TEST_REPAIR_SCHEME,
                "ownerPid": os.getpid(),
                "snapshots": [
                    {
                        "path": path.relative_to(root).as_posix(),
                        "content": (
                            base64.b64encode(content).decode("ascii")
                            if content is not None
                            else None
                        ),
                        "mode": mode,
                        **({"after": expected_after[path]} if path in expected_after else {}),
                    }
                    for path, (content, mode) in sorted(
                        originals.items(), key=lambda item: item[0].as_posix()
                    )
                ],
            }
        )
        _write_transaction_manifest(
            manifest,
            payload,
            pinned_directory=transaction_pin(),
        )

    def add_paths(values: Sequence[str]) -> None:
        for relative in sorted(set(values)):
            _safe_path(root, relative)
            path = root / Path(relative)
            if path in originals:
                continue
            pinned_directory = pin_for(path)
            try:
                content, metadata = pinned_directory.read_bytes_with_stat(path.name)
                mode = stat.S_IMODE(metadata.st_mode)
            except FileNotFoundError:
                content = None
                mode = None
            except OSError as error:
                raise JauntConfigError(
                    f"Could not snapshot managed repair path: {relative}"
                ) from error
            original_hash = _sha256(content) if content is not None else MISSING_INPUT
            if _repair_path_hash(pinned_directory, path) != original_hash:
                raise JauntConfigError(f"Managed repair path changed while read: {relative}")
            originals[path] = (content, mode)
            original_hashes[path] = original_hash
        write_manifest()

    def publish(
        writes: Sequence[_Write],
        expected_inputs: Mapping[str, str],
        pre_commit_guard: Callable[[], None] | None,
        commit_seal: Callable[[], None] | None,
    ) -> None:
        writes_by_path: dict[Path, _Write] = {}
        for write in writes:
            _safe_path(root, write.path)
            path = root / Path(write.path)
            if path in writes_by_path:
                raise JauntGenerationError(f"Duplicate TypeScript artifact path: {write.path}")
            writes_by_path[path] = write
        add_paths(tuple(write.path for write in writes))
        _assert_inputs_unchanged(root, expected_inputs)
        for path, before in original_hashes.items():
            if path in writes_by_path and _repair_path_hash(pin_for(path), path) != before:
                raise JauntGenerationError(
                    f"TypeScript artifact changed during validation: {path.relative_to(root)}"
                )

        staged: dict[Path, str] = {}
        try:
            for path, write in writes_by_path.items():
                if write.content is None:
                    expected_after[path] = MISSING_INPUT
                    continue
                pinned_directory = pin_for(path)
                descriptor, temporary = pinned_directory.create_temp(
                    prefix=f".{path.name}.jaunt-repair-"
                )
                staged[path] = temporary
                try:
                    with os.fdopen(descriptor, "wb", closefd=False) as stream:
                        content = write.content.encode("utf-8")
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                finally:
                    os.close(descriptor)
                expected_after[path] = _sha256(content)
            # The CAS targets are durable before the first output mutation.
            write_manifest()
            if pre_commit_guard is not None:
                pre_commit_guard()
            _assert_inputs_unchanged(root, expected_inputs)
            pinned_workspace.verify_namespace()
            for path in sorted(writes_by_path, key=lambda item: item.as_posix()):
                pinned_directory = pin_for(path)
                # All Jaunt publishers honor the surrounding global lease. Keep
                # the byte CAS immediately adjacent to replacement as an extra
                # guard against non-cooperating filesystem edits; no portable
                # conditional-replace syscall exists across Windows and POSIX.
                if _repair_path_hash(pinned_directory, path) != original_hashes[path]:
                    raise JauntGenerationError(
                        f"TypeScript artifact changed during validation: {path.relative_to(root)}"
                    )
                write = writes_by_path[path]
                if write.content is None:
                    pinned_directory.unlink(path.name, missing_ok=True)
                else:
                    pinned_directory.replace(staged[path], path.name)
                pinned_directory.fsync_required()
            unconverged = [
                path
                for path in writes_by_path
                if _repair_path_hash(pin_for(path), path) != expected_after[path]
            ]
            if unconverged:
                raise JauntGenerationError(
                    "TypeScript test-repair transaction did not converge: "
                    + ", ".join(path.relative_to(root).as_posix() for path in sorted(unconverged))
                )
            pinned_workspace.verify_namespace()
            if commit_seal is not None:
                commit_seal()
            pinned_workspace.verify_namespace()
        finally:
            active_error = sys.exception()
            cleanup_error: OSError | None = None
            for path, temporary in staged.items():
                try:
                    pin_for(path).unlink(temporary, missing_ok=True)
                except OSError as error:
                    if cleanup_error is None:
                        cleanup_error = error
            if cleanup_error is not None:
                if active_error is None:
                    raise cleanup_error
                active_error.add_note(f"TypeScript repair cleanup also failed: {cleanup_error}")

    def commit() -> None:
        nonlocal committed
        changed = [
            path.relative_to(root).as_posix()
            for path, after in expected_after.items()
            if _repair_path_hash(pin_for(path), path) != after
        ]
        if changed:
            raise JauntGenerationError(
                "TypeScript test-repair outputs changed before outer commit: "
                + ", ".join(sorted(changed))
            )
        pinned_workspace.verify_namespace()
        if not _retire_transaction_manifest(
            manifest,
            payload,
            pinned_directory=transaction_pin(),
        ):
            raise JauntConfigError(
                f"Could not durably retire TypeScript test-repair marker: {manifest}"
            )
        committed = True

    with pinned_workspace:
        transaction_directory = pinned_workspace.directory(directory)
        lease = _acquire_transaction_lease(
            directory,
            blocking=True,
            pinned_directory=transaction_directory,
            authority_directory=pinned_workspace.root_directory,
        )
        if lease is None:  # pragma: no cover - blocking acquisition
            raise JauntConfigError("Could not acquire the TypeScript test-repair transaction lease")
        try:
            pending_manifests = transaction_directory.iter_names("*.json")
            if pending_manifests:
                raise JauntGenerationError(
                    "An unresolved TypeScript artifact transaction blocks test repair: "
                    + ", ".join(pending_manifests)
                )
            add_paths(paths)
            yield _RepairFileTransaction(
                commit=commit,
                publish=publish,
            )
        finally:
            try:
                if not committed:
                    pinned_workspace.verify_namespace()
                    for path, after in expected_after.items():
                        pinned_directory = pin_for(path)
                        # A mismatch is a newer owner, not a failed rollback. Leave
                        # those bytes intact and retire this superseded marker once
                        # every still-owned path has been restored.
                        if _repair_path_hash(pinned_directory, path) != after:
                            continue
                        content, mode = originals[path]
                        _restore_repair_snapshot(pinned_directory, path, content, mode)
                    if transaction_pin().stat(
                        manifest.name
                    ) is not None and not _retire_transaction_manifest(
                        manifest,
                        payload,
                        pinned_directory=transaction_pin(),
                    ):
                        raise JauntConfigError(
                            f"Could not durably retire TypeScript test-repair marker: {manifest}"
                        )
            finally:
                lease.release()


@contextmanager
def _isolated_test_workspace(
    root: Path,
    files: Sequence[str],
    overlays: Mapping[str, str],
    *,
    tier: str,
    deleted_files: Sequence[str] = (),
    materialize_external_links: bool = False,
) -> Iterator[Path]:
    """Copy one test tier without leaving links back into the source workspace.

    Both implementation repair and protected Vitest execution use this view.  A
    symlink to the original ``node_modules`` is not isolation: resolving
    ``node_modules/../tests`` follows the physical parent and reaches held-out
    batteries.  Internal links are therefore remapped into the copy and external
    package links can be materialized for mutation runs, but a workspace-wide
    external ``node_modules`` link is rejected rather than copying an unbounded
    package store. Generated batteries are staged only from the exact selected
    bytes after the ordinary tree has been copied.
    """

    if tier not in {"example", "derived"}:
        raise ValueError(f"unsupported isolated test tier: {tier}")

    root = root.resolve()
    with tempfile.TemporaryDirectory(prefix="jaunt-ts-test-isolated-") as raw_temporary:
        temporary = Path(raw_temporary) / "workspace"
        temporary.mkdir()
        generated_battery_suffixes = (
            ".example.test.ts",
            ".example.test.tsx",
            ".derived.test.ts",
            ".derived.test.tsx",
            ".contract.test.ts",
            ".contract.test.tsx",
        )
        skipped_directories = {
            ".git",
            ".jaunt",
            ".nyc_output",
            ".vite",
            ".vitest",
            ".venv",
            "__snapshots__",
            "coverage",
            "venv",
        }

        def source_is_generated_battery(path: Path) -> bool:
            return path.name.endswith(generated_battery_suffixes)

        def source_is_snapshot(path: Path) -> bool:
            return path.suffix == ".snap"

        def root_relative(path: Path) -> Path | None:
            try:
                return path.resolve(strict=True).relative_to(root)
            except (OSError, ValueError):
                return None

        def external_package_store(path: Path) -> Path | None:
            for candidate in (path, *path.parents):
                if candidate.name == "node_modules":
                    return candidate
            return None

        def assert_external_store_safe(store: Path) -> None:
            if store == root or store in root.parents or root in store.parents:
                raise JauntConfigError(
                    "External TypeScript package store overlaps the source workspace"
                )
            for candidate in store.rglob("*"):
                if not candidate.is_symlink():
                    continue
                try:
                    physical = candidate.resolve(strict=True)
                except OSError as exc:
                    raise JauntConfigError(
                        f"External TypeScript package store has an invalid link: {candidate}"
                    ) from exc
                if physical == root or root in physical.parents or physical in root.parents:
                    raise JauntConfigError(
                        "External TypeScript package store links into the source workspace: "
                        f"{candidate}"
                    )

        def resolved_symlink_target(source: Path, external_root: Path | None) -> Path | None:
            try:
                physical = source.resolve(strict=True)
            except OSError as exc:
                if external_root is not None:
                    raise JauntConfigError(
                        "Mutation testing cannot safely materialize an invalid nested "
                        f"external package link: {source}"
                    ) from exc
                return None
            if external_root is not None:
                try:
                    package_root = external_root.resolve(strict=True)
                except OSError as exc:
                    raise JauntConfigError(
                        "Mutation testing cannot safely materialize an invalid "
                        f"external package root: {external_root}"
                    ) from exc
                if physical != package_root and package_root not in physical.parents:
                    raise JauntConfigError(
                        "Mutation testing cannot safely materialize a nested link "
                        f"outside its external package: {source}"
                    )
            return physical

        def copy_symlink(
            source: Path,
            destination: Path,
            active: frozenset[Path],
            external_root: Path | None,
        ) -> None:
            physical = resolved_symlink_target(source, external_root)
            if physical is None:
                return
            if source_is_generated_battery(physical) or source_is_snapshot(physical):
                return
            internal = root_relative(physical)
            if internal is not None:
                mapped = temporary / internal
                # A top-level node_modules link (or an equivalent self-map) must
                # be materialized; otherwise it becomes a loop in the copy.
                if mapped != destination:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(
                        os.path.relpath(mapped, destination.parent),
                        target_is_directory=physical.is_dir(),
                    )
                    return
            elif materialize_external_links and source == root / "node_modules":
                # A whole store can contain unrelated packages and be arbitrarily
                # large. Mutation isolation only supports bounded package links.
                raise JauntConfigError(
                    "Mutation testing cannot safely materialize a workspace-wide "
                    f"node_modules link to an external package store: {source}"
                )
            elif not materialize_external_links:
                store = external_package_store(physical)
                if store is not None:
                    assert_external_store_safe(store)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(physical, target_is_directory=physical.is_dir())
                    return
            copy_entry(
                physical,
                destination,
                active,
                external_root=external_root or physical,
            )

        def copy_entry(
            source: Path,
            destination: Path,
            active: frozenset[Path],
            *,
            external_root: Path | None = None,
        ) -> None:
            # Validate every link in a materialized package before applying name-
            # based filters. A link called `.git` or `node_modules` must not hide
            # an escape merely because its destination would be skipped.
            if source.is_symlink() and external_root is not None:
                resolved_symlink_target(source, external_root)
            if source.name in skipped_directories:
                return
            # A linked package's private install can dwarf the package itself and
            # is not part of the project-local dependency boundary. Dependencies
            # resolve from the copied workspace's own node_modules; if one is
            # missing, the protected runner fails closed instead of importing the
            # linked package's unrelated development tree.
            if (
                external_root is not None
                and source != external_root
                and source.name == "node_modules"
            ):
                return
            if source_is_generated_battery(source) or source_is_snapshot(source):
                return
            if source.is_symlink():
                copy_symlink(source, destination, active, external_root)
                return
            if source.is_dir():
                try:
                    physical = source.resolve(strict=True)
                except OSError:
                    return
                if physical in active:
                    return
                destination.mkdir(parents=True, exist_ok=True)
                nested_active = active | {physical}
                for entry in source.iterdir():
                    copy_entry(
                        entry,
                        destination / entry.name,
                        nested_active,
                        external_root=external_root,
                    )
                shutil.copystat(source, destination, follow_symlinks=False)
                return
            if source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)

        for entry in root.iterdir():
            copy_entry(entry, temporary / entry.name, frozenset({root}))
        # Vite otherwise walks above the disposable root looking for workspace
        # markers, which is both unnecessary and incompatible with the
        # cross-platform Node permission fallback.
        if not any((temporary / name).is_file() for name in ("pnpm-workspace.yaml", "lerna.json")):
            (temporary / "pnpm-workspace.yaml").write_text("packages: []\n", encoding="utf-8")

        # Negative config-resolution inputs are as important as captured files:
        # a concurrently created extension/index candidate must not enter the
        # disposable run and then disappear before the publication seal.
        for relative in sorted(set(deleted_files), key=lambda item: (-item.count("/"), item)):
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise JauntConfigError(f"Unsafe deleted TypeScript test input: {relative!r}")
            target = temporary / path
            if target.is_symlink() or target.is_file():
                target.unlink(missing_ok=True)
            elif target.is_dir():
                shutil.rmtree(target)

        # Non-test overlays (notably an implementation repair candidate) belong
        # to both tier views.  Battery overlays are admitted only via the exact
        # selected-file loop below.
        for relative, source in overlays.items():
            if Path(relative).name.endswith(generated_battery_suffixes):
                continue
            target = _safe_path(temporary, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8", newline="")

        for relative in sorted(set(files)):
            source = overlays.get(relative)
            if source is None:
                original = _safe_path(root, relative)
                if original.is_file():
                    source = original.read_text(encoding="utf-8")
            if source is None:
                continue
            is_example = _is_reviewable_example_battery(relative, source)
            if (tier == "example") != is_example:
                continue
            target = _safe_path(temporary, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8", newline="")

        # No link in the isolated tree may physically resolve into the source
        # workspace.  This catches package-manager and hand-authored traversal
        # links before either Vitest or an implementation model starts.
        for candidate in temporary.rglob("*"):
            if not candidate.is_symlink():
                continue
            try:
                physical = candidate.resolve(strict=True)
            except OSError as exc:
                raise JauntConfigError(
                    f"Isolated TypeScript test workspace has an invalid link: {candidate}"
                ) from exc
            try:
                physical.relative_to(root)
            except ValueError:
                continue
            raise JauntConfigError(
                "Isolated TypeScript test workspace retained a link into the source workspace: "
                f"{candidate}"
            )
        yield temporary


@contextmanager
def _isolated_test_repair_workspace(
    root: Path,
    files: Sequence[str],
    overlays: Mapping[str, str],
    *,
    deleted_files: Sequence[str] = (),
) -> Iterator[Path]:
    """Copy a model-safe workspace with examples visible and held-out tests absent."""

    with _isolated_test_workspace(
        root,
        files,
        overlays,
        tier="example",
        deleted_files=deleted_files,
    ) as temporary:
        yield temporary


def _repair_module_ids(
    result: Mapping[str, Any],
    *,
    targets_by_file: Mapping[str, Sequence[str]],
    modules: Mapping[str, Mapping[str, Any]],
    requested_targets: Sequence[str],
) -> tuple[str, ...]:
    """Resolve failed generated tests to implementation modules, with safe fallbacks."""

    normalized_targets = {
        path.replace("\\", "/").removeprefix("./"): tuple(values)
        for path, values in targets_by_file.items()
    }
    selected: set[str] = set()
    tests = result.get("tests", [])
    if isinstance(tests, list):
        for item in tests:
            if not isinstance(item, Mapping) or item.get("status") != "failed":
                continue
            path = item.get("file")
            if isinstance(path, str):
                selected.update(
                    normalized_targets.get(path.replace("\\", "/").removeprefix("./"), ())
                )
    if not selected:
        selected.update(
            target.split("#", 1)[0]
            for target in requested_targets
            if target.split("#", 1)[0] in modules
        )
    if not selected:
        selected.update(module_id for values in normalized_targets.values() for module_id in values)
    if not selected:
        selected.update(modules)
    return tuple(sorted(selected))


def _runner_failure_categories(result: Mapping[str, Any]) -> frozenset[str]:
    categories: set[str] = set()
    for key in ("failures", "tests"):
        records = result.get(key, [])
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, Mapping):
                continue
            category = record.get("category")
            if isinstance(category, str) and category in _RUNNER_CATEGORIES:
                categories.add(category)
    if result.get("timedOut") is True:
        categories.add("timeout")
    return frozenset(categories)


def _runner_allows_implementation_repair(result: Mapping[str, Any]) -> bool:
    """Repair only failures that can reasonably originate in implementation behavior."""

    categories = _runner_failure_categories(result)
    return bool(categories) and categories.issubset(_IMPLEMENTATION_REPAIR_CATEGORIES)


def _runner_candidate_rejection_paths(
    result: Mapping[str, Any],
    candidate_paths: Sequence[str],
) -> tuple[str, ...]:
    """Identify failures attributable to generated battery source.

    Behavioral failures retain the historical all-candidates fallback because a
    protected derived result may intentionally omit its path. Collection failures
    are rejectable only when the runner attributes them to an exact candidate;
    global collection, protocol, timeout, and runner failures remain infrastructure.
    """

    candidates = {path.replace("\\", "/").removeprefix("./") for path in candidate_paths}
    categories = _runner_failure_categories(result)
    if not categories or not categories.issubset(_CANDIDATE_REJECTION_CATEGORIES):
        return ()
    attributed = candidates.intersection(
        _failed_runner_test_paths(result, candidate_paths=tuple(candidates))
    )
    if "collection" in categories:
        return tuple(sorted(attributed))
    return tuple(sorted(attributed or candidates))


def _typecheck_failure_is_infrastructure(result: Mapping[str, Any]) -> bool:
    """Distinguish compiler incompatibility from an unavailable protected runner."""

    if bool(result.get("ok", False)):
        return False
    categories = _runner_failure_categories(result)
    if categories:
        return not _runner_allows_implementation_repair(result)
    raw_diagnostics = result.get("diagnostics", [])
    source_mismatch = isinstance(raw_diagnostics, list) and any(
        isinstance(item, Mapping)
        and isinstance(item.get("code"), str)
        and (
            re.fullmatch(r"TS\d+", str(item["code"])) is not None
            or str(item["code"]).startswith("JAUNT_TS_TEST_")
            or str(item["code"])
            in {
                "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
                "JAUNT_TS_UNDECLARED_PACKAGE",
            }
        )
        for item in raw_diagnostics
    )
    return not source_mismatch


def _failed_runner_test_paths(
    result: Mapping[str, Any],
    *,
    candidate_paths: Sequence[str] = (),
) -> tuple[str, ...]:
    """Collect failed paths, including opaque candidate-owned collection failures."""

    paths: set[str] = set()
    candidate_by_collection_id = {
        hashlib.sha256(path.replace("\\", "/").removeprefix("./").encode("utf-8")).hexdigest()[
            :16
        ]: path.replace("\\", "/").removeprefix("./")
        for path in candidate_paths
    }

    def visit(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        record = cast("Mapping[str, object]", value)
        tests = record.get("tests", ())
        if isinstance(tests, list):
            for item in tests:
                if not isinstance(item, Mapping):
                    continue
                test_record = cast("Mapping[str, object]", item)
                path = test_record.get("file")
                if test_record.get("status") == "failed" and isinstance(path, str) and path:
                    paths.add(path.replace("\\", "/").removeprefix("./"))
                    continue
                case_id = test_record.get("caseId")
                if test_record.get("category") == "collection" and isinstance(case_id, str):
                    candidate = candidate_by_collection_id.get(case_id)
                    if candidate is not None:
                        paths.add(candidate)
        batches = record.get("batches", {})
        if isinstance(batches, Mapping):
            for batch in batches.values():
                visit(batch)

    visit(result)
    return tuple(sorted(paths))


def _cost_summary(*summaries: Mapping[str, Any]) -> dict[str, int | float]:
    merged: dict[str, int | float] = {}
    for summary in summaries:
        for key, value in summary.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            merged[key] = merged.get(key, 0) + value
    if "estimated_cost_usd" in merged:
        merged["estimated_cost_usd"] = round(float(merged["estimated_cost_usd"]), 6)
    return merged


class _ForwardingPhaseCostTracker(CostTracker):
    """Keep phase-local summaries while charging a caller-owned aggregate tracker."""

    def __init__(self, parent: CostTracker) -> None:
        super().__init__(max_cost=None)
        self._parent = parent

    def record(self, module_name: str, usage: TokenUsage) -> None:
        super().record(module_name, usage)
        self._parent.record(module_name, usage)

    def record_cache_hit(self) -> None:
        super().record_cache_hit()
        self._parent.record_cache_hit()

    def check_budget(self) -> None:
        self._parent.check_budget()


def _build_phase_metadata(report: TargetBuildReport) -> dict[str, Any]:
    return {
        **dict(report.metadata),
        "generated": sorted(report.generated),
        "skipped": sorted(report.skipped),
        "refrozen": sorted(report.refrozen),
        "failed": sorted(report.failed),
        "exit_code": report.exit_code,
    }


@lru_cache(maxsize=8)
def _node_permission_flag(node: str) -> str:
    """Return the permission-model switch supported by this Node executable."""

    try:
        result = subprocess.run(
            [node, "--help"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env=worker_environment(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise JauntConfigError(
            "Could not inspect Node's filesystem permission support for protected tests"
        ) from exc
    help_text = f"{result.stdout}\n{result.stderr}"
    if "--permission" in help_text:
        return "--permission"
    if "--experimental-permission" in help_text:
        return "--experimental-permission"
    raise JauntConfigError("Protected TypeScript test isolation requires a Node permission model")


def _bubblewrap_executable(environment: Mapping[str, str]) -> str | None:
    if not sys.platform.startswith("linux"):
        return None
    return shutil.which("bwrap", path=environment.get("PATH", ""))


async def _run_test_runner(
    client: Any,
    root: Path,
    config: JauntConfig,
    *,
    files: Sequence[str],
    overlays: Mapping[str, str] | None = None,
    root_overlay_paths: Sequence[str] | None = None,
    redact_derived: bool = True,
    typecheck_only: bool = False,
    declaration_emit: bool = False,
    normal_emit: bool = False,
    deleted_files: Sequence[str] = (),
    package_root: str | None = None,
    tsconfig_path: str | None = None,
    project_config_paths: Sequence[str] = (),
    tier: str | None = None,
    isolated_from: Path | None = None,
    timeout: float = _DEFAULT_RUNNER_TIMEOUT,
) -> Mapping[str, Any]:
    target = _target(config)
    if target.vitest_args:
        raise JauntConfigError(
            "target.ts.vitest_args is not supported by the protected Vitest runner"
        )
    if tier is not None and tier not in {"example", "derived"}:
        raise ValueError(f"unsupported protected test tier: {tier}")
    runner = _runner_path(client)
    installation = client.installation
    compiler_module_path = Path(installation.compiler_module_path)

    def isolated_path(path: Path, *, label: str) -> Path:
        if isolated_from is None:
            return path
        source = isolated_from.resolve()
        lexical = Path(os.path.abspath(path))
        try:
            relative = lexical.relative_to(source)
        except ValueError:
            return path
        mapped = root / relative
        if not mapped.is_file():
            raise JauntConfigError(
                f"Isolated TypeScript test workspace is missing {label}: {relative.as_posix()}"
            )
        return mapped

    runner = isolated_path(runner, label="the protected test runner")
    compiler_module_path = isolated_path(
        compiler_module_path,
        label="the TypeScript compiler",
    )
    runner_root = root
    if isolated_from is not None:
        # Resolve the disposable view before crossing the permission boundary.
        # Otherwise Node's realpath checks must inspect an alias ancestor such
        # as macOS /var, and granting that existing directory would implicitly
        # wildcard every sibling temporary workspace.
        lexical_root = Path(os.path.abspath(root))
        runner_root = root.resolve()
        source_lexical = Path(os.path.abspath(isolated_from))
        source_physical = isolated_from.resolve()

        def sandbox_path(path: Path) -> Path:
            lexical = Path(os.path.abspath(path))
            for base in (lexical_root, source_lexical):
                with contextlib.suppress(ValueError):
                    return runner_root / lexical.relative_to(base)
            physical = path.resolve()
            with contextlib.suppress(ValueError):
                return runner_root / physical.relative_to(source_physical)
            return physical

        # Keep package-manager symlinks lexical beneath the physical workspace
        # prefix. The runner validates that compilerModulePath is inside root;
        # separate read grants cover external physical node_modules targets.
        runner = sandbox_path(runner)
        compiler_module_path = sandbox_path(compiler_module_path)
    payload = {
        "root": str(runner_root),
        "files": list(files),
        "overlays": dict(overlays or {}),
        "timeoutMs": int(timeout * 1000),
        # Typecheck mode executes no test code and carries no assertion output.
        # Request the compiler/policy messages, then apply the bounded Python
        # projection below so generation retries receive actionable feedback.
        "redactDerived": False if typecheck_only else redact_derived,
        "mode": "typecheck" if typecheck_only else "run",
        "declarationEmit": declaration_emit,
        "normalEmit": normal_emit,
        "compilerModulePath": str(compiler_module_path),
        "generatedDir": target.generated_dir,
    }
    if root_overlay_paths is not None:
        payload["rootOverlayPaths"] = list(dict.fromkeys(root_overlay_paths))
    if tier is not None:
        payload["tier"] = tier
    if deleted_files:
        payload["deletedFiles"] = list(dict.fromkeys(deleted_files))
    if package_root is not None:
        package_path = Path(package_root)
        payload["packageRoot"] = (
            str(sandbox_path(package_path))
            if isolated_from is not None and package_path.is_absolute()
            else package_root
        )
    if project_config_paths:
        payload["projectConfigPaths"] = list(dict.fromkeys(project_config_paths))
    if typecheck_only:
        if tsconfig_path is None:
            configured = target.test_projects or target.projects
            resolved: list[str] = []
            for entry in configured:
                if any(character in entry for character in "*?["):
                    resolved.extend(
                        path.relative_to(root).as_posix()
                        for path in sorted(root.glob(entry))
                        if path.is_file()
                    )
                elif _safe_path(root, entry).is_file():
                    resolved.append(entry)
            if len(resolved) > 1:
                raise JauntConfigError(
                    "TypeScript test files span multiple configured projects; "
                    "run them in an owner-scoped batch"
                )
            tsconfig_path = resolved[0] if resolved else None
    if tsconfig_path is not None:
        payload["tsconfigPath"] = tsconfig_path
    if not typecheck_only and target.vitest_config:
        payload["vitestConfigPath"] = target.vitest_config
    kwargs: dict[str, Any] = {}
    if os.name == "posix":
        kwargs["start_new_session"] = True
    command = [installation.node]
    environment = worker_environment()
    if isolated_from is not None:
        bubblewrap = _bubblewrap_executable(environment)
        if bubblewrap is not None:
            # Overlay the held-out-free copy at the source workspace's absolute
            # path as well as its temporary path.  Even code that guesses or
            # retained the original cwd can only see the isolated bytes.
            command = [
                bubblewrap,
                "--die-with-parent",
                "--new-session",
                "--unshare-pid",
                "--unshare-net",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--dev-bind",
                "/dev",
                "/dev",
                "--bind",
                str(runner_root),
                str(runner_root),
                "--bind",
                str(runner_root),
                str(isolated_from.resolve()),
                "--chdir",
                str(runner_root),
                installation.node,
            ]
        else:
            payload["permissionSandbox"] = True
            permission_flag = _node_permission_flag(installation.node)
            permission_guard = runner.parent / "permission_guard.cjs"
            if not permission_guard.is_file():
                raise JauntConfigError(
                    "Installed @usejaunt/ts is missing its protected worker permission guard"
                )
            readable = {runner_root, runner.parent, compiler_module_path.parent}
            package_root_path = getattr(installation, "package_root", None)
            if isinstance(package_root_path, Path):
                mapped_package = package_root_path.resolve()
                source = isolated_from.resolve()
                with contextlib.suppress(ValueError):
                    mapped_package = runner_root / mapped_package.relative_to(source)
                if mapped_package.exists():
                    readable.add(mapped_package)
            for candidate in root.rglob("*"):
                if not candidate.is_symlink():
                    continue
                physical = candidate.resolve(strict=True)
                try:
                    physical.relative_to(runner_root)
                except ValueError:
                    for parent in (physical, *physical.parents):
                        if parent.name == "node_modules":
                            readable.add(parent)
                            break
            # Node 20's experimental permission model mishandles overlapping
            # allow-fs-read entries: granting both a directory and one of its
            # descendants can deny enumeration of the parent.  Ancestor grants
            # already cover lexical descendants; separately resolved external
            # package stores remain independent roots in this set.
            minimal_readable = tuple(
                sorted(
                    path
                    for path in readable
                    if not any(
                        path != ancestor and path.is_relative_to(ancestor) for ancestor in readable
                    )
                )
            )
            command.extend(
                [
                    permission_flag,
                    "--allow-addons",
                    "--allow-worker",
                    f"--require={permission_guard}",
                    *(f"--allow-fs-read={path}" for path in minimal_readable),
                    f"--allow-fs-write={runner_root}",
                ]
            )
        source_text = str(isolated_from.resolve())
        for key, value in tuple(environment.items()):
            if key != "PATH" and source_text in value:
                environment.pop(key, None)
        environment["PATH"] = os.pathsep.join(
            entry
            for entry in environment.get("PATH", "").split(os.pathsep)
            if entry and source_text not in os.path.abspath(entry)
        )
        environment["PWD"] = str(runner_root)
    command.append(str(runner))
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=str(runner_root),
        env=environment,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        **kwargs,
    )

    def protocol_failure() -> Mapping[str, Any]:
        failure: dict[str, Any] = {
            "ok": False,
            "mode": "typecheck" if typecheck_only else "run",
            "failures": [{"category": "runner-protocol"}],
            "diagnostics": [
                {
                    "code": "JAUNT_TS_RUNNER_PROTOCOL",
                    "severity": "error",
                    "message": "The protected test runner returned an invalid response.",
                }
            ],
            "tests": [],
            "captured": {"stdout": "", "stderr": ""},
        }
        if process.returncode is not None:
            failure["exitCode"] = process.returncode
        if not redact_derived:
            failure["captured"] = {
                "stdout": stdout.decode("utf-8", errors="replace")[-4000:],
                "stderr": stderr.decode("utf-8", errors="replace")[-4000:],
            }
        return _redact_runner_result(failure, enabled=redact_derived)

    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(json.dumps(payload, sort_keys=True).encode("utf-8")),
            timeout=timeout,
        )
    except TimeoutError:
        await _terminate_runner_process(process)
        return {
            "ok": False,
            "mode": "typecheck" if typecheck_only else "run",
            "timedOut": True,
            "failures": [{"category": "timeout"}],
        }
    except asyncio.CancelledError:
        await asyncio.shield(_terminate_runner_process(process))
        raise
    try:
        result = json.loads(stdout.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        return protocol_failure()
    expected_mode = "typecheck" if typecheck_only else "run"
    if (
        not isinstance(result, Mapping)
        or not _valid_runner_dto(
            result,
            expected_mode=expected_mode,
            redact_derived=redact_derived,
        )
        or (process.returncode != 0 and result.get("ok") is True)
    ):
        return protocol_failure()
    copy = dict(result)
    if process.returncode:
        copy["exitCode"] = process.returncode
    return _redact_runner_result(copy, enabled=redact_derived)


async def _terminate_runner_process(process: Any, *, platform: str | None = None) -> None:
    """Terminate the runner and its descendants on every supported platform."""

    if process.returncode is not None:
        return
    effective = os.name if platform is None else platform
    if effective == "posix":
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    elif effective == "nt":  # pragma: no cover - exercised with a platform-isolated fake
        try:
            taskkill = await asyncio.create_subprocess_exec(
                "taskkill",
                "/PID",
                str(process.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(taskkill.wait(), timeout=5.0)
        except (FileNotFoundError, OSError, TimeoutError):
            process.kill()
    else:  # pragma: no cover - defensive fallback for non-POSIX runtimes
        process.kill()
    try:
        await asyncio.wait_for(process.wait(), timeout=5.0)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()


async def _run_test_batches(
    client: Any,
    root: Path,
    config: JauntConfig,
    workspace: Mapping[str, Any],
    *,
    files: Sequence[str],
    explicit_owners: Mapping[str, str] | None = None,
    overlays: Mapping[str, str] | None = None,
    redact_derived: bool = True,
    typecheck_only: bool = False,
    config_snapshot: tuple[Mapping[str, str], Mapping[str, str]] | None = None,
) -> Mapping[str, Any]:
    effective_overlays = dict(overlays or {})
    validation_overlay_roots = set(effective_overlays)
    deleted_files: tuple[str, ...] = ()
    if config_snapshot is not None:
        config_closure, config_overlays = config_snapshot
        for path, source in config_overlays.items():
            previous = effective_overlays.setdefault(path, source)
            if previous != source:
                raise JauntGenerationError(
                    "TypeScript validation overlays conflict with captured Vitest input: " + path
                )
        validation_overlay_roots.difference_update(config_overlays)
        deleted_files = tuple(
            sorted(
                path
                for path, digest in config_closure.items()
                if digest == MISSING_INPUT and path not in effective_overlays
            )
        )
        _pin_vitest_config_dependency_runtimes(client, root, config_overlays)
    grouped = _group_test_files(
        root,
        config,
        workspace,
        files,
        explicit_owners=explicit_owners,
    )
    _validate_test_owner_dependencies(
        root,
        workspace,
        grouped,
        overlays=effective_overlays,
    )
    _pin_test_dependency_runtimes(client, root, workspace, grouped)
    selected_files = set(files)
    test_overlays = {
        path: source for path, source in effective_overlays.items() if path in selected_files
    }
    shared_overlays = {
        path: source for path, source in effective_overlays.items() if path not in test_overlays
    }
    project_config_paths = _workspace_project_config_paths(workspace)
    results: dict[str, Mapping[str, Any]] = {}
    for project, project_files in grouped.items():
        batch_overlays = {
            **shared_overlays,
            **{path: test_overlays[path] for path in project_files if path in test_overlays},
        }
        if typecheck_only:
            results[project] = await _run_test_runner(
                client,
                root,
                config,
                files=project_files,
                overlays=batch_overlays,
                root_overlay_paths=tuple(
                    path for path in batch_overlays if path in validation_overlay_roots
                ),
                redact_derived=redact_derived,
                typecheck_only=True,
                deleted_files=deleted_files,
                tsconfig_path=project,
                project_config_paths=project_config_paths,
            )
            continue

        tier_files: dict[str, list[str]] = {"example": [], "derived": []}
        for path in project_files:
            source = test_overlays.get(path)
            if source is None:
                source = _safe_path(root, path).read_text(encoding="utf-8")
            tier_files[
                "example" if _is_reviewable_example_battery(path, source) else "derived"
            ].append(path)
        tier_results: dict[str, Mapping[str, Any]] = {}
        for tier in ("example", "derived"):
            selected = tuple(tier_files[tier])
            if not selected:
                continue
            with _isolated_test_workspace(
                root,
                project_files,
                batch_overlays,
                tier=tier,
                deleted_files=deleted_files,
            ) as isolated_root:
                tier_results[tier] = await _run_test_runner(
                    client,
                    isolated_root,
                    config,
                    files=selected,
                    # Every selected byte and every shared implementation
                    # overlay is already staged in the disposable view.  Do not
                    # put the other tier's source into the child payload.
                    overlays={},
                    redact_derived=redact_derived,
                    tier=tier,
                    isolated_from=root,
                    tsconfig_path=project,
                    project_config_paths=project_config_paths,
                )
        results[project] = _aggregate_runner_batches(tier_results, mode="run")
    return _aggregate_runner_batches(
        results,
        mode="typecheck" if typecheck_only else "run",
    )


async def _validate_committed_target_batteries(
    client: Any,
    initialized: Any,
    root: Path,
    config: JauntConfig,
    analysis: Any,
    *,
    module_ids: Sequence[str],
    artifact_overlays: Mapping[str, str],
    proof_sink: dict[str, Any] | None = None,
) -> list[str]:
    """Reject a build candidate that breaks a still-compatible committed battery."""

    from jaunt.typescript.upgrade import compatible_semantic_modules

    if proof_sink is not None:
        proof_sink.clear()
    requested_ids = set(module_ids)
    modules = {_module_id(module): module for module in analysis.modules}
    raw_specs = analysis.workspace.get("testSpecs", [])
    missing_relevant_targets: set[str] = set()
    if isinstance(raw_specs, list):
        for spec in raw_specs:
            if not isinstance(spec, Mapping):
                continue
            raw_targets = spec.get("targets", [])
            if not isinstance(raw_targets, list):
                continue
            declared = {
                str(target).split("#", 1)[0]
                for target in raw_targets
                if isinstance(target, str) and target.startswith("ts:")
            }
            if requested_ids.intersection(declared):
                missing_relevant_targets.update(declared - set(modules))
    if missing_relevant_targets:
        # Scoped analysis intentionally omits independent modules. A committed
        # multi-target battery still needs their API records, so expand only
        # when one of those batteries is relevant to the candidate under test.
        # Fetch contracts directly: a full workspace analysis could reintroduce
        # unrelated diagnostics that targeted builds deliberately excluded.
        pending = set(missing_relevant_targets)
        while pending:
            requested_batch = set(pending)
            response = await client.request(
                "analyzeContracts",
                {"moduleIds": sorted(requested_batch)},
            )
            raw_modules = response.get("modules", [])
            received = (
                [module for module in raw_modules if isinstance(module, Mapping)]
                if isinstance(raw_modules, list)
                else []
            )
            if not received:
                break
            received_ids = {_module_id(module) for module in received}
            pending.difference_update(received_ids)
            for module in received:
                module_id = _module_id(module)
                modules[module_id] = module
                dependencies = module.get("dependencies", [])
                if isinstance(dependencies, list):
                    pending.update(
                        str(dependency).split("#", 1)[0]
                        for dependency in dependencies
                        if isinstance(dependency, str)
                        and str(dependency).split("#", 1)[0] not in modules
                    )
            if not received_ids.intersection(requested_batch):
                break
        unresolved = missing_relevant_targets - set(modules)
        if pending or unresolved:
            unavailable = sorted(pending | unresolved)
            raise _CommittedBatteryInfrastructureError(
                (
                    "Committed multi-target battery validation could not load target "
                    f"contract(s): {', '.join(unavailable)}; the implementation was not retried.",
                )
            )
        analysis = replace(
            analysis,
            contracts={
                **analysis.contracts,
                "modules": [modules[module_id] for module_id in sorted(modules)],
            },
        )

    compatible = compatible_semantic_modules(
        root,
        tuple(analysis.modules),
        allow_environment_drift=True,
    )
    requested = tuple(module_id for module_id in module_ids if module_id in compatible)
    if not requested:
        return []
    specs = _selected_test_specs(
        root,
        config,
        analysis.workspace,
        modules,
        target_ids=requested,
    )
    if not specs:
        return []
    pinned_runner_fingerprint = _runner_fingerprint(root, client, initialized)
    _pin_test_runtime_identity(client)
    target = _target(config)
    config_closure, config_overlays = (
        _local_config_snapshot(root, target.vitest_config, client=client)
        if target.vitest_config
        else ({}, {})
    )
    owners = dict(_workspace_test_file_owners(root, config, analysis.workspace))
    files: list[str] = []
    battery_overlays: dict[str, str] = {}
    battery_preconditions: dict[str, str] = {}
    for spec in specs:
        selected = _selected_test_modules(spec, modules)
        selected_ids = {_module_id(module) for module in selected}
        if not selected_ids or not selected_ids.issubset(compatible):
            continue
        source_path = str(spec.get("path", ""))
        for tier in ("example", "derived"):
            relative = _test_output(source_path, _target(config).generated_dir, tier)
            path = _safe_path(root, relative)
            try:
                source_bytes = path.read_bytes()
                source = source_bytes.decode("utf-8")
            except (OSError, UnicodeError):
                continue
            metadata = _test_header_metadata(source)
            body = _strip_test_header(source)
            if (
                metadata is None
                or metadata.get("tier") != tier
                or metadata.get("source") != source_path
                or metadata.get("body_digest") != _sha256(body.encode("utf-8"))
                or _static_test_validation(
                    body,
                    generated_dirs=(_target(config).generated_dir,),
                )
            ):
                continue
            expected = _test_provenance(
                root,
                config,
                spec,
                modules,
                client,
                initialized,
                tier=tier,
                workspace=analysis.workspace,
            )
            # API and reheader-safe runner/Vitest drift may co-move with an
            # implementation candidate: execute the unchanged committed body
            # under the current pinned runtime. Every content-bearing battery
            # input must still match exactly before this gate is trusted.
            stable_fields = set(_TEST_PROVENANCE_FIELDS) - {
                "target_api_digest",
                "battery_fingerprint",
                "body_digest",
                *_TEST_REHEADER_FINGERPRINTS,
            }
            if any(metadata.get(field) != expected.get(field) for field in stable_fields):
                continue
            if relative not in owners:
                owners[relative] = _owner_project_for_source(
                    root,
                    config,
                    analysis.workspace,
                    relative,
                )
            files.append(relative)
            battery_overlays[relative] = source
            battery_preconditions[relative] = _sha256(source_bytes)
    selected_files = tuple(sorted(set(files)))
    if not selected_files:
        return []
    validation_overlays = dict(artifact_overlays)
    for relative, source in {**config_overlays, **battery_overlays}.items():
        previous = validation_overlays.setdefault(relative, source)
        if previous != source:
            raise _CommittedBatteryInfrastructureError(
                (
                    "Committed battery validation has conflicting captured bytes for "
                    f"{relative}; the implementation was not retried.",
                )
            )
    checked = await _run_test_batches(
        client,
        root,
        config,
        analysis.workspace,
        files=selected_files,
        explicit_owners=owners,
        overlays=validation_overlays,
        redact_derived=True,
        typecheck_only=True,
        config_snapshot=(config_closure, config_overlays),
    )
    if not bool(checked.get("ok", False)):
        if _typecheck_failure_is_infrastructure(checked):
            categories = _runner_failure_categories(checked)
            rendered_categories = ", ".join(sorted(categories)) or "unknown runner failure"
            raise _CommittedBatteryInfrastructureError(
                (
                    "Committed battery typecheck could not validate the candidate because "
                    f"the protected runner failed ({rendered_categories}); the implementation "
                    "was not retried. " + " ".join(_runner_validation_errors(checked)),
                )
            )
        return ["JAUNT_TS_COMMITTED_BATTERY: " + " ".join(_runner_validation_errors(checked))]
    ran = await _run_test_batches(
        client,
        root,
        config,
        analysis.workspace,
        files=selected_files,
        explicit_owners=owners,
        overlays=validation_overlays,
        redact_derived=True,
        config_snapshot=(config_closure, config_overlays),
    )
    if bool(ran.get("ok", False)):
        if proof_sink is not None:
            preconditions = dict(config_closure)
            for relative, digest in battery_preconditions.items():
                previous = preconditions.setdefault(relative, digest)
                if previous != digest:
                    raise _CommittedBatteryInfrastructureError(
                        (
                            "Committed battery validation has conflicting commit proof for "
                            f"{relative}; the implementation was not retried.",
                        )
                    )
            proof_sink.update(
                {
                    "preconditions": dict(sorted(preconditions.items())),
                    "vitest_config": target.vitest_config,
                    "config_closure": dict(config_closure),
                    "runner_fingerprint": pinned_runner_fingerprint,
                }
            )
        return []
    if not _runner_allows_implementation_repair(ran):
        categories = _runner_failure_categories(ran)
        rendered_categories = ", ".join(sorted(categories)) or "unknown runner failure"
        raise _CommittedBatteryInfrastructureError(
            (
                "Committed battery execution could not validate the candidate "
                f"because the protected runner failed ({rendered_categories}); "
                "the implementation was not retried.",
            )
        )
    return ["JAUNT_TS_COMMITTED_BATTERY: " + _implementation_repair_feedback(ran)]


def _runner_validation_errors(result: Mapping[str, Any]) -> list[str]:
    """Render protected-runner diagnostics as actionable generator feedback."""

    errors: list[str] = []
    raw_tests = result.get("tests", [])
    if isinstance(raw_tests, list):
        for item in raw_tests:
            startup_message = _runner_startup_failure(item)
            if startup_message is not None:
                errors.append(
                    "Vitest runner startup failed"
                    + (": " + startup_message if startup_message else " without an error message")
                )
    raw_diagnostics = result.get("diagnostics", [])
    if isinstance(raw_diagnostics, list):
        for item in raw_diagnostics:
            if not isinstance(item, Mapping):
                continue
            code = item.get("code")
            message = item.get("message")
            if not isinstance(code, str) or not isinstance(message, str):
                continue
            detail = f"{code}: {message}"
            path = item.get("path")
            if isinstance(path, str) and path:
                detail += f" ({path})"
            line = item.get("line")
            column = item.get("column")
            if isinstance(line, int) and not isinstance(line, bool):
                detail += f" at line {line}"
                if isinstance(column, int) and not isinstance(column, bool):
                    detail += f", column {column}"
            errors.append(detail)
    if errors:
        return list(dict.fromkeys(errors))
    if bool(result.get("timedOut", False)):
        return ["TypeScript test overlay validation timed out"]
    return ["TypeScript test overlay validation failed without a diagnostic"]


async def run_test(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    no_build: bool = False,
    no_run: bool = False,
    no_redact_derived: bool = False,
    force: bool = False,
    generator: GeneratorBackend | None = None,
    generator_factory: Callable[[], GeneratorBackend] | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    worker_factory: WorkerFactory | None = None,
    worker_session_override: tuple[Any, Any] | None = None,
    jobs: int | None = None,
    max_attempts: int = 2,
    build_instructions: Sequence[str] | None = None,
    semantic_gate_enabled: bool | None = None,
    semantic_gate_exec: Callable[..., Awaitable[Any]] | None = None,
    repo_map_enabled: bool | None = None,
    repo_map_block_override: str | None = None,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
) -> TargetTestReport:
    """Generate typed Vitest batteries and run them through the protected runner."""

    if generator is not None and generator_factory is not None:
        raise JauntConfigError("Pass either generator or generator_factory, not both")
    root = root.resolve()
    _recover_atomic_write_manifests(root)
    if response_cache is None:
        response_cache = ResponseCache(root / ".jaunt" / "cache")
    generated: set[str] = set()
    skipped: set[str] = set()
    refrozen: set[str] = set()
    failed: dict[str, tuple[TargetDiagnostic, ...]] = {}
    build_metadata: Mapping[str, Any] = {}
    build_cost: Mapping[str, Any] = {}
    repair_cost: Mapping[str, Any] = {}
    repair_metadata: Mapping[str, Any] | None = None
    in_memory_api_reuse_proof: dict[str, dict[str, str]] = {}
    effective_jobs = config.test.jobs if jobs is None else jobs
    if effective_jobs < 1:
        raise JauntConfigError("TypeScript test jobs must be >= 1")
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    target_config = _target(config)
    use_auto_skills = (
        target_config.auto_skills_enabled(bool(config.skills.auto))
        if auto_skills_enabled is None
        else auto_skills_enabled
    )
    npm_skill_metadata: Mapping[str, object] = {}
    if use_auto_skills:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        npm_skills = ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, target_config),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
        npm_skill_metadata = npm_skills.metadata()

    aggregate_cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)

    def phase_cost_tracker() -> CostTracker:
        child = getattr(aggregate_cost, "child", None)
        return child() if callable(child) else _ForwardingPhaseCostTracker(aggregate_cost)

    def merged_operation_cost(*summaries: Mapping[str, Any]) -> Mapping[str, object]:
        if cost_tracker is not None and not callable(getattr(cost_tracker, "child", None)):
            return cost_tracker.summary_dict()
        return _cost_summary(*summaries)

    # Resolve one backend only when a build or battery actually needs generation.
    # The cached resolver preserves one quota-wait budget across both phases while
    # keeping clean/no-work commands independent of Codex construction.
    backend = generator

    def model_backend() -> GeneratorBackend:
        nonlocal backend
        if backend is None:
            backend = (
                generator_factory() if generator_factory is not None else _default_backend(config)
            )
        return backend

    if not no_build:
        _progress_reset(progress)
        build_phase_cost = phase_cost_tracker()
        if worker_session_override is None:
            build = await run_build(
                root,
                config,
                target_ids=target_ids,
                force=force,
                generator=generator,
                generator_factory=model_backend if generator is None else None,
                cost_tracker=build_phase_cost,
                response_cache=response_cache,
                progress=progress,
                finish_progress=False,
                worker_factory=worker_factory,
                jobs=jobs,
                max_attempts=max_attempts,
                build_instructions=build_instructions,
                semantic_gate_enabled=semantic_gate_enabled,
                semantic_gate_exec=semantic_gate_exec,
                repo_map_enabled=repo_map_enabled,
                repo_map_block_override=repo_map_block_override,
                auto_skills_enabled=False,
                builtin_skill_names=effective_builtin_skills,
                reuse_proof_sink=in_memory_api_reuse_proof,
                validate_committed_batteries=not no_run,
            )
        else:
            build = await run_build_in_session(
                root,
                config,
                *worker_session_override,
                target_ids=target_ids,
                force=force,
                generator=generator,
                generator_factory=model_backend if generator is None else None,
                cost_tracker=build_phase_cost,
                response_cache=response_cache,
                progress=progress,
                finish_progress=False,
                jobs=jobs,
                max_attempts=max_attempts,
                build_instructions=build_instructions,
                semantic_gate_enabled=semantic_gate_enabled,
                semantic_gate_exec=semantic_gate_exec,
                repo_map_block=repo_map_block_override,
                project_overview_enabled=bool(config.context.overview),
                builtin_skill_names=effective_builtin_skills,
                reuse_proof_sink=in_memory_api_reuse_proof,
                validate_committed_batteries=not no_run,
            )
        build_metadata = _build_phase_metadata(build)
        raw_build_cost = build.metadata.get("cost")
        if isinstance(raw_build_cost, Mapping):
            build_cost = raw_build_cost
        if build.exit_code:
            _progress_finish(progress)
            return TargetTestReport(
                language="ts",
                failed=build.failed,
                runner={"build": build_metadata, "cost": dict(build_cost)},
                exit_code=build.exit_code,
            )

    cost = phase_cost_tracker()
    overlays: dict[str, str] = {}
    planned_generated: set[str] = set()
    planned_refrozen: set[str] = set()
    battery_outcomes: dict[str, dict[str, Any]] = {}
    pending_cache_writes: list[tuple[GenerationRequest, Any, str, str, str]] = []
    cached_battery_responses: dict[str, tuple[GenerationRequest, str, str]] = {}
    failed_battery_paths: set[str] = set()
    output_preconditions: dict[str, str] = {}
    repair_targets_by_file: dict[str, tuple[str, ...]] = {}
    rejected_test_tokens: dict[str, tuple[str, str, str] | None] = {}
    expected_test_provenance: dict[str, Mapping[str, str]] = {}
    modules: dict[str, Mapping[str, Any]] = {}
    current_target_artifact_ids: frozenset[str] = frozenset()
    pinned_vitest_config_snapshot: tuple[Mapping[str, str], Mapping[str, str]] = ({}, {})
    pinned_vitest_config_closure: Mapping[str, str] = {}
    test_owners: dict[str, str] = {}
    files: tuple[str, ...] = ()
    runner: Mapping[str, Any] = {"ok": True, "skipped": True}

    def record_battery_outcome(
        path: str,
        tier: str,
        state: str,
        *,
        result: Any | None = None,
    ) -> None:
        outcome = {**battery_outcomes.get(path, {}), "tier": tier, "state": state}
        outcome.setdefault("attempts", 0)
        outcome.setdefault("retry_count", 0)
        outcome.setdefault("retry_reasons", ())
        if result is not None:
            retry_reasons = tuple(
                dict.fromkeys(error for attempt in result.attempt_errors for error in attempt)
            )
            outcome.update(
                {
                    "attempts": result.attempts,
                    "retry_count": max(0, result.attempts - 1),
                    "retry_reasons": retry_reasons,
                }
            )
            if result.infrastructure_errors:
                outcome.update(
                    {
                        "infrastructure_retries": result.infrastructure_retries,
                        "infrastructure_errors": result.infrastructure_errors,
                    }
                )
        battery_outcomes[path] = outcome

    def stage_validated_batteries() -> None:
        for request, result, fingerprint, path, tier in pending_cache_writes:
            store_generation_result(
                response_cache,
                model_backend(),
                request,
                result,
                generation_fingerprint=fingerprint,
            )
            if isinstance(result.source, str):
                cached_battery_responses[path] = (
                    request,
                    fingerprint,
                    result.source,
                )
            record_battery_outcome(path, tier, "staged")
            _progress_phase(progress, path, "staged", tier)
        pending_cache_writes.clear()

    @asynccontextmanager
    async def operation_worker() -> AsyncIterator[tuple[Any, Any]]:
        if worker_session_override is not None:
            yield worker_session_override
            return
        async with worker_session(
            root,
            config,
            worker_factory=worker_factory,
        ) as session:
            yield session

    async with operation_worker() as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
        pinned_runner_fingerprint = _runner_fingerprint(root, client, initialized)
        _pin_test_runtime_identity(client)
        modules = {_module_id(module): module for module in analysis.modules}
        current_target_artifact_ids, target_artifact_preconditions = (
            _current_target_artifact_snapshot(
                root,
                tuple(modules.values()),
                strict=True,
            )
        )
        pinned_vitest_config_snapshot = (
            _local_config_snapshot(root, target_config.vitest_config, client=client)
            if target_config.vitest_config
            else ({}, {})
        )
        pinned_vitest_config_closure, pinned_vitest_config_overlays = pinned_vitest_config_snapshot
        for relative, digest in {
            **target_artifact_preconditions,
            **pinned_vitest_config_closure,
        }.items():
            previous = output_preconditions.setdefault(relative, digest)
            if previous != digest:
                raise JauntGenerationError(
                    "TypeScript test input changed while transaction preconditions "
                    f"were prepared: {relative}"
                )
        test_specs = _selected_test_specs(
            root,
            config,
            analysis.workspace,
            modules,
            target_ids=target_ids,
        )
        test_owners = dict(_workspace_test_file_owners(root, config, analysis.workspace))
        prepared_requests: list[
            tuple[Mapping[str, Any], str, tuple[str, ...], GenerationRequest]
        ] = []
        for test_spec in test_specs:
            spec_path = str(test_spec.get("path", ""))
            selected_module_ids = tuple(
                sorted(_module_id(module) for module in _selected_test_modules(test_spec, modules))
            )
            if not isinstance(test_spec.get("syntheticSource"), str):
                output_preconditions[spec_path] = (
                    _path_hash(_safe_path(root, spec_path)) or MISSING_INPUT
                )
            for tier in ("example", "derived"):
                request = _test_request(
                    root,
                    config,
                    test_spec,
                    modules,
                    tier=tier,
                    build_instructions=effective_instructions,
                    builtin_skill_names=effective_builtin_skills,
                )
                owner = test_spec.get("project")
                if not isinstance(owner, str):
                    owner = _owner_project_for_source(
                        root,
                        config,
                        analysis.workspace,
                        request.target_path,
                    )
                test_owners[request.target_path] = owner
                repair_targets_by_file[request.target_path] = selected_module_ids
                output_preconditions[request.target_path] = (
                    _path_hash(_safe_path(root, request.target_path)) or MISSING_INPUT
                )
                for candidate, candidate_digest in _fixture_resolution_preconditions(
                    root, spec_path
                ).items():
                    previous_candidate_digest = output_preconditions.setdefault(
                        candidate, candidate_digest
                    )
                    if previous_candidate_digest != candidate_digest:
                        raise JauntGenerationError(
                            "TypeScript fixture resolution changed while battery requests "
                            f"were prepared: {candidate}"
                        )
                fixture_path = request.cache_payload.get("fixturePath")
                fixture_source = request.context_files.get("_context/fixtures.ts")
                fixture_digest = request.cache_payload.get("fixtureDigest")
                if (
                    isinstance(fixture_path, str)
                    and fixture_path
                    and isinstance(fixture_source, str)
                    and isinstance(fixture_digest, str)
                    and fixture_digest
                ):
                    # Bind the commit to the exact fixture bytes supplied to the
                    # request even when the fixture is outside every tsconfig.
                    if _sha256(fixture_source.encode("utf-8")) != fixture_digest:
                        raise JauntGenerationError(
                            "TypeScript fixture bytes do not match their captured digest: "
                            + fixture_path
                        )
                    previous_fixture_digest = output_preconditions.setdefault(
                        fixture_path, fixture_digest
                    )
                    if previous_fixture_digest != fixture_digest:
                        raise JauntGenerationError(
                            "TypeScript fixture changed while battery requests were prepared: "
                            + fixture_path
                        )
                prepared_requests.append((test_spec, spec_path, selected_module_ids, request))

        planned_files = tuple(request.target_path for *_prefix, request in prepared_requests)
        _progress_reset(progress, len(prepared_requests))
        if planned_files:
            planned_groups = _group_test_files(
                root,
                config,
                analysis.workspace,
                planned_files,
                explicit_owners=test_owners,
            )
            _validate_test_owner_dependencies(
                root,
                analysis.workspace,
                planned_groups,
                require_fast_check=any(
                    isinstance(
                        property_count := request.cache_payload.get("propertyCount"),
                        int,
                    )
                    and property_count > 0
                    for *_prefix, request in prepared_requests
                ),
            )
            _pin_test_dependency_runtimes(
                client,
                root,
                analysis.workspace,
                planned_groups,
            )

        generation_work: list[
            tuple[
                str,
                GenerationRequest,
                Mapping[str, Any],
                str,
                str,
            ]
        ] = []
        verification_work: list[
            tuple[
                str,
                GenerationRequest,
                Mapping[str, Any],
                str,
                str,
                str,
            ]
        ] = []
        verified_paths: set[str] = set()
        for test_spec, spec_path, _selected_module_ids, request in prepared_requests:
            tier = str(request.cache_payload.get("tier", "example"))
            provenance = _test_provenance(
                root,
                config,
                test_spec,
                modules,
                client,
                initialized,
                tier=tier,
                builtin_skill_names=effective_builtin_skills,
                prepared_request=request,
                runner_fingerprint=pinned_runner_fingerprint,
                workspace=analysis.workspace,
            )
            expected_test_provenance[request.target_path] = provenance
            selected_modules = _selected_test_modules(test_spec, modules)
            rejected_test_tokens[request.target_path] = _rejected_test_token(
                root,
                request.target_path,
                expected_fingerprint=str(provenance["battery_fingerprint"]),
                expected_provenance=provenance,
            )
            action, existing_source = _existing_test_battery_action(
                root,
                request,
                tier=tier,
                source_path=spec_path,
                provenance=provenance,
                force=force,
                generated_dirs=(_target(config).generated_dir,),
                proven_previous_api_digests=proven_previous_target_api_digests(
                    root,
                    selected_modules,
                    additional_previous=in_memory_api_reuse_proof,
                ),
                allow_verified_api_transition=(
                    not no_run
                    and bool(selected_modules)
                    and all(
                        _module_id(module) in current_target_artifact_ids
                        for module in selected_modules
                    )
                ),
            )
            if action == "skip":
                _clear_rejected_test_candidate(
                    root,
                    request.target_path,
                    expected_token=rejected_test_tokens[request.target_path],
                )
                skipped.add(request.target_path)
                record_battery_outcome(request.target_path, tier, "fresh")
                _progress_phase(progress, request.target_path, "fresh")
                _progress_advance(progress, request.target_path, ok=True)
                continue
            if action == "refreeze":
                assert existing_source is not None
                overlays[request.target_path] = existing_source
                planned_refrozen.add(request.target_path)
                record_battery_outcome(request.target_path, tier, "refrozen")
                _progress_phase(progress, request.target_path, "refrozen")
                _progress_advance(progress, request.target_path, ok=True)
                continue
            if action == "verify":
                assert existing_source is not None
                record_battery_outcome(request.target_path, tier, "verification-pending")
                _progress_phase(progress, request.target_path, "verifying", tier)
                verification_work.append(
                    (
                        spec_path,
                        request,
                        provenance,
                        tier,
                        f"{spec_path}#{tier}",
                        existing_source,
                    )
                )
                continue
            key = f"{spec_path}#{tier}"
            generation_work.append((spec_path, request, provenance, tier, key))

        async def verify_existing(
            items: Sequence[tuple[str, GenerationRequest, Mapping[str, Any], str, str, str]],
        ) -> tuple[str, Mapping[str, Any]]:
            paths = tuple(item[1].target_path for item in items)
            candidate_overlays = {item[1].target_path: item[5] for item in items}
            checked = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=paths,
                explicit_owners=test_owners,
                overlays=candidate_overlays,
                redact_derived=not no_redact_derived,
                typecheck_only=True,
                config_snapshot=pinned_vitest_config_snapshot,
            )
            if not bool(checked.get("ok", False)):
                return (
                    "infrastructure"
                    if _typecheck_failure_is_infrastructure(checked)
                    else "incompatible",
                    checked,
                )
            ran = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=paths,
                explicit_owners=test_owners,
                overlays=candidate_overlays,
                redact_derived=not no_redact_derived,
                config_snapshot=pinned_vitest_config_snapshot,
            )
            if bool(ran.get("ok", False)):
                return "verified", ran
            return (
                "incompatible" if _runner_allows_implementation_repair(ran) else "infrastructure",
                ran,
            )

        verified_items: set[str] = set()
        verification_infrastructure: dict[str, Mapping[str, Any]] = {}
        if verification_work:
            verification_state, verification_result = await verify_existing(verification_work)
            if verification_state == "verified":
                verified_items.update(item[1].target_path for item in verification_work)
            elif verification_state == "infrastructure":
                verification_infrastructure.update(
                    {item[1].target_path: verification_result for item in verification_work}
                )
            else:
                for item in verification_work:
                    item_state, item_result = await verify_existing((item,))
                    if item_state == "verified":
                        verified_items.add(item[1].target_path)
                    elif item_state == "infrastructure":
                        verification_infrastructure[item[1].target_path] = item_result
        for spec_path, request, provenance, tier, key, existing_source in verification_work:
            if request.target_path in verified_items:
                overlays[request.target_path] = existing_source
                planned_refrozen.add(request.target_path)
                verified_paths.add(request.target_path)
                record_battery_outcome(request.target_path, tier, "verified")
                _progress_phase(progress, request.target_path, "verified", tier)
                _progress_advance(progress, request.target_path, ok=True)
            elif request.target_path in verification_infrastructure:
                infrastructure_result = verification_infrastructure[request.target_path]
                categories = _runner_failure_categories(infrastructure_result)
                failed_battery_paths.add(request.target_path)
                failed[key] = (
                    TargetDiagnostic(
                        code="JAUNT_TS_TEST_VERIFICATION_INFRASTRUCTURE",
                        message=(
                            "The existing TypeScript battery could not verify its API-only "
                            "transition because the protected runner was unavailable; no model "
                            "generation was queued. "
                            + " ".join(_runner_validation_errors(infrastructure_result))
                        ),
                        path=request.target_path,
                        data={
                            "source": spec_path,
                            "tier": tier,
                            "categories": tuple(sorted(categories)) or ("unknown",),
                        },
                    ),
                )
                record_battery_outcome(
                    request.target_path,
                    tier,
                    "verification-infrastructure",
                )
                _progress_phase(progress, request.target_path, "failed", tier)
                _progress_advance(progress, request.target_path, ok=False)
            else:
                record_battery_outcome(request.target_path, tier, "verification-failed")
                _progress_phase(progress, request.target_path, "regenerating", tier)
                generation_work.append((spec_path, request, provenance, tier, key))

        semaphore = asyncio.Semaphore(effective_jobs)

        async def generate_one(
            item: tuple[
                str,
                GenerationRequest,
                Mapping[str, Any],
                str,
                str,
            ],
        ) -> tuple[
            str,
            GenerationRequest,
            Mapping[str, Any],
            str,
            str,
            Any,
            str,
        ]:
            spec_path, request, provenance, tier, key = item

            base_validator = request.validator

            async def validate_candidate(
                source: str,
                *,
                _request: GenerationRequest = request,
                _base_validator: Callable[[str], Any] = base_validator,
                _owner: str = test_owners[request.target_path],
            ) -> list[str]:
                static_result = _base_validator(source)
                static_errors = (
                    await static_result if inspect.isawaitable(static_result) else static_result
                )
                if static_errors:
                    return list(static_errors)
                property_block = _request.cache_payload.get("propertyBlock", "")
                rendered = attach_property_block(
                    source,
                    property_block if isinstance(property_block, str) else "",
                )
                checked = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=(_request.target_path,),
                    explicit_owners={_request.target_path: _owner},
                    overlays={_request.target_path: rendered},
                    redact_derived=not no_redact_derived,
                    typecheck_only=True,
                    config_snapshot=pinned_vitest_config_snapshot,
                )
                if bool(checked.get("ok", False)):
                    return []
                if _typecheck_failure_is_infrastructure(checked):
                    categories = _runner_failure_categories(checked)
                    rendered_categories = ", ".join(sorted(categories)) or "unknown runner failure"
                    error = _CommittedBatteryInfrastructureError(
                        (
                            "Generated TypeScript battery typechecking could not validate the "
                            "candidate because the protected runner failed "
                            f"({rendered_categories}); the candidate was preserved and no "
                            "additional model attempt was made. "
                            + " ".join(_runner_validation_errors(checked)),
                        )
                    )
                    raise error.attach_candidate(source)
                return _runner_validation_errors(checked)

            cache_fingerprint = str(provenance["battery_fingerprint"])
            cache_for_request = None if force else response_cache
            validated_request = replace(request, validator=validate_candidate)
            async with semaphore:
                _progress_phase(progress, request.target_path, "generating", tier)
                attempt_count = 0
                attempt_usage: list[TokenUsage] = []
                cached_source_failed_infrastructure = False

                def request_progress(stage: str, detail: str) -> None:
                    nonlocal attempt_count
                    if stage == "attempt":
                        attempt_count += 1
                    _progress_phase(progress, request.target_path, stage, detail)

                def record_request_usage(usage: TokenUsage) -> None:
                    attempt_usage.append(usage)
                    cost.record(key, usage)
                    cost.check_budget()

                async def validate_cached_source(source: str) -> list[str]:
                    nonlocal cached_source_failed_infrastructure
                    try:
                        return await validate_candidate(source)
                    except _CommittedBatteryInfrastructureError:
                        cached_source_failed_infrastructure = True
                        raise

                try:
                    result = await generate_request_cached(
                        model_backend(),
                        validated_request,
                        max_attempts=max_attempts,
                        generation_fingerprint=cache_fingerprint,
                        response_cache=cache_for_request,
                        cost_tracker=cost,
                        usage_callback=record_request_usage,
                        usage_label=key,
                        progress=request_progress,
                        cached_validator=validate_cached_source,
                        store=False,
                    )
                except (JauntBudgetExceededError, JauntQuotaGenerationError):
                    raise
                except _CommittedBatteryInfrastructureError as error:
                    usage = (
                        TokenUsage(
                            prompt_tokens=sum(item.prompt_tokens for item in attempt_usage),
                            completion_tokens=sum(item.completion_tokens for item in attempt_usage),
                            model=attempt_usage[-1].model,
                            provider=attempt_usage[-1].provider,
                            cached_prompt_tokens=sum(
                                item.cached_prompt_tokens for item in attempt_usage
                            ),
                        )
                        if attempt_usage
                        else None
                    )
                    result = GenerationResult(
                        attempts=(
                            0 if cached_source_failed_infrastructure else max(1, attempt_count)
                        ),
                        source=error.candidate_source,
                        errors=list(error.errors),
                        usage=usage,
                        infrastructure_errors=error.errors,
                        infrastructure_exhausted=True,
                    )
                    if result.source is not None and result.attempts > 0:
                        store_generation_result(
                            response_cache,
                            model_backend(),
                            validated_request,
                            replace(result, errors=[]),
                            generation_fingerprint=cache_fingerprint,
                        )
                except JauntGenerationError as error:
                    message = str(error)
                    result = GenerationResult(
                        attempts=0,
                        source=None,
                        errors=[message],
                        infrastructure_errors=(message,),
                        infrastructure_exhausted=True,
                    )
            return (
                spec_path,
                validated_request,
                provenance,
                tier,
                key,
                result,
                cache_fingerprint,
            )

        tasks = [asyncio.create_task(generate_one(item)) for item in generation_work]
        try:
            for completed in asyncio.as_completed(tasks):
                (
                    spec_path,
                    request,
                    provenance,
                    tier,
                    key,
                    result,
                    cache_fingerprint,
                ) = await completed
                if result.source is None or result.errors:
                    failed_battery_paths.add(request.target_path)
                    property_block = request.cache_payload.get("propertyBlock", "")
                    rejected_source = (
                        attach_property_block(
                            result.source,
                            property_block if isinstance(property_block, str) else "",
                        )
                        if isinstance(result.source, str)
                        else ""
                    )
                    rejected_artifacts = (
                        None
                        if result.infrastructure_exhausted or not rejected_source
                        else _write_rejected_test_candidate(
                            root,
                            request,
                            source_path=spec_path,
                            tier=tier,
                            fingerprint=cache_fingerprint,
                            candidate_source=rejected_source,
                            attempts=result.attempts,
                            errors=result.errors,
                            attempt_errors=result.attempt_errors,
                            terminal=result.attempts >= max_attempts,
                            expected_provenance=provenance,
                        )
                    )
                    exhausted = (
                        not result.infrastructure_exhausted and result.attempts >= max_attempts
                    )
                    failed[key] = tuple(
                        TargetDiagnostic(
                            code=(
                                "JAUNT_TS_TEST_INFRASTRUCTURE"
                                if result.infrastructure_exhausted
                                else (
                                    "JAUNT_TS_TEST_GENERATION_EXHAUSTED"
                                    if exhausted
                                    else "JAUNT_TS_TEST_GENERATION"
                                )
                            ),
                            message=(
                                error
                                + (
                                    f" Last rejected candidate: {rejected_artifacts[0]}."
                                    if rejected_artifacts is not None
                                    else ""
                                )
                            ),
                            path=request.target_path,
                            data={
                                "source": spec_path,
                                "tier": tier,
                                "attempts": result.attempts,
                                **(
                                    {
                                        "candidate": rejected_artifacts[0],
                                        "metadata": rejected_artifacts[1],
                                    }
                                    if rejected_artifacts is not None
                                    else {}
                                ),
                            },
                        )
                        for error in result.errors or ["The generator returned no test source"]
                    )
                    record_battery_outcome(
                        request.target_path,
                        tier,
                        "infrastructure-failed" if result.infrastructure_exhausted else "failed",
                        result=result,
                    )
                    if rejected_artifacts is not None:
                        battery_outcomes[request.target_path].update(
                            {
                                "candidate": rejected_artifacts[0],
                                "candidate_metadata": rejected_artifacts[1],
                                "terminal": exhausted,
                            }
                        )
                    _progress_phase(progress, request.target_path, "failed", tier)
                    _progress_advance(progress, request.target_path, ok=False)
                    continue
                property_block = request.cache_payload.get("propertyBlock", "")
                rendered_source = attach_property_block(
                    result.source,
                    property_block if isinstance(property_block, str) else "",
                )
                overlays[request.target_path] = _with_test_header(
                    rendered_source,
                    tier=tier,
                    source_path=spec_path,
                    provenance=provenance,
                )
                planned_generated.add(request.target_path)
                if result.attempts > 0:
                    pending_cache_writes.append(
                        (
                            request,
                            result,
                            cache_fingerprint,
                            request.target_path,
                            tier,
                        )
                    )
                state = "cached" if result.attempts == 0 else "validated"
                record_battery_outcome(
                    request.target_path,
                    tier,
                    state,
                    result=result,
                )
                if result.attempts == 0:
                    cached_battery_responses[request.target_path] = (
                        request,
                        cache_fingerprint,
                        result.source,
                    )
                _progress_phase(progress, request.target_path, state, tier)
                _progress_advance(progress, request.target_path, ok=True)
        finally:
            # A fatal result may arrive after one or more sibling tasks have
            # already failed.  Draining only pending tasks leaves those done
            # exceptions unobserved and emits ``Task exception was never
            # retrieved`` after the command returns.  Cancel pending work, but
            # gather every task so the first awaited exception remains the
            # public failure while all sibling outcomes are consumed.
            for task in tasks:
                if not task.done():
                    task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        files = tuple(
            sorted(
                set(
                    _selected_generated_test_files(
                        root,
                        config,
                        test_specs,
                        target_ids=target_ids,
                    )
                )
                | set(overlays)
            )
        )

        async def isolate_valid_overlays(
            *, excluded_paths: frozenset[str] = frozenset()
        ) -> tuple[
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
            Mapping[str, Any],
            Mapping[str, tuple[str, ...]],
        ]:
            """Find a deterministic maximal overlay subset that validates together."""

            effective_files = tuple(path for path in files if path not in excluded_paths)
            candidate_paths = tuple(sorted(path for path in overlays if path not in excluded_paths))
            baseline_files = tuple(path for path in effective_files if path not in overlays)
            baseline_result: Mapping[str, Any] = {"ok": True, "skipped": True}
            if baseline_files:
                baseline_result = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=baseline_files,
                    explicit_owners=test_owners,
                    overlays={},
                    redact_derived=not no_redact_derived,
                    typecheck_only=True,
                    config_snapshot=pinned_vitest_config_snapshot,
                )
            if not bool(baseline_result.get("ok", False)):
                if _typecheck_failure_is_infrastructure(baseline_result):
                    return (
                        (),
                        (),
                        candidate_paths,
                        effective_files,
                        {
                            "baseline": baseline_result,
                            "candidates": [],
                            "infrastructure": True,
                        },
                        {},
                    )
                reasons = tuple(_runner_validation_errors(baseline_result))
                return (
                    (),
                    (),
                    candidate_paths,
                    effective_files,
                    {
                        "baseline": baseline_result,
                        "candidates": [],
                        "baseline_failure": True,
                        "baseline_errors": reasons,
                    },
                    {},
                )

            accepted: list[str] = []
            rejected: list[str] = []
            rejection_reasons: dict[str, tuple[str, ...]] = {}
            candidate_results: list[dict[str, Any]] = []
            for path in candidate_paths:
                trial_paths = (*accepted, path)
                trial_files = tuple(sorted({*baseline_files, *trial_paths}))
                checked = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=trial_files,
                    explicit_owners=test_owners,
                    overlays={item: overlays[item] for item in trial_paths},
                    redact_derived=not no_redact_derived,
                    typecheck_only=True,
                    config_snapshot=pinned_vitest_config_snapshot,
                )
                valid = bool(checked.get("ok", False))
                candidate_results.append({"path": path, "ok": valid})
                if valid:
                    accepted.append(path)
                    continue
                if _typecheck_failure_is_infrastructure(checked):
                    retained = tuple(path for path in candidate_paths if path not in accepted)
                    return (
                        tuple(accepted),
                        (),
                        retained,
                        tuple(sorted({*baseline_files, *accepted})),
                        {
                            "baseline": baseline_result,
                            "candidates": candidate_results,
                            "infrastructure": True,
                        },
                        {},
                    )
                rejected.append(path)
                rejection_reasons[path] = tuple(_runner_validation_errors(checked))
            accepted_files = tuple(sorted({*baseline_files, *accepted}))
            return (
                tuple(accepted),
                tuple(rejected),
                (),
                accepted_files,
                {"baseline": baseline_result, "candidates": candidate_results},
                rejection_reasons,
            )

        def reject_batteries(
            paths: Sequence[str],
            reasons: Mapping[str, tuple[str, ...]],
        ) -> None:
            tiers = {
                path: str(battery_outcomes.get(path, {}).get("tier", "example")) for path in paths
            }
            for path in paths:
                record_battery_outcome(path, tiers[path], "rejected")
                battery_outcomes[path]["rejection_reasons"] = reasons.get(path, ())
                cached = cached_battery_responses.pop(path, None)
                pending = next(
                    (item for item in pending_cache_writes if item[3] == path),
                    None,
                )
                if cached is not None or pending is not None:
                    result = None
                    if cached is not None:
                        request, fingerprint, source = cached
                    else:
                        assert pending is not None
                        request, result, fingerprint, _pending_path, _pending_tier = pending
                        source = result.source if isinstance(result.source, str) else ""
                    property_block = request.cache_payload.get("propertyBlock", "")
                    candidate_source = attach_property_block(
                        source,
                        property_block if isinstance(property_block, str) else "",
                    )
                    rejected_artifacts = _write_rejected_test_candidate(
                        root,
                        request,
                        source_path=str(request.cache_payload.get("path", "")),
                        tier=tiers[path],
                        fingerprint=fingerprint,
                        candidate_source=candidate_source,
                        attempts=(
                            result.attempts
                            if result is not None
                            else int(battery_outcomes[path].get("attempts", 0))
                        ),
                        errors=reasons.get(path, ()),
                        attempt_errors=(
                            (*result.attempt_errors, reasons.get(path, ()))
                            if result is not None
                            else (reasons.get(path, ()),)
                        ),
                        terminal=False,
                        expected_provenance=expected_test_provenance[path],
                    )
                    if rejected_artifacts is not None:
                        battery_outcomes[path].update(
                            {
                                "candidate": rejected_artifacts[0],
                                "candidate_metadata": rejected_artifacts[1],
                            }
                        )
                    if cached is not None:
                        battery_outcomes[path]["cache_evicted"] = discard_cached_generation(
                            response_cache,
                            model_backend(),
                            request,
                            generation_fingerprint=fingerprint,
                            expected_source=source,
                        )

        async def run_surviving_batteries(
            candidate_paths: Sequence[str],
            candidate_files: Sequence[str],
            *,
            rejection_reasons: Callable[[Mapping[str, Any]], tuple[str, ...]],
        ) -> tuple[
            tuple[str, ...],
            tuple[str, ...],
            Mapping[str, str],
            tuple[str, ...],
            Mapping[str, Any],
        ]:
            """Reject attributable runtime failures, then prove the survivors.

            Each failed iteration must remove at least one candidate before the
            next run. An unattributed/protocol failure stops the loop without
            publishing the still-staged survivors.
            """

            current_paths = tuple(candidate_paths)
            current_files = tuple(candidate_files)
            current_overlays = {path: overlays[path] for path in current_paths}
            rejected: list[str] = []
            result: Mapping[str, Any] = {"ok": True, "skipped": True}
            while current_paths:
                result = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=current_files,
                    explicit_owners=test_owners,
                    overlays=current_overlays,
                    redact_derived=not no_redact_derived,
                    config_snapshot=pinned_vitest_config_snapshot,
                )
                if bool(result.get("ok", False)):
                    break
                failed_paths = _runner_candidate_rejection_paths(result, current_paths)
                if not failed_paths:
                    break
                failed_set = set(failed_paths)
                reasons = rejection_reasons(result)
                reject_batteries(
                    failed_paths,
                    {path: reasons for path in failed_paths},
                )
                rejected.extend(failed_paths)
                current_paths = tuple(path for path in current_paths if path not in failed_set)
                current_files = tuple(path for path in current_files if path not in failed_set)
                current_overlays = {path: overlays[path] for path in current_paths}
            return (
                current_paths,
                current_files,
                current_overlays,
                tuple(dict.fromkeys(rejected)),
                result,
            )

        if failed:
            stage_preflight: Mapping[str, Any] = {"ok": True, "skipped": True}
            stage_isolation: Mapping[str, Any] | None = None
            accepted_paths = tuple(sorted(overlays))
            rejected_paths: tuple[str, ...] = ()
            retained_paths: tuple[str, ...] = ()
            stage_files = tuple(path for path in files if path not in failed_battery_paths)
            if overlays and stage_files:
                stage_preflight = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=stage_files,
                    explicit_owners=test_owners,
                    overlays=overlays,
                    redact_derived=not no_redact_derived,
                    typecheck_only=True,
                    config_snapshot=pinned_vitest_config_snapshot,
                )
            stage_preflight_infrastructure = False
            stage_preflight_baseline_failure = False
            if bool(stage_preflight.get("ok", False)):
                accepted = set(accepted_paths)
            elif _typecheck_failure_is_infrastructure(stage_preflight):
                retained_paths = accepted_paths
                accepted_paths = ()
                accepted = set()
                stage_preflight_infrastructure = True
                stage_isolation = {"infrastructure": True, "preflight": stage_preflight}
            else:
                (
                    accepted_paths,
                    rejected_paths,
                    retained_paths,
                    _accepted_files,
                    stage_isolation,
                    reasons,
                ) = await isolate_valid_overlays(excluded_paths=frozenset(failed_battery_paths))
                accepted = set(accepted_paths)
                stage_preflight_infrastructure = bool(stage_isolation.get("infrastructure"))
                stage_preflight_baseline_failure = bool(stage_isolation.get("baseline_failure"))
                if not stage_preflight_infrastructure and not stage_preflight_baseline_failure:
                    reject_batteries(rejected_paths, reasons)

            stage_preflight_blocked = (
                stage_preflight_infrastructure or stage_preflight_baseline_failure
            )
            if stage_preflight_blocked:
                failed["stage-preflight"] = (
                    TargetDiagnostic(
                        code=(
                            "JAUNT_TS_TEST_INFRASTRUCTURE"
                            if stage_preflight_infrastructure
                            else "JAUNT_TS_TEST_TYPECHECK"
                        ),
                        message=(
                            "The surviving TypeScript battery set could not be typechecked "
                            + (
                                "because the protected runner was unavailable; "
                                if stage_preflight_infrastructure
                                else "because a committed baseline battery is invalid; "
                            )
                            + "validated candidates were retained in the response cache and "
                            "were not committed."
                        ),
                    ),
                )

            accepted_overlays = {path: overlays[path] for path in accepted_paths}
            surviving_stage_files = tuple(
                path for path in stage_files if path not in set(rejected_paths)
            )
            stage_runner: Mapping[str, Any] = (
                {
                    "ok": False,
                    "skipped": True,
                    "reason": (
                        "preflight-infrastructure"
                        if stage_preflight_infrastructure
                        else "baseline-typecheck"
                    ),
                }
                if stage_preflight_blocked
                else {"ok": True, "skipped": True}
            )
            if accepted_overlays and not no_run and not stage_preflight_blocked:
                (
                    accepted_paths,
                    surviving_stage_files,
                    accepted_overlays,
                    runtime_rejected,
                    stage_runner,
                ) = await run_surviving_batteries(
                    accepted_paths,
                    surviving_stage_files,
                    rejection_reasons=lambda result: tuple(_runner_validation_errors(result)),
                )
                accepted = set(accepted_paths)
                rejected_paths = tuple(sorted({*rejected_paths, *runtime_rejected}))
                if not bool(stage_runner.get("ok", False)) and accepted_paths:
                    failed["stage-runner"] = (
                        TargetDiagnostic(
                            code="JAUNT_TS_TEST_INFRASTRUCTURE",
                            message=(
                                "The surviving TypeScript battery set could not be executed; "
                                "validated candidates were retained in the response cache and "
                                "were not committed."
                            ),
                        ),
                    )

            cacheable_paths = accepted | set(retained_paths)
            pending_cache_writes[:] = [
                item for item in pending_cache_writes if item[3] in cacheable_paths
            ]
            stage_validated_batteries()
            partial_committed = bool(accepted_overlays) and bool(stage_runner.get("ok", False))
            if partial_committed:
                atomic_write_manifest(
                    root,
                    tuple(
                        _Write(
                            path=path,
                            content=source,
                            kind="test",
                            module_id=f"ts-test:{path}",
                        )
                        for path, source in accepted_overlays.items()
                    ),
                    expected_inputs={
                        **_input_hashes(analysis.contracts),
                        **output_preconditions,
                    },
                    pre_commit_guard=lambda: _verify_test_commit_environment(
                        root,
                        client,
                        initialized,
                        pinned_runner_fingerprint,
                        vitest_config=target_config.vitest_config,
                        config_closure=pinned_vitest_config_closure,
                    ),
                    commit_seal=lambda: _seal_test_commit_environment(
                        root,
                        client,
                        initialized,
                        pinned_runner_fingerprint,
                        vitest_config=target_config.vitest_config,
                        config_closure=pinned_vitest_config_closure,
                    ),
                )
                generated.update(planned_generated.intersection(accepted))
                refrozen.update(planned_refrozen.intersection(accepted))
                for path in accepted:
                    _clear_rejected_test_candidate(
                        root,
                        path,
                        expected_token=rejected_test_tokens.get(path),
                    )
                    if path in planned_generated:
                        record_battery_outcome(
                            path,
                            str(battery_outcomes[path].get("tier", "example")),
                            "committed",
                        )
                for path in planned_refrozen.intersection(accepted):
                    if battery_outcomes.get(path, {}).get("state") == "verification-pending":
                        record_battery_outcome(
                            path,
                            str(battery_outcomes[path].get("tier", "example")),
                            "verified",
                        )
            _progress_finish(progress)
            test_cost = cost.summary_dict()
            merged_cost = merged_operation_cost(build_cost, test_cost)
            return TargetTestReport(
                language="ts",
                generated=frozenset(generated),
                skipped=frozenset(skipped),
                refrozen=frozenset(refrozen),
                failed=failed,
                runner={
                    "cost": merged_cost,
                    "test_cost": test_cost,
                    "build": build_metadata,
                    "stage_preflight": stage_preflight,
                    "partial_landing": {
                        "accepted": accepted_paths,
                        "rejected": rejected_paths,
                        "retained": retained_paths,
                        "committed": partial_committed,
                        "runner": stage_runner,
                    },
                    **({"stage_isolation": stage_isolation} if stage_isolation is not None else {}),
                    "jobs": effective_jobs,
                    "batteries": [
                        {"path": path, **battery_outcomes[path]}
                        for path in sorted(battery_outcomes)
                    ],
                    **(
                        {
                            "cache": {
                                "hits": response_cache.hits,
                                "misses": response_cache.misses,
                            }
                        }
                        if response_cache is not None
                        else {}
                    ),
                },
                exit_code=3,
            )

        preflight: Mapping[str, Any] = {"ok": True}
        if files:
            preflight = await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=files,
                explicit_owners=test_owners,
                overlays=overlays,
                redact_derived=not no_redact_derived,
                typecheck_only=True,
                config_snapshot=pinned_vitest_config_snapshot,
            )
        if not bool(preflight.get("ok", False)):
            (
                accepted_paths,
                rejected_paths,
                retained_paths,
                accepted_files,
                isolation,
                reasons,
            ) = await isolate_valid_overlays()
            accepted = set(accepted_paths)
            accepted_overlays = {path: overlays[path] for path in accepted_paths}
            preflight_infrastructure = bool(isolation.get("infrastructure"))
            preflight_baseline_failure = bool(isolation.get("baseline_failure"))
            preflight_blocked = preflight_infrastructure or preflight_baseline_failure
            if not preflight_blocked:
                reject_batteries(rejected_paths, reasons)
            cacheable_paths = accepted | set(retained_paths)
            pending_cache_writes[:] = [
                item for item in pending_cache_writes if item[3] in cacheable_paths
            ]
            stage_validated_batteries()

            partial_runner: Mapping[str, Any] = (
                {
                    "ok": False,
                    "skipped": True,
                    "reason": (
                        "preflight-infrastructure"
                        if preflight_infrastructure
                        else "baseline-typecheck"
                    ),
                }
                if preflight_blocked
                else {"ok": True, "skipped": True}
            )
            if not no_run and not preflight_blocked and accepted_overlays and accepted_files:
                (
                    accepted_paths,
                    accepted_files,
                    accepted_overlays,
                    failed_partial_paths,
                    partial_runner,
                ) = await run_surviving_batteries(
                    accepted_paths,
                    accepted_files,
                    rejection_reasons=lambda _result: (
                        "The compatible-subset Vitest run rejected this battery; "
                        "its cached response was removed.",
                    ),
                )
                accepted = set(accepted_paths)
                rejected_paths = tuple(sorted({*rejected_paths, *failed_partial_paths}))
                failed_partial = set(failed_partial_paths)
                retained_paths = tuple(
                    path for path in retained_paths if path not in failed_partial
                )
            partial_committed = bool(accepted_overlays) and bool(partial_runner.get("ok", False))
            committed_generated = planned_generated.intersection(accepted)
            committed_refrozen = planned_refrozen.intersection(accepted)
            if partial_committed and accepted_overlays:
                atomic_write_manifest(
                    root,
                    tuple(
                        _Write(
                            path=path,
                            content=source,
                            kind="test",
                            module_id=f"ts-test:{path}",
                        )
                        for path, source in accepted_overlays.items()
                    ),
                    expected_inputs={
                        **_input_hashes(analysis.contracts),
                        **output_preconditions,
                    },
                    pre_commit_guard=lambda: _verify_test_commit_environment(
                        root,
                        client,
                        initialized,
                        pinned_runner_fingerprint,
                        vitest_config=target_config.vitest_config,
                        config_closure=pinned_vitest_config_closure,
                    ),
                    commit_seal=lambda: _seal_test_commit_environment(
                        root,
                        client,
                        initialized,
                        pinned_runner_fingerprint,
                        vitest_config=target_config.vitest_config,
                        config_closure=pinned_vitest_config_closure,
                    ),
                )
                generated.update(committed_generated)
                refrozen.update(committed_refrozen)
                for path in accepted:
                    _clear_rejected_test_candidate(
                        root,
                        path,
                        expected_token=rejected_test_tokens.get(path),
                    )
                    if path in committed_generated:
                        record_battery_outcome(
                            path,
                            str(battery_outcomes[path].get("tier", "example")),
                            "committed",
                        )
                for path in committed_refrozen:
                    if battery_outcomes.get(path, {}).get("state") == "verification-pending":
                        record_battery_outcome(
                            path,
                            str(battery_outcomes[path].get("tier", "example")),
                            "verified",
                        )
            _progress_finish(progress)
            test_cost = cost.summary_dict()
            merged_cost = merged_operation_cost(build_cost, test_cost)
            return TargetTestReport(
                language="ts",
                generated=frozenset(generated),
                skipped=frozenset(skipped),
                refrozen=frozenset(refrozen),
                failed={
                    "typecheck": (
                        TargetDiagnostic(
                            code=(
                                "JAUNT_TS_TEST_INFRASTRUCTURE"
                                if preflight_infrastructure
                                else "JAUNT_TS_TEST_TYPECHECK"
                            ),
                            message=(
                                "The protected TypeScript typecheck runner was unavailable; "
                                "validated candidates were retained in the response cache."
                                if preflight_infrastructure
                                else (
                                    "A committed TypeScript battery failed baseline "
                                    "typechecking; unrelated validated candidates were retained "
                                    "in the response cache."
                                    if preflight_baseline_failure
                                    else "Generated TypeScript tests failed overlay typechecking."
                                )
                            ),
                        ),
                    )
                },
                runner={
                    **preflight,
                    "cost": merged_cost,
                    "test_cost": test_cost,
                    "build": build_metadata,
                    "jobs": effective_jobs,
                    "partial_landing": {
                        "accepted": accepted_paths,
                        "rejected": rejected_paths,
                        "retained": retained_paths,
                        "committed": partial_committed,
                        "isolation": isolation,
                        "runner": partial_runner,
                    },
                    "batteries": [
                        {"path": path, **battery_outcomes[path]}
                        for path in sorted(battery_outcomes)
                    ],
                    **(
                        {
                            "cache": {
                                "hits": response_cache.hits,
                                "misses": response_cache.misses,
                            }
                        }
                        if response_cache is not None
                        else {}
                    ),
                },
                exit_code=3,
            )
        stage_validated_batteries()
        runner = (
            {"ok": True, "skipped": True}
            if no_run or not files
            else await _run_test_batches(
                client,
                root,
                config,
                analysis.workspace,
                files=files,
                explicit_owners=test_owners,
                overlays=overlays,
                redact_derived=not no_redact_derived,
                config_snapshot=pinned_vitest_config_snapshot,
            )
        )

    files_committed = False
    repair_writes: tuple[_Write, ...] = ()
    repair_output_preconditions: dict[str, str] = {}
    delayed_repair_journal_events: tuple[JournalEvent, ...] = ()

    def publish_test_files(transaction: _RepairFileTransaction | None = None) -> None:
        if files_committed:
            return
        test_writes = tuple(
            _Write(
                path=path,
                content=source,
                kind="test",
                module_id=f"ts-test:{path}",
            )
            for path, source in overlays.items()
        )
        all_writes = (*repair_writes, *test_writes)
        expected_inputs = {
            **_input_hashes(analysis.contracts),
            **output_preconditions,
            **repair_output_preconditions,
        }

        def pre_commit_guard() -> None:
            _verify_test_commit_environment(
                root,
                client,
                initialized,
                pinned_runner_fingerprint,
                vitest_config=target_config.vitest_config,
                config_closure=pinned_vitest_config_closure,
            )

        def commit_seal() -> None:
            _seal_test_commit_environment(
                root,
                client,
                initialized,
                pinned_runner_fingerprint,
                vitest_config=target_config.vitest_config,
                config_closure=pinned_vitest_config_closure,
            )

        if transaction is None:
            atomic_write_manifest(
                root,
                all_writes,
                expected_inputs=expected_inputs,
                pre_commit_guard=pre_commit_guard,
                commit_seal=commit_seal,
            )
        else:
            transaction.publish(
                all_writes,
                expected_inputs=expected_inputs,
                pre_commit_guard=pre_commit_guard,
                commit_seal=commit_seal,
            )

    def finalize_test_files(*, delay_repair_journal: bool) -> None:
        nonlocal delayed_repair_journal_events, files_committed
        if files_committed:
            return
        generated.update(planned_generated)
        refrozen.update(planned_refrozen)
        for path in overlays:
            _clear_rejected_test_candidate(
                root,
                path,
                expected_token=rejected_test_tokens.get(path),
            )
        if repair_writes:
            events = tuple(
                JournalEvent("build", module_id, "TypeScript test repair validated")
                for module_id in sorted({write.module_id for write in repair_writes})
            )
            if delay_repair_journal:
                delayed_repair_journal_events = events
            else:
                append_events(root, events)
        files_committed = True

    def commit_test_outputs() -> None:
        publish_test_files()
        finalize_test_files(delay_repair_journal=False)

    initial_runner = runner
    repair_exit_code = 0

    def runner_failed_verified_battery(result: Mapping[str, Any]) -> bool:
        failed_paths = set(_failed_runner_test_paths(result))
        return bool(verified_paths) and (
            not failed_paths or bool(failed_paths.intersection(verified_paths))
        )

    if (
        not no_build
        and not no_run
        and files
        and not bool(runner.get("ok", False))
        and _runner_allows_implementation_repair(runner)
        and not runner_failed_verified_battery(runner)
    ):
        repair_targets = _repair_module_ids(
            runner,
            targets_by_file=repair_targets_by_file,
            modules=modules,
            requested_targets=target_ids,
        )
        if repair_targets:
            route_artifacts = {
                str(path): (kind, _module_id(module))
                for module in modules.values()
                for key, kind in (
                    ("facadePath", "facade"),
                    ("apiMirrorPath", "api-mirror"),
                    ("implementationPath", "implementation"),
                    ("sidecarPath", "sidecar"),
                )
                if isinstance(path := module.get(key), str)
            }
            repair_output_preconditions = {
                path: _path_hash(_safe_path(root, path)) or MISSING_INPUT
                for path in route_artifacts
            }
            hidden_batteries = tuple(
                relative
                for relative in files
                if not (
                    (source := overlays.get(relative)) is not None
                    and _is_reviewable_example_battery(relative, source)
                )
                and not (
                    source is None
                    and (path := _safe_path(root, relative)).is_file()
                    and _is_reviewable_example_battery(relative, path.read_text(encoding="utf-8"))
                )
            )
            repair_workspace_overlays = dict(overlays)
            for relative, source in pinned_vitest_config_overlays.items():
                previous = repair_workspace_overlays.setdefault(relative, source)
                if previous != source:
                    raise JauntGenerationError(
                        "TypeScript repair overlays conflict with captured Vitest input: "
                        + relative
                    )
            missing_config_inputs = tuple(
                relative
                for relative, digest in pinned_vitest_config_closure.items()
                if digest == MISSING_INPUT and relative not in repair_workspace_overlays
            )
            with _isolated_test_repair_workspace(
                root,
                files,
                repair_workspace_overlays,
                deleted_files=missing_config_inputs,
            ) as repair_root:
                repair_phase_cost = phase_cost_tracker()
                _progress_reset(progress)
                repair = await run_build(
                    repair_root,
                    config,
                    target_ids=repair_targets,
                    force=True,
                    generator=model_backend(),
                    cost_tracker=repair_phase_cost,
                    response_cache=ResponseCache(repair_root / ".jaunt" / "cache"),
                    progress=progress,
                    finish_progress=False,
                    worker_factory=worker_factory,
                    jobs=jobs,
                    max_attempts=1,
                    build_instructions=build_instructions,
                    semantic_gate_enabled=semantic_gate_enabled,
                    semantic_gate_exec=semantic_gate_exec,
                    ephemeral_prompt=_implementation_repair_feedback(runner),
                    repo_map_enabled=repo_map_enabled,
                    repo_map_block_override=repo_map_block_override,
                    auto_skills_enabled=False,
                    builtin_skill_names=effective_builtin_skills,
                    validate_committed_batteries=False,
                )
                prepared_paths = {
                    str(path)
                    for path in repair.metadata.get("artifacts", ())
                    if isinstance(path, str)
                }
                prepared_paths.update(
                    path
                    for path in route_artifacts
                    if (
                        _safe_path(repair_root, path).read_bytes()
                        if _safe_path(repair_root, path).is_file()
                        else None
                    )
                    != (
                        _safe_path(root, path).read_bytes()
                        if _safe_path(root, path).is_file()
                        else None
                    )
                )
                repair_writes = tuple(
                    _Write(
                        path=path,
                        content=_safe_path(repair_root, path).read_text(encoding="utf-8"),
                        kind=route_artifacts[path][0],
                        module_id=route_artifacts[path][1],
                    )
                    for path in sorted(prepared_paths)
                    if path in route_artifacts and _safe_path(repair_root, path).is_file()
                )
                raw_repair_cost = repair.metadata.get("cost")
                if isinstance(raw_repair_cost, Mapping):
                    repair_cost = raw_repair_cost
                repair_metadata = {
                    "attempted": True,
                    "targets": list(repair_targets),
                    "ok": repair.exit_code == 0,
                    "build": _build_phase_metadata(repair),
                    "initial_runner": _redact_runner_result(
                        initial_runner,
                        enabled=not no_redact_derived,
                    ),
                    "held_out_sources_hidden": list(hidden_batteries),
                    "reran": False,
                }
                if repair.exit_code:
                    failed.update(repair.failed)
                    repair_exit_code = repair.exit_code
                else:
                    repair_overlays = {write.path: write.content or "" for write in repair_writes}
                    async with worker_session(
                        root,
                        config,
                        worker_factory=worker_factory,
                    ) as (repair_client, repair_initialized):
                        repair_analysis = await analyze(repair_client, repair_initialized)
                        runner = await _run_test_batches(
                            repair_client,
                            root,
                            config,
                            repair_analysis.workspace,
                            files=files,
                            explicit_owners=test_owners,
                            overlays={**repair_overlays, **overlays},
                            redact_derived=not no_redact_derived,
                            config_snapshot=pinned_vitest_config_snapshot,
                        )
                    repair_metadata = {**repair_metadata, "reran": True}
                    if bool(runner.get("ok", False)):
                        commit_paths = [
                            *(write.path for write in repair_writes),
                            *overlays,
                        ]
                        with _preserve_managed_files(root, commit_paths) as transaction:
                            publish_test_files(transaction)
                            transaction.commit()
                        finalize_test_files(delay_repair_journal=True)
                        # This is auxiliary provenance, not part of the repair's
                        # rollback domain. Emit it only after the marker is
                        # durably retired and the publication lease is released.
                        # A crash or journal I/O error may omit the line, but can
                        # never leave a validation claim for rolled-back bytes.
                        if delayed_repair_journal_events:
                            with contextlib.suppress(OSError):
                                append_events(root, delayed_repair_journal_events)

    should_reject_final_candidates = no_build or not _runner_allows_implementation_repair(runner)
    if not no_run and not bool(runner.get("ok", False)) and should_reject_final_candidates:
        failed_cache_paths = _runner_candidate_rejection_paths(
            runner,
            tuple(cached_battery_responses),
        )
        reject_batteries(
            failed_cache_paths,
            {
                path: (
                    "The final protected Vitest run rejected this battery; "
                    "its cached response was removed.",
                )
                for path in failed_cache_paths
            },
        )

    exit_code = repair_exit_code or (0 if bool(runner.get("ok", False)) else 4)
    if exit_code:
        failed["vitest"] = (
            TargetDiagnostic(code="JAUNT_TS_TEST_FAILED", message="The Vitest battery failed."),
        )
    else:
        commit_test_outputs()
    test_cost = cost.summary_dict()
    merged_cost = merged_operation_cost(build_cost, test_cost, repair_cost)
    runner_metadata: dict[str, Any] = {
        **runner,
        "cost": merged_cost,
        "test_cost": test_cost,
        "build": build_metadata,
        "jobs": effective_jobs,
        "batteries": [
            {"path": path, **battery_outcomes[path]} for path in sorted(battery_outcomes)
        ],
        **(
            {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
            if response_cache is not None
            else {}
        ),
        **({"npm_skills": npm_skill_metadata} if npm_skill_metadata else {}),
    }
    if repair_metadata is not None:
        runner_metadata["repair"] = repair_metadata
    _progress_finish(progress)
    return TargetTestReport(
        language="ts",
        generated=frozenset(generated),
        skipped=frozenset(skipped),
        refrozen=frozenset(refrozen),
        failed=failed,
        runner=runner_metadata,
        exit_code=exit_code,
    )


__all__ = ["run_test"]
