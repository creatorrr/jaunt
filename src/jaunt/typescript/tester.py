"""TypeScript test generation and disposable Vitest runner orchestration."""

from __future__ import annotations

import asyncio
import base64
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
from typing import Any, Iterator
from urllib.parse import unquote

from jaunt.config import JauntConfig
from jaunt.cache import ResponseCache
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError
from jaunt.generate.base import GenerationRequest, GeneratorBackend
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
    WorkerFactory,
    _default_backend,
    _input_hashes,
    _module_id,
    _path_hash,
    _prompt_text,
    _progress_advance,
    _progress_finish,
    _progress_phase,
    _progress_reset,
    _safe_path,
    _sha256,
    _target,
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
    proven_previous_target_api_digests,
    target_api_digest,
)
from jaunt.typescript.worker import TypeScriptWorkerError, worker_environment

_DEFAULT_RUNNER_TIMEOUT = 300.0
_RUNNER_PROTOCOL = "jaunt-ts-test-runner/1"
_RUNNER_ENTRY = "dist/test/runner.js"
# Keep this list to the runtime closure of ``runner.js``. In particular,
# ``heldout.js`` is part of the security boundary: changing its leak checks must
# invalidate/refreeze every generated battery before those tests run again.
_RUNNER_RUNTIME_FILES = (
    _RUNNER_ENTRY,
    "dist/test/permission_guard.cjs",
    "dist/test/reporter.js",
    "dist/test/heldout.js",
    "dist/analyzer/artifacts.js",
    "dist/analyzer/diagnostics.js",
    "dist/analyzer/canonical.js",
    "dist/protocol/errors.js",
)
_TEST_SPEC_RE = re.compile(r"\.jaunt-test\.(?:ts|tsx)$")
_GENERATED_TEST_HEADER = "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`."
_TEST_PROVENANCE_FIELDS = (
    "test_spec_digest",
    "target_api_digest",
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


def _installed_test_dependency_version(root: Path, owner: Path, package: str) -> str | None:
    package_path = Path(*package.split("/"))
    current = owner.resolve()
    root = root.resolve()
    while current == root or current.is_relative_to(root):
        candidate = current / "node_modules" / package_path / "package.json"
        try:
            parsed = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, Mapping) and isinstance(parsed.get("version"), str):
            return str(parsed["version"])
        if current == root:
            break
        current = current.parent
    return None


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


def _local_config_closure(root: Path, initial: str) -> Mapping[str, str]:
    """Hash a Vitest config and statically referenced local setup/config files."""

    pending = [_safe_path(root, initial)]
    seen: set[Path] = set()
    hashes: dict[str, str] = {}
    while pending:
        path = pending.pop()
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        relative = path.relative_to(root).as_posix()
        source = path.read_text(encoding="utf-8")
        hashes[relative] = _sha256(source.encode("utf-8"))
        specifiers = re.findall(r"[\"'](?P<path>\.{1,2}/[^\"']+)[\"']", source)
        for specifier in specifiers:
            base = path.parent / specifier
            candidates = (
                base,
                *(base.with_suffix(extension) for extension in (".ts", ".tsx", ".mts", ".cts")),
                *(base.with_suffix(extension) for extension in (".js", ".mjs", ".cjs", ".json")),
                *(base / f"index{extension}" for extension in (".ts", ".tsx", ".js", ".json")),
                root / specifier,
            )
            for candidate in candidates:
                try:
                    relative_candidate = candidate.resolve().relative_to(root)
                except (ValueError, OSError):
                    continue
                confined = _safe_path(root, relative_candidate.as_posix())
                if confined.is_file():
                    pending.append(confined)
                    break
    return dict(sorted(hashes.items()))


def _fixture_fingerprint(root: Path, config: JauntConfig) -> str:
    target = _target(config)
    fixtures: dict[str, str] = {}
    for entry in target.test_roots:
        roots = (
            [path for path in root.glob(entry) if path.is_dir()]
            if any(character in entry for character in "*?[")
            else [_safe_path(root, entry)]
        )
        for test_root in roots:
            if not test_root.is_dir():
                continue
            for pattern in ("**/fixtures.ts", "**/fixtures.tsx"):
                for path in test_root.glob(pattern):
                    fixtures[path.relative_to(root).as_posix()] = _semantic_test_spec_digest(
                        path.read_text(encoding="utf-8")
                    )
    return _canonical_digest(fixtures)


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

    required = (
        _RUNNER_RUNTIME_FILES
        if package_managed
        else tuple(
            relative for relative in _RUNNER_RUNTIME_FILES if (package_root / relative).is_file()
        )
    )

    def digest_files() -> dict[str, str]:
        return {
            relative: _stable_runner_digest(package_root / relative, package_root=package_root)
            for relative in required
        }

    files = digest_files()
    # A second full read catches a runner/support replacement between individual
    # reads, while remaining path-independent for source checkouts and installs.
    if files != digest_files():
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
    roots = _tool_search_roots(root, client)
    return _canonical_digest(
        {
            "protocol": _RUNNER_PROTOCOL,
            "packageVersion": package_version or _read_package_version(roots, "@usejaunt/ts"),
            "testRunnerExport": runner_export,
            "workerVersion": str(getattr(initialized, "worker_version", "unknown")),
            "typescriptVersion": str(getattr(initialized, "typescript_version", "unknown")),
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
    config_digest: object = (
        _local_config_closure(root, target.vitest_config) if target.vitest_config else "default"
    )
    request = _test_request(
        root,
        config,
        test_spec,
        modules,
        tier=tier,
        builtin_skill_names=builtin_skill_names,
    )
    values = {
        "test_spec_digest": _semantic_test_spec_digest(source),
        "target_api_digest": target_api_digest(selected),
        "vitest_fingerprint": _canonical_digest(
            {
                "runner": target.test_runner,
                "configPath": target.vitest_config,
                "configDigest": config_digest,
                "version": _read_package_version(roots, "vitest"),
                "fixtures": _fixture_fingerprint(root, config),
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
        "runner_fingerprint": _runner_fingerprint(root, client, initialized),
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

    mismatches = {key for key, value in provenance.items() if metadata.get(key) != value}
    if not mismatches:
        return "skip", source

    allowed_tooling = set(_TEST_REHEADER_FINGERPRINTS)
    api_proof_matches = metadata.get("target_api_digest") in proven_previous_api_digests
    if api_proof_matches:
        allowed_tooling.add("target_api_digest")
    allowed = allowed_tooling | {"battery_fingerprint"}
    if (
        not mismatches.intersection(allowed_tooling)
        or "battery_fingerprint" not in mismatches
        or not mismatches.issubset(allowed)
    ):
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
        cursor = expression_start
        depth = 1
        previous_significant = ""
        while cursor < len(source) and depth:
            next_cursor = _skip_typescript_trivia(source, cursor)
            if next_cursor != cursor:
                cursor = next_cursor
                continue
            character = source[cursor]
            if character in {'"', "'"}:
                parsed = _read_typescript_string_literal(source, cursor)
                cursor = parsed[1] if parsed is not None else cursor + 1
                previous_significant = "literal"
                continue
            if character == "`":
                cursor, nested = _typescript_template_expressions(source, cursor)
                expressions.extend(nested)
                previous_significant = "literal"
                continue
            if character == "/" and (
                not previous_significant
                or previous_significant in "=(:,[!&|?{;"
                or previous_significant in {"case", "return", "throw"}
            ):
                cursor = _skip_typescript_regex(source, cursor)
                previous_significant = "literal"
                continue
            if character == "{":
                depth += 1
            elif character == "}":
                depth -= 1
                if depth == 0:
                    expressions.append(source[expression_start:cursor])
                    cursor += 1
                    break
            if character.isalpha() or character in "_$":
                end = cursor + 1
                while end < len(source) and (source[end].isalnum() or source[end] in "_$"):
                    end += 1
                previous_significant = source[cursor:end]
                cursor = end
            else:
                previous_significant = character
                cursor += 1
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
            continue
        if character == "`":
            cursor, expressions = _typescript_template_expressions(source, cursor)
            for expression in expressions:
                references.extend(_static_typescript_module_references(expression))
            previous_significant = "literal"
            continue
        if character == "/" and (
            not previous_significant
            or previous_significant in "=(:,[!&|?{;"
            or previous_significant in {"case", "return", "throw"}
        ):
            cursor = _skip_typescript_regex(source, cursor)
            previous_significant = "literal"
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
            previous_significant = identifier
            cursor = end
            continue
        previous_significant = character
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


def _fixture_for_path(root: Path, relative: str) -> tuple[str, str] | None:
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
            return path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")
        if current == root:
            break
        current = current.parent
    return None


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


def _test_request(
    root: Path,
    config: JauntConfig,
    test_spec: Mapping[str, Any],
    modules: Mapping[str, Mapping[str, Any]],
    *,
    tier: str = "example",
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
            selected[0]
            if len(selected) == 1
            else {
                "targets": [
                    {**dict(module), "facadeSpecifier": specifier}
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
    fixture_path = ""
    fixture_specifier = ""
    if fixture is not None:
        fixture_path, fixture_source = fixture
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
            if not path.is_file():
                diagnostics.append(
                    TargetDiagnostic(
                        code="JAUNT_TS_TEST_BATTERY_MISSING",
                        message=(
                            f"The {tier} TypeScript battery for {source_path} is missing; "
                            "run `jaunt test --language ts`."
                        ),
                        path=relative,
                        data={"scope": "magic", "source": source_path, "tier": tier},
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
            expected = _test_provenance(
                root,
                config,
                test_spec,
                modules,
                client,
                initialized,
                tier=tier,
            )
            mismatches: list[str] = []
            if metadata is None:
                mismatches.append("provenance")
            else:
                if metadata.get("tier") != tier:
                    mismatches.append("tier")
                if metadata.get("source") != source_path:
                    mismatches.append("source")
                mismatches.extend(
                    key for key, value in expected.items() if metadata.get(key) != value
                )
                rendered_body_digest = _sha256(
                    (_strip_test_header(source).rstrip() + "\n").encode("utf-8")
                )
                if metadata.get("body_digest") != rendered_body_digest:
                    mismatches.append("body_digest")
            if mismatches:
                diagnostics.append(
                    TargetDiagnostic(
                        code="JAUNT_TS_TEST_BATTERY_STALE",
                        message=(
                            f"The {tier} TypeScript battery for {source_path} is stale "
                            f"({', '.join(sorted(set(mismatches)))}); run `jaunt test "
                            "--language ts`."
                        ),
                        path=relative,
                        data={
                            "scope": "magic",
                            "source": source_path,
                            "tier": tier,
                            "mismatches": tuple(sorted(set(mismatches))),
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
    __slots__ = ("add_paths", "commit")

    def __init__(
        self,
        *,
        add_paths: Callable[[Sequence[str]], None],
        commit: Callable[[], None],
    ) -> None:
        self.add_paths = add_paths
        self.commit = commit


def _recover_pending_test_repairs(root: Path) -> tuple[str, ...]:
    """Restore durable pre-repair bytes left by a terminated Jaunt process."""

    root = root.resolve()
    directory = root / ".jaunt" / "transactions"
    if not directory.is_dir():
        return ()
    restored: list[str] = []
    for manifest in sorted(directory.glob("test-repair-*.json")):
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise JauntConfigError(f"Invalid TypeScript test-repair marker: {manifest}") from error
        if not isinstance(payload, Mapping) or payload.get("scheme") != "jaunt-ts-test-repair/1":
            raise JauntConfigError(f"Invalid TypeScript test-repair marker: {manifest}")
        owner_pid = payload.get("ownerPid")
        if not isinstance(owner_pid, int) or owner_pid < 1:
            raise JauntConfigError(f"TypeScript test-repair marker has no owner PID: {manifest}")
        if owner_pid == os.getpid():
            continue
        try:
            os.kill(owner_pid, 0)
        except ProcessLookupError:
            pass
        except PermissionError as error:
            raise JauntConfigError(
                f"TypeScript test repair is still owned by process {owner_pid}"
            ) from error
        else:
            raise JauntConfigError(f"TypeScript test repair is still owned by process {owner_pid}")
        snapshots = payload.get("snapshots")
        if not isinstance(snapshots, list):
            raise JauntConfigError(f"TypeScript test-repair marker has no snapshots: {manifest}")
        restored_paths: set[str] = set()
        for snapshot in snapshots:
            if not isinstance(snapshot, Mapping) or not isinstance(snapshot.get("path"), str):
                raise JauntConfigError(f"Invalid snapshot in test-repair marker: {manifest}")
            relative = str(snapshot["path"])
            path = _safe_path(root, relative)
            encoded = snapshot.get("content")
            mode = snapshot.get("mode")
            if encoded is None:
                path.unlink(missing_ok=True)
            elif isinstance(encoded, str) and isinstance(mode, int):
                try:
                    content = base64.b64decode(encoded, validate=True)
                except ValueError as error:
                    raise JauntConfigError(
                        f"Invalid backup bytes in test-repair marker: {manifest}"
                    ) from error
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{path.name}.jaunt-recover-", dir=path.parent
                )
                temporary = Path(temporary_name)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(temporary, mode)
                    os.replace(temporary, path)
                finally:
                    temporary.unlink(missing_ok=True)
            else:
                raise JauntConfigError(f"Invalid snapshot in test-repair marker: {manifest}")
            restored_paths.add(relative)
            restored.append(relative)
        for transaction in directory.glob("ts-*.json"):
            try:
                value = json.loads(transaction.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            writes = value.get("writes") if isinstance(value, Mapping) else None
            paths = {
                str(write.get("path"))
                for write in writes or []
                if isinstance(write, Mapping) and isinstance(write.get("path"), str)
            }
            if paths and paths <= restored_paths:
                transaction.unlink(missing_ok=True)
        manifest.unlink(missing_ok=True)
    return tuple(sorted(set(restored)))


@contextmanager
def _preserve_managed_files(
    root: Path,
    paths: Sequence[str],
) -> Iterator[_RepairFileTransaction]:
    """Rollback a bounded repair's managed files unless its final battery passes."""

    root = root.resolve()
    originals: dict[Path, tuple[bytes | None, int | None]] = {}
    committed = False
    manifest = root / ".jaunt" / "transactions" / f"test-repair-{uuid.uuid4().hex}.json"

    def replace(path: Path, content: bytes, mode: int) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.jaunt-rollback-",
            dir=path.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                temporary.unlink()

    def fsync_directory(path: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        try:
            descriptor = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        except OSError:
            pass
        finally:
            os.close(descriptor)

    def write_manifest() -> None:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "scheme": "jaunt-ts-test-repair/1",
            "ownerPid": os.getpid(),
            "snapshots": [
                {
                    "path": path.relative_to(root).as_posix(),
                    "content": (
                        base64.b64encode(content).decode("ascii") if content is not None else None
                    ),
                    "mode": mode,
                }
                for path, (content, mode) in sorted(
                    originals.items(), key=lambda item: item[0].as_posix()
                )
            ],
        }
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{manifest.name}.", suffix=".tmp", dir=manifest.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, manifest)
            fsync_directory(manifest.parent)
        finally:
            temporary.unlink(missing_ok=True)

    def add_paths(values: Sequence[str]) -> None:
        for relative in sorted(set(values)):
            path = _safe_path(root, relative)
            if path in originals:
                continue
            if path.is_symlink():
                raise JauntConfigError(f"Refusing to repair a managed symlink: {relative}")
            if path.exists() and not path.is_file():
                raise JauntConfigError(f"Managed repair path is not a file: {relative}")
            originals[path] = (
                path.read_bytes() if path.is_file() else None,
                stat.S_IMODE(path.stat().st_mode) if path.is_file() else None,
            )
        write_manifest()

    def commit() -> None:
        nonlocal committed
        manifest.unlink(missing_ok=True)
        fsync_directory(manifest.parent)
        committed = True

    add_paths(paths)
    try:
        yield _RepairFileTransaction(add_paths=add_paths, commit=commit)
    finally:
        if not committed:
            for path, (content, mode) in originals.items():
                if content is None:
                    path.unlink(missing_ok=True)
                    continue
                replace(path, content, mode or 0o644)
            manifest.unlink(missing_ok=True)
            fsync_directory(manifest.parent)


@contextmanager
def _isolated_test_workspace(
    root: Path,
    files: Sequence[str],
    overlays: Mapping[str, str],
    *,
    tier: str,
) -> Iterator[Path]:
    """Copy one test tier without leaving links back into the source workspace.

    Both implementation repair and protected Vitest execution use this view.  A
    symlink to the original ``node_modules`` is not isolation: resolving
    ``node_modules/../tests`` follows the physical parent and reaches held-out
    batteries.  Internal links are therefore remapped into the copy and external
    links are materialized.  Generated batteries are staged only from the exact
    selected bytes after the ordinary tree has been copied.
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

        def copy_symlink(source: Path, destination: Path, active: frozenset[Path]) -> None:
            try:
                physical = source.resolve(strict=True)
            except OSError:
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
            else:
                store = external_package_store(physical)
                if store is not None:
                    assert_external_store_safe(store)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.symlink_to(physical, target_is_directory=physical.is_dir())
                    return
            copy_entry(physical, destination, active)

        def copy_entry(source: Path, destination: Path, active: frozenset[Path]) -> None:
            if source.name in skipped_directories:
                return
            if source_is_generated_battery(source) or source_is_snapshot(source):
                return
            if source.is_symlink():
                copy_symlink(source, destination, active)
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
                    copy_entry(entry, destination / entry.name, nested_active)
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

        # Non-test overlays (notably an implementation repair candidate) belong
        # to both tier views.  Battery overlays are admitted only via the exact
        # selected-file loop below.
        for relative, source in overlays.items():
            if Path(relative).name.endswith(generated_battery_suffixes):
                continue
            target = _safe_path(temporary, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source, encoding="utf-8")

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
            target.write_text(source, encoding="utf-8")

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
) -> Iterator[Path]:
    """Copy a model-safe workspace with examples visible and held-out tests absent."""

    with _isolated_test_workspace(root, files, overlays, tier="example") as temporary:
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


def _failed_runner_test_paths(result: Mapping[str, Any]) -> tuple[str, ...]:
    """Collect public failed-test paths from an aggregated protected-runner result."""

    paths: set[str] = set()

    def visit(value: object) -> None:
        if not isinstance(value, Mapping):
            return
        tests = value.get("tests", ())
        if isinstance(tests, list):
            for item in tests:
                if not isinstance(item, Mapping) or item.get("status") != "failed":
                    continue
                path = item.get("file")
                if isinstance(path, str) and path:
                    paths.add(path.replace("\\", "/").removeprefix("./"))
        batches = value.get("batches", {})
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
) -> Mapping[str, Any]:
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
        overlays=overlays,
    )
    test_overlays = {
        path: source
        for path, source in (overlays or {}).items()
        if path.endswith((".test.ts", ".test.tsx"))
    }
    shared_overlays = {
        path: source for path, source in (overlays or {}).items() if path not in test_overlays
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
                redact_derived=redact_derived,
                typecheck_only=True,
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


def _runner_validation_errors(result: Mapping[str, Any]) -> list[str]:
    """Render protected-runner diagnostics as actionable generator feedback."""

    errors: list[str] = []
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

    root = root.resolve()
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

    def phase_cost_tracker() -> CostTracker:
        if cost_tracker is None:
            return CostTracker(max_cost=config.llm.max_cost_per_build)
        child = getattr(cost_tracker, "child", None)
        return child() if callable(child) else cost_tracker

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
            )
        else:
            build = await run_build_in_session(
                root,
                config,
                *worker_session_override,
                target_ids=target_ids,
                force=force,
                generator=generator,
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

    backend = generator or _default_backend(config)
    cost = phase_cost_tracker()
    overlays: dict[str, str] = {}
    planned_generated: set[str] = set()
    planned_refrozen: set[str] = set()
    battery_outcomes: dict[str, dict[str, Any]] = {}
    pending_cache_writes: list[tuple[GenerationRequest, Any, str, str, str]] = []
    cached_battery_responses: dict[str, tuple[GenerationRequest, str, str]] = {}
    output_preconditions: dict[str, str] = {}
    repair_targets_by_file: dict[str, tuple[str, ...]] = {}
    modules: dict[str, Mapping[str, Any]] = {}
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
                backend,
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
        modules = {_module_id(module): module for module in analysis.modules}
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

        generation_work: list[
            tuple[
                str,
                GenerationRequest,
                Mapping[str, Any],
                str,
                str,
            ]
        ] = []
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
            )
            selected_modules = _selected_test_modules(test_spec, modules)
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
            )
            if action == "skip":
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
            key = f"{spec_path}#{tier}"
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
                )
                if bool(checked.get("ok", False)):
                    return []
                return _runner_validation_errors(checked)

            cache_fingerprint = str(provenance["battery_fingerprint"])
            cache_for_request = None if force else response_cache
            validated_request = replace(request, validator=validate_candidate)
            async with semaphore:
                _progress_phase(progress, request.target_path, "generating", tier)
                result = await generate_request_cached(
                    backend,
                    validated_request,
                    max_attempts=max_attempts,
                    generation_fingerprint=cache_fingerprint,
                    response_cache=cache_for_request,
                    cost_tracker=cost,
                    usage_label=key,
                    progress=lambda stage, detail, path=request.target_path: _progress_phase(
                        progress, path, stage, detail
                    ),
                    store=False,
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
                    failed[key] = tuple(
                        TargetDiagnostic(
                            code=(
                                "JAUNT_TS_TEST_INFRASTRUCTURE"
                                if result.infrastructure_exhausted
                                else "JAUNT_TS_TEST_GENERATION"
                            ),
                            message=error,
                        )
                        for error in result.errors or ["The generator returned no test source"]
                    )
                    record_battery_outcome(
                        request.target_path,
                        tier,
                        "infrastructure-failed" if result.infrastructure_exhausted else "failed",
                        result=result,
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
            unfinished = [task for task in tasks if not task.done()]
            for task in unfinished:
                task.cancel()
            if unfinished:
                await asyncio.gather(*unfinished, return_exceptions=True)

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

        async def isolate_valid_overlays() -> tuple[
            tuple[str, ...],
            tuple[str, ...],
            tuple[str, ...],
            Mapping[str, Any],
            Mapping[str, tuple[str, ...]],
        ]:
            """Find a deterministic maximal overlay subset that validates together."""

            candidate_paths = tuple(sorted(overlays))
            baseline_files = tuple(path for path in files if path not in overlays)
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
                )
            if not bool(baseline_result.get("ok", False)):
                reasons = tuple(_runner_validation_errors(baseline_result))
                return (
                    (),
                    candidate_paths,
                    baseline_files,
                    {"baseline": baseline_result, "candidates": []},
                    {path: reasons for path in candidate_paths},
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
                )
                valid = bool(checked.get("ok", False))
                candidate_results.append({"path": path, "ok": valid})
                if valid:
                    accepted.append(path)
                    continue
                rejected.append(path)
                rejection_reasons[path] = tuple(_runner_validation_errors(checked))
            accepted_files = tuple(sorted({*baseline_files, *accepted}))
            return (
                tuple(accepted),
                tuple(rejected),
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
                if cached is not None:
                    request, fingerprint, source = cached
                    battery_outcomes[path]["cache_evicted"] = discard_cached_generation(
                        response_cache,
                        backend,
                        request,
                        generation_fingerprint=fingerprint,
                        expected_source=source,
                    )

        if failed:
            stage_preflight: Mapping[str, Any] = {"ok": True, "skipped": True}
            stage_isolation: Mapping[str, Any] | None = None
            if pending_cache_writes and files:
                stage_preflight = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=files,
                    explicit_owners=test_owners,
                    overlays=overlays,
                    redact_derived=not no_redact_derived,
                    typecheck_only=True,
                )
            if bool(stage_preflight.get("ok", False)):
                stage_validated_batteries()
            else:
                (
                    accepted_paths,
                    rejected_paths,
                    _accepted_files,
                    stage_isolation,
                    reasons,
                ) = await isolate_valid_overlays()
                accepted = set(accepted_paths)
                pending_cache_writes[:] = [
                    item for item in pending_cache_writes if item[3] in accepted
                ]
                stage_validated_batteries()
                reject_batteries(rejected_paths, reasons)
            _progress_finish(progress)
            test_cost = cost.summary_dict()
            merged_cost = (
                cost_tracker.summary_dict()
                if cost_tracker is not None and not callable(getattr(cost_tracker, "child", None))
                else _cost_summary(build_cost, test_cost)
            )
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
            )
        if not bool(preflight.get("ok", False)):
            (
                accepted_paths,
                rejected_paths,
                accepted_files,
                isolation,
                reasons,
            ) = await isolate_valid_overlays()
            accepted = set(accepted_paths)
            accepted_overlays = {path: overlays[path] for path in accepted_paths}
            pending_cache_writes[:] = [item for item in pending_cache_writes if item[3] in accepted]
            stage_validated_batteries()
            reject_batteries(rejected_paths, reasons)

            partial_runner: Mapping[str, Any] = {"ok": True, "skipped": True}
            if not no_run and accepted_overlays and accepted_files:
                partial_runner = await _run_test_batches(
                    client,
                    root,
                    config,
                    analysis.workspace,
                    files=accepted_files,
                    explicit_owners=test_owners,
                    overlays=accepted_overlays,
                    redact_derived=not no_redact_derived,
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
                )
                generated.update(committed_generated)
                refrozen.update(committed_refrozen)
            _progress_finish(progress)
            test_cost = cost.summary_dict()
            merged_cost = (
                cost_tracker.summary_dict()
                if cost_tracker is not None and not callable(getattr(cost_tracker, "child", None))
                else _cost_summary(build_cost, test_cost)
            )
            return TargetTestReport(
                language="ts",
                generated=frozenset(generated),
                skipped=frozenset(skipped),
                refrozen=frozenset(refrozen),
                failed={
                    "typecheck": (
                        TargetDiagnostic(
                            code="JAUNT_TS_TEST_TYPECHECK",
                            message="Generated TypeScript tests failed overlay typechecking.",
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
            )
        )

    files_committed = False
    repair_writes: tuple[_Write, ...] = ()
    repair_output_preconditions: dict[str, str] = {}

    def commit_test_files() -> None:
        nonlocal files_committed
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
        atomic_write_manifest(
            root,
            (*repair_writes, *test_writes),
            expected_inputs={
                **_input_hashes(analysis.contracts),
                **output_preconditions,
                **repair_output_preconditions,
            },
        )
        if repair_writes:
            append_events(
                root,
                [
                    JournalEvent("build", module_id, "TypeScript test repair validated")
                    for module_id in sorted({write.module_id for write in repair_writes})
                ],
            )
        generated.update(planned_generated)
        refrozen.update(planned_refrozen)
        files_committed = True

    def commit_test_outputs() -> None:
        commit_test_files()

    initial_runner = runner
    repair_exit_code = 0
    if (
        not no_build
        and not no_run
        and files
        and not bool(runner.get("ok", False))
        and _runner_allows_implementation_repair(runner)
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
            with _isolated_test_repair_workspace(root, files, overlays) as repair_root:
                repair_phase_cost = phase_cost_tracker()
                _progress_reset(progress)
                repair = await run_build(
                    repair_root,
                    config,
                    target_ids=repair_targets,
                    force=True,
                    generator=generator,
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
                        )
                    repair_metadata = {**repair_metadata, "reran": True}
                    if bool(runner.get("ok", False)):
                        commit_paths = [
                            *(write.path for write in repair_writes),
                            *overlays,
                            *(["JAUNT_LOG"] if (root / "JAUNT_LOG").is_file() else []),
                        ]
                        with _preserve_managed_files(root, commit_paths) as transaction:
                            commit_test_files()
                            transaction.commit()

    if (
        no_build
        and not no_run
        and not bool(runner.get("ok", False))
        and _runner_allows_implementation_repair(runner)
    ):
        failed_cache_paths = tuple(
            path for path in _failed_runner_test_paths(runner) if path in cached_battery_responses
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
    if cost_tracker is not None and not callable(getattr(cost_tracker, "child", None)):
        merged_cost = cost_tracker.summary_dict()
    else:
        merged_cost = _cost_summary(build_cost, test_cost, repair_cost)
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
