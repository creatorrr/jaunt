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


class WorkerProtocolError(TypeScriptWorkerError):
    """The worker emitted malformed or mismatched protocol data."""


class WorkerTimeoutError(TypeScriptWorkerError):
    """A worker request exceeded its deadline."""


class WorkerCrashedError(TypeScriptWorkerError):
    """The worker exited before completing a request."""


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


_WORKER_RUNTIME_SUFFIXES = frozenset({".js", ".cjs", ".mjs", ".json"})


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


def _runtime_package_files(package_root: Path, worker_entry: Path) -> tuple[Path, ...]:
    """List path-independent runtime inputs for a packaged worker.

    Test-runner files are deliberately excluded because they have a separate
    fingerprint and can be reheadered without regenerating implementations.
    Declarations and source maps cannot affect the worker process either.
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
                and path.suffix in _WORKER_RUNTIME_SUFFIXES
                and "test" not in path.relative_to(dist).parts[:1]
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


def worker_runtime_identity(installation: WorkerInstallation) -> str:
    """Return a portable content identity for the exact worker runtime.

    A normal package and a source-tree override with the same packed runtime
    bytes receive the same identity. An arbitrary ``JAUNT_TS_WORKER`` override
    has no trusted version, so its executable bytes are the complete identity.
    """

    entry = installation.worker_entry
    if not installation.package_managed:
        content = _stable_bytes(entry, label="TypeScript worker override")
        payload: object = {
            "format": "jaunt-ts-worker-runtime/1",
            "kind": "override",
            "entryDigest": hashlib.sha256(content).hexdigest(),
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
        paths = _runtime_package_files(package_root, entry)

        def file_digests(runtime_paths: tuple[Path, ...]) -> dict[str, str]:
            return {
                path.relative_to(package_root).as_posix(): hashlib.sha256(
                    _stable_bytes(path, label="@usejaunt/ts runtime file")
                ).hexdigest()
                for path in runtime_paths
            }

        files = file_digests(paths)
        after_paths = _runtime_package_files(package_root, entry)
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
            "format": "jaunt-ts-worker-runtime/1",
            "kind": "package",
            "name": "@usejaunt/ts",
            "version": str(manifest["version"]),
            "workerExport": _export_target(worker_export),
            "entry": entry.resolve().relative_to(package_root).as_posix(),
            "files": files,
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
    ) -> None:
        self.root = root.resolve()
        self.installation = installation
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.max_message_bytes = max_message_bytes
        self.stderr_limit = stderr_limit
        self._environment = worker_environment(environ)
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
        self._stderr = bytearray()
        self._closed = False

    @property
    def stderr(self) -> str:
        return bytes(self._stderr).decode("utf-8", errors="replace")

    async def __aenter__(self) -> WorkerClient:
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def start(self) -> None:
        if self._process is not None:
            return
        if self._closed:
            raise TypeScriptWorkerError("TypeScript worker client is closed")
        self._verify_worker_runtime_identity()
        kwargs: dict[str, Any] = {}
        if os.name == "posix":
            kwargs["start_new_session"] = True
        self._process = await asyncio.create_subprocess_exec(
            self.installation.node,
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
        worker_identity = self._verify_worker_runtime_identity()
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

    def _verify_worker_runtime_identity(self) -> str:
        """Pin one immutable worker runtime to the lifetime of this client."""

        current = worker_runtime_identity(self.installation)
        if self._worker_runtime_identity is None:
            self._worker_runtime_identity = current
        elif current != self._worker_runtime_identity:
            raise TypeScriptWorkerError(
                "TypeScript worker runtime changed while the analyzer session was active"
            )
        return current

    async def request(
        self,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float | None = None,
        deadline_ms: int | None = None,
    ) -> Mapping[str, Any]:
        await self.start()
        failed_generation = self._process_generation
        try:
            return await self._request_once(
                method,
                params,
                timeout=timeout,
                deadline_ms=deadline_ms,
            )
        except WorkerCrashedError:
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
