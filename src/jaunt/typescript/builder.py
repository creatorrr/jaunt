"""High-level TypeScript discovery, synchronization, and build transactions.

The Node worker is the semantic authority.  This module deliberately treats every
worker response as a proposal: artifacts are written only after ``validateOverlay``
returns exact bytes and the workspace inputs still match the analyzed snapshot.
"""

from __future__ import annotations

import contextlib
import asyncio
import hashlib
import inspect
import json
import os
import posixpath
import re
import tempfile
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

import jaunt
from jaunt.cache import ResponseCache
from jaunt.config import JauntConfig
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.generate.base import GenerationRequest, GeneratorBackend, TokenUsage
from jaunt.generate.codex_backend import CodexBackend
from jaunt.generate.codex_backend import run_codex_exec
from jaunt.generate.request_cache import generate_request_cached
from jaunt.journal import JournalEvent, append_events
from jaunt.skill_seed import skills_fingerprint
from jaunt.targets.base import TargetBuildReport, TargetDiagnostic
from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    ProtocolDiagnostic,
    ValidateOverlayParams,
    ValidateOverlayResult,
)
from jaunt.typescript.reuse import capture_target_api_records, update_target_api_reuse_proof
from jaunt.typescript.worker import (
    WorkerClient,
    resolve_worker_installation,
    validate_worker_capabilities,
)

_DEFAULT_ATTEMPTS = 2
_ANALYZE_CONTRACT_BATCH_SIZE = 4
_SYNC_BATCH_SIZE = 4
_PLACEHOLDER_MARKERS = ("state=unbuilt", 'state = "unbuilt"', "state: unbuilt")
MISSING_INPUT = "<missing>"


class WorkerLike(Protocol):
    """Small worker surface used by the high-level operations and test fakes."""

    installation: Any

    async def initialize(self, params: InitializeParams) -> InitializeResult: ...

    async def request(self, method: str, params: Mapping[str, Any]) -> Mapping[str, Any]: ...


WorkerFactory: TypeAlias = Callable[
    [Path, TypeScriptTargetConfig],
    WorkerLike | Awaitable[WorkerLike],
]


@dataclass(frozen=True, slots=True)
class TypeScriptAnalysis:
    """One worker snapshot shared by a single high-level operation."""

    initialized: InitializeResult
    workspace: Mapping[str, Any]
    contracts: Mapping[str, Any]

    @property
    def modules(self) -> tuple[Mapping[str, Any], ...]:
        value = self.contracts.get("modules", [])
        return (
            tuple(item for item in value if isinstance(item, Mapping))
            if isinstance(value, list)
            else ()
        )


@dataclass(frozen=True, slots=True)
class SyncReport:
    mirrors: tuple[str, ...] = ()
    placeholders: tuple[str, ...] = ()
    created_facades: tuple[str, ...] = ()
    failed: Mapping[str, tuple[TargetDiagnostic, ...]] = field(default_factory=dict)
    exit_code: int = 0

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass(frozen=True, slots=True)
class _Write:
    path: str
    content: str | None
    kind: str
    module_id: str


@dataclass(frozen=True, slots=True)
class _BuildUnit:
    """An owner/reference component committed as one artifact transaction."""

    key: str
    module_ids: tuple[str, ...]


def _target(config: JauntConfig) -> TypeScriptTargetConfig:
    target = config.typescript_target
    if target is None:
        raise JauntConfigError("No [target.ts] is configured in jaunt.toml")
    return target


def _safe_path(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        raise JauntConfigError(f"TypeScript worker returned an unsafe path: {relative!r}")
    resolved_root = root.resolve()
    resolved = (resolved_root / path).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise JauntConfigError(f"TypeScript worker path escapes the workspace: {relative!r}")
    return resolved


def _sha256(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def _path_hash(path: Path) -> str | None:
    try:
        return _sha256(path.read_bytes())
    except FileNotFoundError:
        return None


def _fsync_directory(path: Path) -> None:
    """Persist directory-entry updates where the filesystem supports it."""

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


def _artifact_preconditions(root: Path, modules: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for module in modules:
        for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath"):
            relative = _module_path(module, key)
            result[relative] = _path_hash(_safe_path(root, relative)) or MISSING_INPUT
    return result


def _input_hashes(value: Mapping[str, Any]) -> Mapping[str, str]:
    hashes = value.get("inputHashes", {})
    if not isinstance(hashes, Mapping):
        return {}
    return {str(path): str(digest) for path, digest in hashes.items() if isinstance(digest, str)}


def _assert_inputs_unchanged(root: Path, expected: Mapping[str, str]) -> None:
    changed: list[str] = []
    for relative, digest in sorted(expected.items()):
        path = _safe_path(root, relative)
        actual = _path_hash(path)
        if digest == MISSING_INPUT:
            if actual is not None:
                changed.append(relative)
            continue
        normalized = digest if digest.startswith("sha256:") else f"sha256:{digest}"
        if actual != normalized:
            changed.append(relative)
    if changed:
        raise JauntGenerationError(
            "TypeScript inputs changed after analysis; no artifacts were written: "
            + ", ".join(changed)
        )


def _diagnostic(value: ProtocolDiagnostic | Mapping[str, Any]) -> TargetDiagnostic:
    if isinstance(value, ProtocolDiagnostic):
        return TargetDiagnostic(
            code=value.code,
            message=value.message,
            severity=cast(
                Any, value.severity if value.severity in {"error", "warning", "info"} else "error"
            ),
            path=value.path,
            line=value.line,
            column=value.column,
            data={"start": value.start, "end": value.end},
        )
    severity = value.get("severity", "error")
    return TargetDiagnostic(
        code=str(value.get("code", "JAUNT_TS_DIAGNOSTIC")),
        message=str(value.get("message", "TypeScript operation failed")),
        severity=cast(Any, severity if severity in {"error", "warning", "info"} else "error"),
        path=str(value["path"]) if value.get("path") is not None else None,
        line=value.get("line") if isinstance(value.get("line"), int) else None,
        column=value.get("column") if isinstance(value.get("column"), int) else None,
        data={
            str(key): item
            for key, item in value.items()
            if key not in {"code", "message", "severity", "path", "line", "column"}
        },
    )


def _diagnostics(
    values: Sequence[ProtocolDiagnostic | Mapping[str, Any]],
) -> tuple[TargetDiagnostic, ...]:
    return tuple(_diagnostic(value) for value in values)


def _initialize_params(
    root: Path,
    config: JauntConfig,
    target: TypeScriptTargetConfig,
    client: WorkerLike,
    *,
    generation_fingerprint: str | None = None,
) -> InitializeParams:
    installation = getattr(client, "installation", None)
    compiler = getattr(installation, "compiler_module_path", Path("typescript/lib/typescript.js"))
    return InitializeParams(
        root=str(root.resolve()),
        projects=tuple(target.projects),
        test_projects=tuple(target.test_projects),
        source_roots=tuple(target.source_roots),
        test_roots=tuple(target.test_roots),
        generated_dir=target.generated_dir,
        tool_owner=target.tool_owner,
        compiler_module_path=str(compiler),
        client_version=jaunt.__version__,
        tool_version=jaunt.__version__,
        generation_fingerprint=generation_fingerprint or _generation_fingerprint(config, root=root),
    )


@asynccontextmanager
async def worker_session(
    root: Path,
    config: JauntConfig,
    *,
    worker_factory: WorkerFactory | None = None,
    generation_fingerprint: str | None = None,
) -> AsyncIterator[tuple[WorkerLike, InitializeResult]]:
    """Open and initialize one project-local analyzer session."""

    root = root.resolve()
    # A killed test-repair process can leave an implementation commit between
    # its model pass and held-out rerun. Restore the durable pre-repair snapshot
    # before any subsequent TypeScript operation observes that state.
    from jaunt.typescript.tester import _recover_pending_test_repairs

    _recover_pending_test_repairs(root)
    target = _target(config)
    if worker_factory is None:
        installation = resolve_worker_installation(root, target)
        client: WorkerLike = WorkerClient(
            root=root,
            installation=installation,
            request_timeout=target.worker_timeout_seconds,
            startup_timeout=target.worker_startup_timeout_seconds,
        )
    else:
        created = worker_factory(root, target)
        client = await created if inspect.isawaitable(created) else created

    entered = False
    enter = getattr(client, "__aenter__", None)
    exit_ = getattr(client, "__aexit__", None)
    try:
        if callable(enter):
            entered_client = await enter()
            if entered_client is not None:
                client = cast(WorkerLike, entered_client)
            entered = True
        initialized = await client.initialize(
            _initialize_params(
                root,
                config,
                target,
                client,
                generation_fingerprint=generation_fingerprint,
            )
        )
        validate_worker_capabilities(initialized)
        yield client, initialized
    finally:
        if entered and callable(exit_):
            await exit_(None, None, None)
        elif not entered:
            close = getattr(client, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result


async def analyze(
    client: WorkerLike,
    initialized: InitializeResult,
    *,
    target_ids: Sequence[str] = (),
) -> TypeScriptAnalysis:
    supports_scoped_diagnostics = "scoped-diagnostics" in getattr(initialized, "capabilities", ())
    workspace = await client.request(
        "analyzeWorkspace",
        ({"moduleIds": list(target_ids)} if target_ids and supports_scoped_diagnostics else {}),
    )
    supports_scoped_analysis = "scoped-analysis" in getattr(initialized, "capabilities", ())
    raw_specs = workspace.get("specs", [])
    workspace_module_ids = (
        list(
            dict.fromkeys(
                str(item["moduleId"])
                for item in raw_specs
                if isinstance(item, Mapping) and isinstance(item.get("moduleId"), str)
            )
        )
        if isinstance(raw_specs, list)
        else []
    )
    requested_module_ids = list(dict.fromkeys(target.split("#", 1)[0] for target in target_ids))
    if target_ids and supports_scoped_analysis:
        batches = [requested_module_ids]
    elif not target_ids and workspace_module_ids:
        batches = [
            workspace_module_ids[index : index + _ANALYZE_CONTRACT_BATCH_SIZE]
            for index in range(0, len(workspace_module_ids), _ANALYZE_CONTRACT_BATCH_SIZE)
        ]
    else:
        batches = [[]]
    contract_responses = [
        await client.request(
            "analyzeContracts",
            ({"moduleIds": batch} if batch else {}),
        )
        for batch in batches
    ]
    contracts = dict(contract_responses[0])
    merged_modules: dict[str, Mapping[str, Any]] = {}
    for response in contract_responses:
        raw_modules = response.get("modules", [])
        if not isinstance(raw_modules, list):
            continue
        for module in raw_modules:
            if isinstance(module, Mapping):
                merged_modules[_module_id(module)] = module
    contracts["modules"] = list(merged_modules.values())
    if target_ids:
        raw_modules = contracts.get("modules", [])
        modules = (
            [item for item in raw_modules if isinstance(item, Mapping)]
            if isinstance(raw_modules, list)
            else []
        )
        by_id = {_module_id(module): module for module in modules}
        requested = {target.split("#", 1)[0] for target in target_ids}
        missing = set(requested - set(by_id))
        for target in target_ids:
            module_id, separator, symbol_name = target.partition("#")
            if not separator or module_id not in by_id:
                continue
            symbols = by_id[module_id].get("symbols", [])
            names = (
                {
                    str(symbol.get("name"))
                    for symbol in symbols
                    if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str)
                }
                if isinstance(symbols, list)
                else set()
            )
            if symbol_name not in names:
                missing.add(target)
        if missing:
            raise JauntConfigError("Unknown TypeScript target(s): " + ", ".join(sorted(missing)))
        if not supports_scoped_analysis:
            selected: set[str] = set()
            pending = list(requested)
            while pending:
                module_id = pending.pop()
                if module_id in selected:
                    continue
                selected.add(module_id)
                dependencies = by_id[module_id].get("dependencies", [])
                if isinstance(dependencies, list):
                    pending.extend(
                        dependency.split("#", 1)[0]
                        for dependency in dependencies
                        if isinstance(dependency, str) and dependency.split("#", 1)[0] in by_id
                    )
            contracts = {
                **contracts,
                "modules": [module for module in modules if _module_id(module) in selected],
            }
    diagnostics = workspace.get("diagnostics", [])
    errors = (
        [
            item
            for item in diagnostics
            if isinstance(item, Mapping) and item.get("severity") == "error"
        ]
        if isinstance(diagnostics, list)
        else []
    )
    if errors:
        rendered = "; ".join(
            str(item.get("message", "TypeScript discovery failed")) for item in errors
        )
        raise JauntConfigError(f"TypeScript workspace analysis failed: {rendered}")
    return TypeScriptAnalysis(initialized=initialized, workspace=workspace, contracts=contracts)


def _stamp_params(
    analysis: TypeScriptAnalysis,
    candidates: Mapping[str, str],
    module_ids: Sequence[str],
) -> ValidateOverlayParams:
    source = analysis.contracts
    return ValidateOverlayParams(
        session_id=str(source.get("sessionId", analysis.initialized.stamp.session_id)),
        expected_epoch=int(source.get("epoch", analysis.initialized.stamp.epoch)),
        expected_snapshot=str(source.get("snapshot", analysis.initialized.stamp.snapshot)),
        candidates=candidates,
        module_ids=tuple(module_ids),
    )


async def validate_overlay(
    client: WorkerLike,
    analysis: TypeScriptAnalysis,
    candidates: Mapping[str, str],
    module_ids: Sequence[str],
    *,
    sync_module_ids: Sequence[str] = (),
    restamp_module_ids: Sequence[str] = (),
    recompose_module_ids: Sequence[str] = (),
    scoped_validation: bool = False,
    baseline_unselected: bool = False,
) -> ValidateOverlayResult:
    wire = _stamp_params(analysis, candidates, module_ids).to_wire()
    if sync_module_ids:
        wire["syncModuleIds"] = list(sync_module_ids)
    if restamp_module_ids:
        wire["restampModuleIds"] = list(restamp_module_ids)
    if recompose_module_ids:
        wire["recomposeModuleIds"] = list(recompose_module_ids)
    if scoped_validation and "scoped-validation" in analysis.initialized.capabilities:
        wire["scopeToModuleIds"] = True
    if baseline_unselected:
        wire["baselineUnselected"] = True
    raw = await client.request(
        "validateOverlay",
        wire,
    )
    return ValidateOverlayResult.from_wire(raw)


def _artifact_writes(result: ValidateOverlayResult) -> tuple[_Write, ...]:
    writes: list[_Write] = []
    for artifact in result.artifacts:
        if _sha256(artifact.content.encode("utf-8")) != artifact.sha256:
            raise JauntGenerationError(
                f"TypeScript worker returned a bad content hash for {artifact.path}"
            )
        writes.append(
            _Write(
                path=artifact.path,
                content=artifact.content,
                kind=artifact.kind,
                module_id=artifact.module_id,
            )
        )
    return tuple(writes)


def atomic_write_manifest(
    root: Path,
    writes: Sequence[_Write],
    *,
    expected_inputs: Mapping[str, str] | None = None,
    preserve_existing_facades: bool = False,
    preserve_real_implementations: bool = False,
) -> tuple[_Write, ...]:
    """Replace a validated artifact manifest with preconditions and rollback.

    ``os.replace`` is atomic per path.  The explicit rollback makes the multi-file
    transaction recoverable if a later replacement fails.
    """

    root = root.resolve()
    expected_inputs = expected_inputs or {}
    _assert_inputs_unchanged(root, expected_inputs)
    filtered: list[_Write] = []
    seen: set[Path] = set()
    for write in writes:
        path = _safe_path(root, write.path)
        if write.kind == "facade" and preserve_existing_facades and path.exists():
            continue
        if write.kind in {"implementation", "placeholder"} and preserve_real_implementations:
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                if not any(marker in existing for marker in _PLACEHOLDER_MARKERS):
                    continue
        if path in seen:
            raise JauntGenerationError(f"Duplicate TypeScript artifact path: {write.path}")
        seen.add(path)
        filtered.append(write)
    if not filtered:
        return ()

    original: dict[Path, bytes | None] = {}
    observed: dict[Path, str | None] = {}
    staged: dict[Path, Path] = {}
    manifest: Path | None = None
    try:
        for write in filtered:
            path = _safe_path(root, write.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            try:
                old = path.read_bytes()
            except FileNotFoundError:
                old = None
            original[path] = old
            observed[path] = _sha256(old) if old is not None else None
            if write.content is None:
                continue
            fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.jaunt-", dir=path.parent)
            temp = Path(raw_temp)
            with os.fdopen(fd, "wb") as handle:
                handle.write(write.content.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            staged[path] = temp

        _assert_inputs_unchanged(root, expected_inputs)
        for path, expected in observed.items():
            if _path_hash(path) != expected:
                raise JauntGenerationError(
                    f"TypeScript artifact changed during validation: {path.relative_to(root)}"
                )

        writes_by_path = {_safe_path(root, write.path): write for write in filtered}
        manifest_directory = root / ".jaunt" / "transactions"
        manifest_directory.mkdir(parents=True, exist_ok=True)
        manifest = manifest_directory / f"ts-{uuid.uuid4().hex}.json"
        manifest_payload = {
            "state": "prepared",
            "writes": [
                {
                    "path": path.relative_to(root).as_posix(),
                    "kind": writes_by_path[path].kind,
                    "moduleId": writes_by_path[path].module_id,
                    "before": observed[path] or MISSING_INPUT,
                    "after": (
                        _sha256(cast(str, writes_by_path[path].content).encode("utf-8"))
                        if writes_by_path[path].content is not None
                        else MISSING_INPUT
                    ),
                }
                for path in sorted(writes_by_path, key=lambda item: item.as_posix())
            ],
        }
        fd, raw_manifest = tempfile.mkstemp(
            prefix=f".{manifest.name}.", suffix=".tmp", dir=manifest_directory
        )
        manifest_temp = Path(raw_manifest)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(manifest_payload, handle, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(manifest_temp, manifest)
            _fsync_directory(manifest_directory)
        finally:
            with contextlib.suppress(FileNotFoundError):
                manifest_temp.unlink()

        replaced: list[Path] = []
        try:
            for path in sorted(writes_by_path, key=lambda item: item.as_posix()):
                write = writes_by_path[path]
                if write.content is None:
                    path.unlink(missing_ok=True)
                else:
                    os.replace(staged[path], path)
                _fsync_directory(path.parent)
                replaced.append(path)
        except BaseException:
            rollback_ok = True
            for path in reversed(replaced):
                old = original[path]
                try:
                    if old is None:
                        path.unlink(missing_ok=True)
                    else:
                        fd, raw_temp = tempfile.mkstemp(
                            prefix=f".{path.name}.rollback-", dir=path.parent
                        )
                        temp = Path(raw_temp)
                        try:
                            with os.fdopen(fd, "wb") as handle:
                                handle.write(old)
                                handle.flush()
                                os.fsync(handle.fileno())
                            os.replace(temp, path)
                            _fsync_directory(path.parent)
                        finally:
                            with contextlib.suppress(FileNotFoundError):
                                temp.unlink()
                except OSError:
                    rollback_ok = False
            if rollback_ok and manifest is not None:
                manifest.unlink(missing_ok=True)
                _fsync_directory(manifest.parent)
            raise
        else:
            manifest.unlink(missing_ok=True)
            _fsync_directory(manifest.parent)
    finally:
        for temp in staged.values():
            with contextlib.suppress(FileNotFoundError):
                temp.unlink()
    return tuple(filtered)


def _module_id(module: Mapping[str, Any]) -> str:
    value = module.get("moduleId")
    if not isinstance(value, str) or not value.startswith("ts:"):
        raise JauntConfigError("TypeScript worker returned a module without a stable ts: ID")
    return value


def _module_path(module: Mapping[str, Any], key: str) -> str:
    value = module.get(key)
    if not isinstance(value, str) or not value:
        routes = module.get("routes")
        if isinstance(routes, Mapping):
            value = routes.get(key)
    if not isinstance(value, str) or not value:
        raise JauntConfigError(f"TypeScript worker omitted {key} for {_module_id(module)}")
    return value


async def run_sync(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    worker_factory: WorkerFactory | None = None,
) -> SyncReport:
    """Render mirrors/facades/placeholders without invoking a model."""

    root = root.resolve()
    async with worker_session(root, config, worker_factory=worker_factory) as (client, initialized):
        analysis = await analyze(client, initialized, target_ids=target_ids)
        modules = _topological_modules(analysis.modules)
        output_preconditions = _artifact_preconditions(root, modules)
        module_ids = tuple(_module_id(module) for module in modules)
        validated_batches: list[ValidateOverlayResult] = []
        for index in range(0, len(module_ids), _SYNC_BATCH_SIZE):
            batch = module_ids[index : index + _SYNC_BATCH_SIZE]
            validated = await validate_overlay(
                client,
                analysis,
                {},
                batch,
                sync_module_ids=batch,
                scoped_validation=bool(target_ids),
            )
            if not validated.valid:
                failed = {
                    module_id: _diagnostics(validated.diagnostics) for module_id in module_ids
                }
                return SyncReport(failed=failed, exit_code=2)
            validated_batches.append(validated)
        validated_writes = tuple(
            write for validated in validated_batches for write in _artifact_writes(validated)
        )
        writes = atomic_write_manifest(
            root,
            validated_writes,
            expected_inputs={
                **_input_hashes(analysis.contracts),
                **output_preconditions,
            },
            preserve_existing_facades=True,
            preserve_real_implementations=True,
        )

    append_events(
        root,
        [JournalEvent("sync", write.module_id, write.path) for write in writes],
    )
    return SyncReport(
        mirrors=tuple(sorted(write.path for write in writes if write.kind == "api-mirror")),
        placeholders=tuple(sorted(write.path for write in writes if write.kind == "placeholder")),
        created_facades=tuple(sorted(write.path for write in writes if write.kind == "facade")),
    )


def _prompt_text(configured: str, bundled: str) -> str:
    path = Path(configured) if configured else Path(__file__).with_name("prompts") / bundled
    return path.read_text(encoding="utf-8").strip()


def _generation_fingerprint(
    config: JauntConfig,
    *,
    root: Path | None = None,
    build_instructions: Sequence[str] | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    repo_map_enabled: bool | None = None,
    project_overview_enabled: bool | None = None,
) -> str:
    """Hash every input that can change model-authored implementation bytes."""

    instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    payload = {
        "format": "jaunt-ts-generation/1-draft.1",
        "model": config.codex.model,
        "reasoning_effort": config.codex.reasoning_effort,
        "sandbox": config.codex.sandbox,
        "codex_features": tuple(config.codex.features),
        "codex_config": config.codex.config,
        "build_system": _prompt_text(config.typescript_prompts.build_system, "build_system.md"),
        "build_module": _prompt_text(config.typescript_prompts.build_module, "build_module.md"),
        "build_instructions": instructions,
        "repo_map_enabled": (
            bool(config.context.repo_map) if repo_map_enabled is None else repo_map_enabled
        ),
        "project_overview_enabled": (
            bool(config.context.overview)
            if project_overview_enabled is None
            else project_overview_enabled
        ),
        "builtin_skills": effective_builtin_skills,
        "skills_fingerprint": skills_fingerprint(
            project_root=root,
            builtin_names=effective_builtin_skills,
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest}"


def _typescript_project_digest(
    modules: Sequence[Mapping[str, Any]],
    repo_map_block: str,
) -> str:
    """Digest the exact TS contracts used to generate a project overview."""

    payload = [
        {
            "moduleId": _module_id(module),
            "specSource": str(module.get("specSource", "")),
            "apiSource": str(module.get("apiSource", "")),
        }
        for module in sorted(modules, key=_module_id)
    ]
    return hashlib.sha256(
        json.dumps(
            {"modules": payload, "repoMap": repo_map_block},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


async def _project_overview_block(
    root: Path,
    config: JauntConfig,
    modules: Sequence[Mapping[str, Any]],
    *,
    repo_map_block: str,
    backend: GeneratorBackend,
    cost_tracker: CostTracker,
    enabled: bool,
) -> str:
    """Build/reuse the common project overview for a TypeScript workspace."""

    if not enabled:
        return ""
    from jaunt.repo_context.overview import build_project_docs_block, load_or_build_overview

    docs = build_project_docs_block(root, max_chars=config.context.max_chars)
    try:
        result = await load_or_build_overview(
            backend,
            repo_map_block=repo_map_block,
            project_docs=docs,
            digest=_typescript_project_digest(modules, repo_map_block),
            # Keep the TS overview cache separate from Python's project overview.
            # Mixed commands may build both concurrently and each digest covers a
            # different contract set.
            state_dir=root / ".jaunt" / "typescript",
            enabled=True,
            prompts=config.prompts,
            cost_tracker=cost_tracker,
        )
        cost_tracker.check_budget()
        return result
    except JauntGenerationError:
        raise
    except Exception:
        # Match Python's best-effort context behavior: an unavailable overview must
        # never block otherwise-valid generation.
        return ""


def _progress_phase(progress: object | None, item: str, stage: str, detail: str = "") -> None:
    if progress is None:
        return
    phase = getattr(progress, "phase", None)
    if callable(phase):
        try:
            phase(item, stage, detail)
        except Exception:
            pass


def _progress_advance(progress: object | None, item: str, *, ok: bool) -> None:
    if progress is None:
        return
    advance = getattr(progress, "advance", None)
    if callable(advance):
        try:
            advance(item, ok=ok)
        except Exception:
            pass


def _progress_finish(progress: object | None) -> None:
    if progress is None:
        return
    finish = getattr(progress, "finish", None)
    if callable(finish):
        try:
            finish()
        except Exception:
            pass


def _clear_recovered_build_manifests(
    root: Path,
    modules: Sequence[Mapping[str, Any]],
) -> None:
    """Clear crash markers only after every module named by them is healthy."""

    from jaunt.typescript.artifacts import incomplete_transaction_manifests
    from jaunt.typescript.status import classify_modules

    state = classify_modules(root, modules)
    known = {_module_id(module) for module in modules}
    unhealthy = set(state.stale) | set(state.unbuilt) | set(state.invalid)
    for manifest in incomplete_transaction_manifests(root):
        try:
            value = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        writes = value.get("writes", []) if isinstance(value, Mapping) else []
        module_ids = {
            str(write.get("moduleId"))
            for write in writes
            if isinstance(write, Mapping) and isinstance(write.get("moduleId"), str)
        }
        if module_ids and module_ids <= known and not module_ids.intersection(unhealthy):
            manifest.unlink(missing_ok=True)


def _reserved_bindings(module: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = module.get("reservedBindings")
    if isinstance(explicit, list) and all(isinstance(item, str) for item in explicit):
        return tuple(explicit)
    symbols = module.get("symbols", [])
    names: list[str] = []
    if isinstance(symbols, list):
        for symbol in symbols:
            if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str):
                names.append(f"__jaunt_impl_{symbol['name']}")
    return tuple(names)


def _dependency_module_ids(module: Mapping[str, Any]) -> tuple[str, ...]:
    dependencies = module.get("dependencies", [])
    if not isinstance(dependencies, list):
        return ()
    module_id = _module_id(module)
    return tuple(
        sorted(
            {
                dependency.split("#", 1)[0]
                for dependency in dependencies
                if isinstance(dependency, str)
                and dependency.startswith("ts:")
                and dependency.split("#", 1)[0] != module_id
            }
        )
    )


def _topological_modules(
    modules: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    by_id = {_module_id(module): module for module in modules}
    visiting: list[str] = []
    visited: set[str] = set()
    ordered: list[Mapping[str, Any]] = []

    def visit(module_id: str) -> None:
        if module_id in visited:
            return
        if module_id in visiting:
            cycle = visiting[visiting.index(module_id) :] + [module_id]
            raise JauntConfigError("TypeScript dependency cycle: " + " -> ".join(cycle))
        visiting.append(module_id)
        module = by_id[module_id]
        for dependency in _dependency_module_ids(module):
            if dependency in by_id:
                visit(dependency)
        visiting.pop()
        visited.add(module_id)
        ordered.append(module)

    for module_id in sorted(by_id):
        visit(module_id)
    return tuple(ordered)


def _build_units(
    _analysis: TypeScriptAnalysis,
    modules: Sequence[Mapping[str, Any]],
) -> tuple[_BuildUnit, ...]:
    """Group only dependency-connected writes into atomic transactions.

    A shared package owner or project-reference graph is a validation scope,
    not a reason to discard unrelated successful candidates. Explicit Jaunt
    dependency edges still keep candidates that must move together in one unit.
    """

    by_id = {_module_id(module): module for module in modules}
    if not by_id:
        return ()

    module_parent = {module_id: module_id for module_id in by_id}

    def module_find(value: str) -> str:
        while module_parent[value] != value:
            module_parent[value] = module_parent[module_parent[value]]
            value = module_parent[value]
        return value

    def module_union(left: str, right: str) -> None:
        left_root = module_find(left)
        right_root = module_find(right)
        if left_root != right_root:
            first, second = sorted((left_root, right_root))
            module_parent[second] = first

    for module_id, module in by_id.items():
        for dependency in _dependency_module_ids(module):
            if dependency in by_id:
                module_union(module_id, dependency)

    grouped: dict[str, list[str]] = {}
    for module_id in sorted(by_id):
        grouped.setdefault(module_find(module_id), []).append(module_id)
    return tuple(
        _BuildUnit(key=module_ids[0], module_ids=tuple(module_ids))
        for module_ids in sorted(grouped.values(), key=lambda values: values[0])
    )


def _dependency_failure(module_id: str, dependencies: Sequence[str]) -> TargetDiagnostic:
    rendered = ", ".join(sorted(dependencies))
    return TargetDiagnostic(
        code="JAUNT_TS_DEPENDENCY_FAILED",
        message=(
            f"Skipped {module_id} because TypeScript dependency generation failed: {rendered}"
        ),
        data={"dependencies": tuple(sorted(dependencies))},
    )


def _component_failure(module_id: str, failed_ids: Sequence[str]) -> TargetDiagnostic:
    rendered = ", ".join(sorted(failed_ids))
    return TargetDiagnostic(
        code="JAUNT_TS_COMPONENT_ABORTED",
        message=(
            f"No artifacts were written for {module_id} because its dependency-connected "
            f"component failed: {rendered}"
        ),
        data={"failed_modules": tuple(sorted(failed_ids))},
    )


def _runtime_dependency_specifier(from_path: str, facade_path: str) -> str:
    emitted = str(Path(facade_path).with_suffix(".js")).replace("\\", "/")
    relative = posixpath.relpath(emitted, posixpath.dirname(from_path))
    return relative if relative.startswith(".") else f"./{relative}"


def _authored_dependency_specifier(module: Mapping[str, Any], dependency_id: str) -> str | None:
    """Reuse the spec's project-aware import for the generated public facade.

    Across project references, a filesystem-relative import into another
    project's source tree bypasses TypeScript's reference redirect and violates
    the consumer's rootDir. Authored path/package aliases already encode the
    project boundary, so retain them while removing the private ``.jaunt``
    segment. Relative imports are also safe here: IR renders them from the API
    mirror directory, which is the implementation directory too.
    """

    dependencies = module.get("dependencies", [])
    symbols = {
        dependency.split("#", 1)[1]
        for dependency in dependencies
        if isinstance(dependency, str)
        and dependency.startswith(f"{dependency_id}#")
        and "#" in dependency
    }
    imports = module.get("typeImports", [])
    if not isinstance(imports, list):
        return None
    for item in imports:
        if not isinstance(item, Mapping):
            continue
        bindings = item.get("namedImports", [])
        imported = {
            str(binding.get("imported"))
            for binding in bindings
            if isinstance(binding, Mapping) and isinstance(binding.get("imported"), str)
        }
        specifier = item.get("specifier")
        if symbols.intersection(imported) and isinstance(specifier, str):
            return re.sub(r"\.jaunt(?=(?:\.(?:js|jsx|ts|tsx))?$)", "", specifier)
    return None


def _build_request(
    root: Path,
    config: JauntConfig,
    module: Mapping[str, Any],
    dependency_modules: Mapping[str, Mapping[str, Any]],
    validator: Callable[[str], Awaitable[list[str]]],
    *,
    build_instructions: Sequence[str] = (),
    ephemeral_prompt: str = "",
    repo_map_block: str = "",
    project_overview_block: str = "",
    builtin_skill_names: Sequence[str] | None = None,
) -> GenerationRequest:
    module_id = _module_id(module)
    target_path = _module_path(module, "implementationPath")
    bindings = _reserved_bindings(module)
    system = _prompt_text(config.typescript_prompts.build_system, "build_system.md")
    user = _prompt_text(config.typescript_prompts.build_module, "build_module.md")
    user = (
        user.replace("{{target_path}}", target_path)
        .replace("{{reserved_bindings}}", ", ".join(bindings))
        .replace("{{module_kind}}", str(module.get("moduleKind", "project default")))
        .replace("{{module_resolution}}", str(module.get("moduleResolution", "project default")))
    )
    if build_instructions:
        user += "\n\nAdditional project instructions:\n" + "\n".join(
            f"- {instruction}" for instruction in build_instructions
        )
    context: dict[str, str] = {
        "_context/contract.json": json.dumps(module, sort_keys=True, indent=2, default=str) + "\n",
        "_context/spec.ts": str(module.get("specSource", "")),
        "_context/api.ts": str(module.get("apiSource", "")),
    }
    context_source = module.get("contextSource")
    if isinstance(context_source, str):
        context["_context/context.ts"] = context_source
    dependency_context: list[dict[str, Any]] = []
    for dependency_id in _dependency_module_ids(module):
        other = dependency_modules.get(dependency_id)
        if other is None:
            raise JauntConfigError(
                f"{module_id} names unknown TypeScript dependency module {dependency_id}"
            )
        facade_path = _module_path(other, "facadePath")
        specifier = _authored_dependency_specifier(module, dependency_id) or (
            _runtime_dependency_specifier(target_path, facade_path)
        )
        index = len(dependency_context)
        context[f"_context/dependency_{index}.api.ts"] = str(other.get("apiSource", ""))
        dependency_context.append(
            {
                "moduleId": dependency_id,
                "facadePath": facade_path,
                "facadeSpecifier": specifier,
                "apiDigest": other.get("apiDigest"),
                "symbols": [
                    dependency
                    for dependency in module.get("dependencies", [])
                    if isinstance(dependency, str) and dependency.split("#", 1)[0] == dependency_id
                ],
            }
        )
    if dependency_context:
        context["_context/dependencies.json"] = (
            json.dumps(dependency_context, sort_keys=True, indent=2, default=str) + "\n"
        )
        user += "\n\nDeclared Jaunt dependency facades:\n" + "\n".join(
            f"- {item['moduleId']}: `{item['facadeSpecifier']}`" for item in dependency_context
        )
    if repo_map_block:
        context["_context/repository-map.md"] = repo_map_block
        user += "\n\nUse `_context/repository-map.md` for bounded repository orientation."
    if project_overview_block:
        context["_context/project-overview.md"] = project_overview_block
        user += "\n\nUse `_context/project-overview.md` as project-level architectural context."
    if ephemeral_prompt:
        user += "\n\nEphemeral validation feedback for this invocation only:\n" + ephemeral_prompt
    return GenerationRequest(
        language="ts",
        kind="build",
        target_path=target_path,
        context_files=context,
        prompt=f"{system}\n\n{user}",
        cache_payload={
            "moduleId": module_id,
            "structuralDigest": module.get("structuralDigest"),
            "proseDigest": module.get("proseDigest"),
            "apiDigest": module.get("apiDigest"),
            "bindings": bindings,
        },
        validator=validator,
        project_root=root,
        builtin_skill_names=(
            tuple(builtin_skill_names)
            if builtin_skill_names is not None
            else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
        ),
    )


def _default_backend(config: JauntConfig) -> GeneratorBackend:
    if config.codex.model != "gpt-5.6-sol":
        raise JauntConfigError('TypeScript generation requires [codex].model = "gpt-5.6-sol"')
    return CodexBackend(config.codex, config.llm, config.prompts)


async def _gate_prose_change(
    root: Path,
    module: Mapping[str, Any],
    config: JauntConfig,
    *,
    cost: CostTracker | None = None,
    run_exec: Callable[..., Awaitable[Any]] | None = None,
) -> bool:
    """Return true only when the judge confidently finds prose equivalent."""

    sidecar_path = _safe_path(root, _module_path(module, "sidecarPath"))
    try:
        old = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(old, Mapping):
        return False
    old_symbols = old.get("symbols", [])
    new_symbols = module.get("symbols", [])
    if not isinstance(old_symbols, list) or not isinstance(new_symbols, list):
        return False
    previous = {
        str(symbol.get("name")): symbol
        for symbol in old_symbols
        if isinstance(symbol, Mapping) and isinstance(symbol.get("name"), str)
    }
    judged_change = False

    async def equivalent(prompt: str) -> bool:
        try:
            with tempfile.TemporaryDirectory() as temporary:
                exec_fn = run_exec or run_codex_exec
                result = await exec_fn(
                    prompt=prompt,
                    cwd=temporary,
                    sandbox="read-only",
                    model=config.semantic_gate.model,
                    reasoning_effort=config.semantic_gate.reasoning_effort,
                    ignore_user_config=True,
                )
        except Exception:
            return False
        usage_input = getattr(result, "usage_input", None)
        usage_output = getattr(result, "usage_output", None)
        if cost is not None and isinstance(usage_input, int) and isinstance(usage_output, int):
            cost.record(
                f"{_module_id(module)}:semantic-gate",
                TokenUsage(
                    prompt_tokens=usage_input,
                    completion_tokens=usage_output,
                    model=config.semantic_gate.model,
                    provider="codex",
                    cached_prompt_tokens=getattr(result, "usage_cached", None) or 0,
                ),
            )
            cost.check_budget()
        return result.final_message.strip() == "EQUIVALENT"

    for symbol in new_symbols:
        if not isinstance(symbol, Mapping) or not isinstance(symbol.get("name"), str):
            return False
        before = previous.get(str(symbol["name"]))
        if before is None:
            return False
        old_docs = str(before.get("docs", ""))
        new_docs = str(symbol.get("docs", ""))
        if old_docs == new_docs:
            continue
        judged_change = True
        structural_contract = {str(key): value for key, value in symbol.items() if key != "docs"}
        prompt = (
            "A TypeScript declaration's TSDoc is its behavioral contract. Its type/"
            "signature IR is unchanged. Determine whether NEW requires, forbids, or "
            "relaxes any behavior compared with OLD (including results, errors, ordering, "
            "edge cases, async behavior, or complexity). Reply exactly EQUIVALENT or "
            "MEANINGFUL; uncertainty is MEANINGFUL.\n\n"
            f"DECLARATION IR:\n{json.dumps(structural_contract, sort_keys=True)}\n\n"
            f"OLD:\n{old_docs}\n\nNEW:\n{new_docs}"
        )
        if not await equivalent(prompt):
            return False

    old_context_docs = old.get("contextDocs")
    new_context_docs = module.get("contextDocs")
    if old_context_docs != new_context_docs:
        # An old sidecar without the canonical records cannot establish what
        # prose changed. Rebuild once instead of guessing from digests alone.
        if not isinstance(old_context_docs, list) or not isinstance(new_context_docs, list):
            return False
        judged_change = True
        prompt = (
            "These are TSDoc contracts imported into a generated TypeScript module's "
            "type/runtime context. The structural type environment is unchanged. "
            "Determine whether NEW requires, forbids, or relaxes any behavior compared "
            "with OLD (including results, errors, ordering, edge cases, async behavior, "
            "or complexity). Reply exactly EQUIVALENT or MEANINGFUL; uncertainty is "
            "MEANINGFUL.\n\n"
            f"OLD CONTEXT DOCS:\n{json.dumps(old_context_docs, sort_keys=True)}\n\n"
            f"NEW CONTEXT DOCS:\n{json.dumps(new_context_docs, sort_keys=True)}"
        )
        if not await equivalent(prompt):
            return False

    # A changed prose digest without an inspectable doc delta is not evidence
    # of equivalence. Failing closed here prevents automatic restamps.
    return judged_change


async def run_build_in_session(
    root: Path,
    config: JauntConfig,
    client: WorkerLike,
    initialized: InitializeResult,
    *,
    target_ids: Sequence[str] = (),
    force: bool = False,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    jobs: int | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
    semantic_gate_enabled: bool | None = None,
    semantic_gate_exec: Callable[..., Awaitable[Any]] | None = None,
    build_instructions: Sequence[str] | None = None,
    ephemeral_prompt: str = "",
    repo_map_block: str | None = None,
    project_overview_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    generation_fingerprint: str | None = None,
    reuse_proof_sink: dict[str, dict[str, str]] | None = None,
) -> TargetBuildReport:
    """Build against an already initialized analyzer session.

    Watch mode uses this entry point so worker caches survive across cycles.
    Normal callers should use :func:`run_build`.
    """

    from jaunt.typescript.status import classify_modules

    root = root.resolve()
    if response_cache is None:
        response_cache = ResponseCache(root / ".jaunt" / "cache")
    effective_jobs = config.build.jobs if jobs is None else jobs
    if effective_jobs < 1:
        raise JauntConfigError("TypeScript build jobs must be >= 1")
    backend = generator or _default_backend(config)
    cost = cost_tracker or CostTracker(max_cost=config.llm.max_cost_per_build)
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    overview_enabled = (
        bool(config.context.overview)
        if project_overview_enabled is None
        else project_overview_enabled
    )
    repo_map_is_enabled = (
        bool(config.context.repo_map) if repo_map_block is None else bool(repo_map_block)
    )
    if repo_map_block is None:
        repo_map_block = ""
        if repo_map_is_enabled:
            from jaunt.repo_context.api import repo_map_block_for_build

            repo_map_block = repo_map_block_for_build(
                root=root,
                cfg=config,
                today=date.today().isoformat(),
            )
    advisories: dict[str, tuple[str, ...]] = {}
    candidates: dict[str, str] = {}
    generated: set[str] = set()
    skipped: set[str] = set()
    refrozen: set[str] = set()
    recomposed: set[str] = set()
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    request_fingerprint = generation_fingerprint or _generation_fingerprint(
        config,
        root=root,
        build_instructions=effective_instructions,
        builtin_skill_names=effective_builtin_skills,
        repo_map_enabled=repo_map_is_enabled,
        project_overview_enabled=overview_enabled,
    )
    analysis = await analyze(client, initialized, target_ids=target_ids)
    previous_target_api_records = capture_target_api_records(root, analysis.modules)
    pending_designs = [
        _module_id(module)
        for module in analysis.modules
        if "@jauntDesign" in str(module.get("specSource", ""))
    ]
    if pending_designs:
        raise JauntConfigError(
            "TypeScript declarations still require reviewable design; run "
            "`jaunt design --target <module#symbol>` first: " + ", ".join(sorted(pending_designs))
        )
    output_preconditions = _artifact_preconditions(root, analysis.modules)
    status = classify_modules(root, analysis.modules)
    gate_enabled = (
        config.semantic_gate.enabled if semantic_gate_enabled is None else semantic_gate_enabled
    )
    for module in analysis.modules:
        module_id = _module_id(module)
        reason = status.stale.get(module_id)
        if not force and reason == "fingerprint":
            refrozen.add(module_id)
        elif not force and reason == "toolchain":
            refrozen.add(module_id)
            recomposed.add(module_id)
        elif not force and reason == "prose" and gate_enabled:
            if await _gate_prose_change(
                root,
                module,
                config,
                cost=cost,
                run_exec=semantic_gate_exec,
            ):
                refrozen.add(module_id)
        elif not force and module_id in status.invalid:
            invalid_codes = {item.code for item in status.invalid[module_id]}
            if invalid_codes and invalid_codes <= {
                "JAUNT_TS_API_DRIFT",
                "JAUNT_TS_FACADE_DRIFT",
            }:
                refrozen.add(module_id)
    selected = [
        module
        for module in analysis.modules
        if force
        or (_module_id(module) in status.stale and _module_id(module) not in refrozen)
        or _module_id(module) in status.unbuilt
        or (_module_id(module) in status.invalid and _module_id(module) not in refrozen)
    ]
    # Validate the selected dependency graph before launching any model work.
    selected = list(_topological_modules(selected))
    selected_ids = {_module_id(module) for module in selected}
    skipped.update(
        _module_id(module)
        for module in analysis.modules
        if _module_id(module) not in selected_ids and _module_id(module) not in refrozen
    )
    by_id = {_module_id(module): module for module in analysis.modules}

    if not selected and not refrozen:
        _clear_recovered_build_manifests(root, analysis.modules)
        _progress_finish(progress)
        return TargetBuildReport(
            language="ts",
            skipped=frozenset(skipped),
            metadata={
                "cost": cost.summary_dict(),
                **(
                    {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                    if response_cache is not None
                    else {}
                ),
            },
        )

    actionable_ids = selected_ids | refrozen
    actionable_modules = [by_id[module_id] for module_id in sorted(actionable_ids)]
    units = _build_units(analysis, actionable_modules)
    failed: dict[str, tuple[TargetDiagnostic, ...]] = {}
    pending = set(selected_ids)
    semaphore = asyncio.Semaphore(effective_jobs)
    progress_advanced: set[str] = set()
    project_overview_block = (
        await _project_overview_block(
            root,
            config,
            analysis.modules,
            repo_map_block=repo_map_block,
            backend=backend,
            cost_tracker=cost,
            enabled=overview_enabled,
        )
        if selected
        else ""
    )

    def selected_dependencies(module_id: str) -> set[str]:
        return set(_dependency_module_ids(by_id[module_id])).intersection(selected_ids)

    def candidate_dependencies(module_id: str) -> dict[str, str]:
        found: dict[str, str] = {}
        stack = list(selected_dependencies(module_id))
        while stack:
            dependency = stack.pop()
            if dependency in found:
                continue
            source = candidates.get(dependency)
            if source is not None:
                found[dependency] = source
            stack.extend(selected_dependencies(dependency))
        return found

    async def generate_one(module_id: str) -> tuple[str, Any]:
        module = by_id[module_id]

        async def candidate_validator(source: str) -> list[str]:
            proposed = {**candidate_dependencies(module_id), module_id: source}
            validation = await validate_overlay(
                client,
                analysis,
                proposed,
                tuple(sorted(proposed)),
                scoped_validation=bool(target_ids),
            )
            if validation.valid:
                return []
            return [
                f"{diagnostic.code}: {diagnostic.message}"
                + (f" ({diagnostic.path})" if diagnostic.path else "")
                for diagnostic in validation.diagnostics
            ] or ["TypeScript overlay validation failed"]

        request = _build_request(
            root,
            config,
            module,
            by_id,
            candidate_validator,
            build_instructions=effective_instructions,
            ephemeral_prompt=ephemeral_prompt,
            repo_map_block=repo_map_block,
            project_overview_block=project_overview_block,
            builtin_skill_names=effective_builtin_skills,
        )
        async with semaphore:
            _progress_phase(progress, module_id, "generating")
            result = await generate_request_cached(
                backend,
                request,
                max_attempts=max_attempts,
                generation_fingerprint=request_fingerprint,
                response_cache=(None if force else response_cache),
                cost_tracker=cost,
                progress=lambda stage, detail: _progress_phase(progress, module_id, stage, detail),
            )
        return module_id, result

    while pending:
        propagated = False
        for module_id in sorted(tuple(pending)):
            blocked = sorted(selected_dependencies(module_id).intersection(failed))
            if not blocked:
                continue
            failed[module_id] = (_dependency_failure(module_id, blocked),)
            pending.remove(module_id)
            _progress_advance(progress, module_id, ok=False)
            progress_advanced.add(module_id)
            propagated = True
        if propagated:
            continue
        ready = sorted(
            module_id
            for module_id in pending
            if not selected_dependencies(module_id).intersection(pending)
        )
        if not ready:
            # _topological_modules already reports a precise cycle. This is a
            # defensive guard against malformed dependency data changing mid-run.
            raise JauntConfigError("TypeScript dependency scheduler made no progress")
        results = await asyncio.gather(*(generate_one(module_id) for module_id in ready))
        for module_id, result in results:
            pending.remove(module_id)
            if result.usage is not None:
                cost.record(module_id, result.usage)
                cost.check_budget()
            if result.source is None or result.errors:
                failed[module_id] = tuple(
                    TargetDiagnostic(code="JAUNT_TS_GENERATION", message=error)
                    for error in result.errors or ["The generator returned no TypeScript source"]
                )
                _progress_advance(progress, module_id, ok=False)
                progress_advanced.add(module_id)
                continue
            candidates[module_id] = result.source
            if result.advisories:
                advisories[module_id] = result.advisories

    # A component is an atomic write boundary. Successful siblings of a failed
    # module are reported as aborted rather than silently appearing generated.
    for unit in units:
        failed_in_unit = sorted(set(unit.module_ids).intersection(failed))
        if not failed_in_unit:
            continue
        for module_id in unit.module_ids:
            if module_id in actionable_ids and module_id not in failed:
                failed[module_id] = (_component_failure(module_id, failed_in_unit),)
            candidates.pop(module_id, None)

    changing_artifacts = {
        _module_path(by_id[module_id], key)
        for module_id in actionable_ids
        for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath")
    }
    immutable_inputs = {
        path: digest
        for path, digest in _input_hashes(analysis.contracts).items()
        if path not in changing_artifacts
    }
    writes: list[_Write] = []
    committed_refrozen: set[str] = set()
    for index, unit in enumerate(units):
        unit_ids = set(unit.module_ids).intersection(actionable_ids)
        if unit_ids.intersection(failed):
            continue
        unit_candidates = {
            module_id: candidates[module_id]
            for module_id in sorted(unit_ids.intersection(candidates))
        }
        unit_refrozen = tuple(sorted(unit_ids.intersection(refrozen)))
        unit_recomposed = tuple(sorted(unit_ids.intersection(recomposed)))
        unit_restamped = tuple(sorted(set(unit_refrozen) - set(unit_recomposed)))
        validated = await validate_overlay(
            client,
            analysis,
            unit_candidates,
            tuple(sorted(set(unit_candidates) | set(unit_refrozen))),
            restamp_module_ids=unit_restamped,
            recompose_module_ids=unit_recomposed,
            scoped_validation=bool(target_ids),
            baseline_unselected=True,
        )
        if not validated.valid:
            diagnostics = _diagnostics(validated.diagnostics)
            for module_id in unit_ids:
                failed[module_id] = diagnostics
            continue
        unit_artifact_paths = {
            _module_path(by_id[module_id], key)
            for module_id in unit_ids
            for key in ("facadePath", "apiMirrorPath", "implementationPath", "sidecarPath")
        }
        unit_writes = atomic_write_manifest(
            root,
            _artifact_writes(validated),
            expected_inputs={
                **immutable_inputs,
                **{path: output_preconditions[path] for path in sorted(unit_artifact_paths)},
            },
        )
        writes.extend(unit_writes)
        generated.update(unit_ids.intersection(candidates))
        committed_refrozen.update(unit_ids.intersection(refrozen))
        later_units = units[index + 1 :]
        if any(
            set(later.module_ids).intersection(actionable_ids)
            and not set(later.module_ids).intersection(failed)
            for later in later_units
        ):
            await client.request(
                "invalidate",
                {"paths": sorted(write.path for write in unit_writes)},
            )
            analysis = await analyze(client, initialized, target_ids=target_ids)
    update_target_api_reuse_proof(
        root,
        before=previous_target_api_records,
        modules=analysis.modules,
        reused_module_ids=committed_refrozen,
        touched_module_ids=set(generated) | committed_refrozen,
    )
    if reuse_proof_sink is not None:
        reuse_proof_sink.clear()
        reuse_proof_sink.update(
            {
                module_id: dict(previous_target_api_records[module_id])
                for module_id in committed_refrozen.intersection(recomposed)
                if module_id in previous_target_api_records
            }
        )
    _clear_recovered_build_manifests(root, analysis.modules)
    for module_id in sorted(actionable_ids - progress_advanced):
        if module_id in committed_refrozen:
            _progress_phase(
                progress,
                module_id,
                "recomposed" if module_id in recomposed else "refrozen",
            )
        _progress_advance(progress, module_id, ok=module_id not in failed)
    _progress_finish(progress)

    append_events(
        root,
        [
            JournalEvent("build", module_id, "TypeScript overlay validated")
            for module_id in sorted(generated)
        ]
        + [
            JournalEvent(
                "recompose" if module_id in recomposed else "refreeze",
                module_id,
                (
                    "TypeScript candidate recomposed for compatible toolchain"
                    if module_id in recomposed
                    else "TypeScript contract equivalent"
                ),
            )
            for module_id in sorted(committed_refrozen)
        ],
    )
    return TargetBuildReport(
        language="ts",
        generated=frozenset(generated),
        skipped=frozenset(skipped),
        refrozen=frozenset(committed_refrozen),
        failed=failed,
        advisories=advisories,
        metadata={
            "cost": cost.summary_dict(),
            "artifacts": tuple(write.path for write in writes),
            "build_units": tuple(unit.module_ids for unit in units),
            "jobs": effective_jobs,
            "generation_fingerprint": request_fingerprint,
            "recomposed": tuple(sorted(committed_refrozen.intersection(recomposed))),
            **(
                {"cache": {"hits": response_cache.hits, "misses": response_cache.misses}}
                if response_cache is not None
                else {}
            ),
        },
        exit_code=3 if failed else 0,
    )


async def run_build(
    root: Path,
    config: JauntConfig,
    *,
    target_ids: Sequence[str] = (),
    force: bool = False,
    generator: GeneratorBackend | None = None,
    cost_tracker: CostTracker | None = None,
    response_cache: ResponseCache | None = None,
    progress: object | None = None,
    worker_factory: WorkerFactory | None = None,
    jobs: int | None = None,
    max_attempts: int = _DEFAULT_ATTEMPTS,
    semantic_gate_enabled: bool | None = None,
    semantic_gate_exec: Callable[..., Awaitable[Any]] | None = None,
    build_instructions: Sequence[str] | None = None,
    ephemeral_prompt: str = "",
    repo_map_enabled: bool | None = None,
    repo_map_block_override: str | None = None,
    auto_skills_enabled: bool | None = None,
    builtin_skill_names: Sequence[str] | None = None,
    reuse_proof_sink: dict[str, dict[str, str]] | None = None,
) -> TargetBuildReport:
    """Generate reserved TypeScript bindings, validate overlays, and commit them."""

    root = root.resolve()
    effective_instructions = (
        tuple(build_instructions)
        if build_instructions is not None
        else tuple(config.build.instructions)
    )
    effective_builtin_skills = (
        tuple(builtin_skill_names)
        if builtin_skill_names is not None
        else (tuple(config.skills.builtin_skills) if config.skills.builtin else ())
    )
    use_repo_map = bool(config.context.repo_map) if repo_map_enabled is None else repo_map_enabled
    target = _target(config)
    use_auto_skills = (
        target.auto_skills_enabled(bool(config.skills.auto))
        if auto_skills_enabled is None
        else auto_skills_enabled
    )
    npm_skill_metadata: Mapping[str, object] = {}
    if use_auto_skills:
        from jaunt.skills_npm import ensure_npm_skills, typescript_package_owners

        npm_skills = ensure_npm_skills(
            project_root=root,
            package_owners=typescript_package_owners(root, target),
            max_readme_chars=config.skills.max_chars_per_skill,
        )
        npm_skill_metadata = npm_skills.metadata()

    repo_map_block = ""
    if use_repo_map and repo_map_block_override is not None:
        repo_map_block = repo_map_block_override
    elif use_repo_map:
        from jaunt.repo_context.api import repo_map_block_for_build

        repo_map_block = repo_map_block_for_build(
            root=root,
            cfg=config,
            today=date.today().isoformat(),
        )
    request_fingerprint = _generation_fingerprint(
        config,
        root=root,
        build_instructions=effective_instructions,
        builtin_skill_names=effective_builtin_skills,
        repo_map_enabled=use_repo_map,
        project_overview_enabled=bool(config.context.overview),
    )
    async with worker_session(
        root,
        config,
        worker_factory=worker_factory,
        generation_fingerprint=request_fingerprint,
    ) as (client, initialized):
        report = await run_build_in_session(
            root,
            config,
            client,
            initialized,
            target_ids=target_ids,
            force=force,
            generator=generator,
            cost_tracker=cost_tracker,
            response_cache=response_cache,
            progress=progress,
            jobs=jobs,
            max_attempts=max_attempts,
            semantic_gate_enabled=semantic_gate_enabled,
            semantic_gate_exec=semantic_gate_exec,
            build_instructions=effective_instructions,
            ephemeral_prompt=ephemeral_prompt,
            repo_map_block=repo_map_block,
            project_overview_enabled=bool(config.context.overview),
            builtin_skill_names=effective_builtin_skills,
            generation_fingerprint=request_fingerprint,
            reuse_proof_sink=reuse_proof_sink,
        )
    if npm_skill_metadata:
        return TargetBuildReport(
            language=report.language,
            generated=report.generated,
            skipped=report.skipped,
            refrozen=report.refrozen,
            failed=report.failed,
            advisories=report.advisories,
            metadata={**report.metadata, "npm_skills": npm_skill_metadata},
            exit_code=report.exit_code,
        )
    return report


__all__ = [
    "SyncReport",
    "TypeScriptAnalysis",
    "WorkerFactory",
    "MISSING_INPUT",
    "analyze",
    "atomic_write_manifest",
    "run_build",
    "run_build_in_session",
    "run_sync",
    "validate_overlay",
    "worker_session",
]
