"""Secure lifecycle and JSONL client for the project-local TypeScript worker."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shutil
import signal
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

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


_WORKER_RUNTIME_SUFFIXES = frozenset({".js", ".cjs", ".mjs", ".json"})
_WORKER_DECLARATION_SUFFIXES = (".d.ts", ".d.cts", ".d.mts")
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


def _is_toolchain_identity_file(path: Path) -> bool:
    """Return whether a shipped file can affect worker or compiler behavior."""

    return path.suffix in _WORKER_RUNTIME_SUFFIXES or path.name.endswith(
        _WORKER_DECLARATION_SUFFIXES
    )


def _runtime_manifest_identity(manifest: Mapping[str, Any]) -> list[list[object]]:
    """Keep resolution semantics while ignoring unrelated manifest formatting/data."""

    def ordered(value: object) -> object:
        if isinstance(value, Mapping):
            # Conditional export key order is semantic in Node. Encode mappings
            # as ordered pairs before the outer identity JSON is key-sorted.
            return [[str(key), ordered(item)] for key, item in value.items()]
        if isinstance(value, list):
            return [ordered(item) for item in value]
        return value

    return [
        [field, ordered(manifest[field])] for field in _RUNTIME_MANIFEST_FIELDS if field in manifest
    ]


def _runtime_package_identity_files(package_root: Path) -> tuple[Path, ...]:
    """Enumerate one package's resolution/runtime closure without dependencies."""

    try:
        physical_root = package_root.resolve(strict=True)
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not resolve runtime package at {package_root}: {exc}"
        ) from exc
    if not physical_root.is_dir():
        raise TypeScriptWorkerError(f"Runtime package is not a directory: {package_root}")
    try:
        paths = {
            path.resolve(strict=True)
            for path in physical_root.rglob("*")
            if path.is_file()
            and path != physical_root / "package.json"
            and "node_modules" not in path.relative_to(physical_root).parts
            and _is_toolchain_identity_file(path)
        }
    except OSError as exc:
        raise TypeScriptWorkerError(
            f"Could not enumerate runtime package files under {package_root}: {exc}"
        ) from exc
    if any(path != physical_root and physical_root not in path.parents for path in paths):
        raise TypeScriptWorkerError(f"Runtime package file escapes its package: {package_root}")
    return tuple(sorted(paths, key=lambda path: path.relative_to(physical_root).as_posix()))


def runtime_package_identity(package_root: Path, *, expected_name: str | None = None) -> str:
    """Return a path-portable identity for one resolved JavaScript package.

    The package's own shipped runtime, JSON, declarations, and Node resolution
    manifest fields are covered. Nested ``node_modules`` are intentionally not:
    each separately resolved tool is pinned at its actual owner boundary.
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
    if (
        paths != after_paths
        or files != file_digests(after_paths)
        or manifest_bytes != _stable_bytes(manifest_path, label="runtime package.json")
    ):
        raise TypeScriptWorkerError(
            f"Runtime package changed while its freshness identity was read: {lexical_root}"
        )
    payload = {
        "format": "javascript-runtime-package/1",
        "manifestResolution": _runtime_manifest_identity(manifest),
        "files": files,
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
    before_paths = paths()
    before_metadata = metadata(before_paths)
    before_manifest = _stable_bytes(manifest_path, label="runtime package.json")
    content_identity = runtime_package_identity(lexical_root, expected_name=expected_name)
    after_entry = entry_metadata()
    after_paths = paths()
    after_metadata = metadata(after_paths)
    after_manifest = _stable_bytes(manifest_path, label="runtime package.json")
    if (
        before_entry != after_entry
        or before_paths != after_paths
        or before_metadata != after_metadata
        or before_manifest != after_manifest
    ):
        raise TypeScriptWorkerError(
            f"Runtime package changed while its session identity was read: {lexical_root}"
        )
    encoded = json.dumps(
        {
            "format": "javascript-runtime-package-session/2",
            "contentIdentity": content_identity,
            "manifestDigest": hashlib.sha256(before_manifest).hexdigest(),
            "packageEntry": before_entry,
            "files": before_metadata,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


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
    Shipped declarations remain part of the worker identity because the
    analyzer/compiler resolves package exports such as ``@usejaunt/ts/spec``
    through them. Source maps cannot affect either process.
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
        try:
            paths = {
                path.resolve(strict=True)
                for path in dist.rglob("*")
                if path.is_file()
                and _is_toolchain_identity_file(path)
                and (include_test or "test" not in path.relative_to(dist).parts[:1])
            }
        except OSError as exc:
            raise TypeScriptWorkerError(
                f"Could not enumerate @usejaunt/ts runtime files under {dist}: {exc}"
            ) from exc
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
            "format": "jaunt-ts-worker-runtime/2",
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
            "format": "jaunt-ts-worker-runtime/2",
            "kind": "package",
            "scope": "full-package" if include_test else "worker",
            "name": "@usejaunt/ts",
            "version": str(manifest["version"]),
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
            "format": "jaunt-ts-command-runtime/1",
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
        pending = [root_pin.resolved_root]
        expanded: set[Path] = set()
        edge_number = 0
        while pending:
            package_root = pending.pop()
            try:
                physical_root = package_root.resolve(strict=True)
            except OSError as exc:
                raise WorkerToolchainChangedError(
                    f"The {label} dependency closure became unreadable while it was pinned. "
                    "Rerun after the toolchain is stable."
                ) from exc
            if physical_root in expanded:
                continue
            expanded.add(physical_root)
            manifest_path = package_root / "package.json"
            try:
                dependencies = _runtime_package_dependencies(package_root)
            except TypeScriptWorkerError as exc:
                raise WorkerToolchainChangedError(
                    f"The {label} dependency closure could not be pinned for this command. "
                    "Rerun after the toolchain is stable."
                ) from exc
            for dependency, required in dependencies:
                edge_number += 1
                edge_label = f"{label} runtime dependency {edge_number} ({dependency})"
                try:
                    resolved = resolve_node_package(
                        manifest_path,
                        dependency,
                        module_path=True,
                    )
                except TypeScriptWorkerError as exc:
                    raise WorkerToolchainChangedError(
                        f"The {edge_label} resolution could not be pinned for this command. "
                        "Rerun after the toolchain is stable."
                    ) from exc
                if resolved is None:
                    if required:
                        raise WorkerToolchainChangedError(
                            f"The required {edge_label} is not installed. Reinstall the "
                            "project-local test toolchain before rerunning Jaunt."
                        )
                    self._pin_absent_package_resolution(
                        edge_label,
                        manifest_path,
                        dependency,
                        boundary=None,
                        module_path=True,
                    )
                    continue
                self.pin_package_resolution_identity(
                    edge_label,
                    manifest_path,
                    dependency,
                    module_path=True,
                )
                pending.append(self._package_resolution_pins[edge_label].resolved_root)
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
        # A second full-package read catches a replacement triggered during the
        # first worker verification while rollback bytes remain available.
        if self._full_runtime_session_identity is not None:
            self.pin_full_runtime_identity()
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
