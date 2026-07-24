from __future__ import annotations

import asyncio
import base64
import gc
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from jaunt.cache import CacheEntry, ResponseCache
from jaunt.config import JauntConfig, load_config
from jaunt.cost import CostTracker
from jaunt.errors import (
    JauntBudgetExceededError,
    JauntConfigError,
    JauntGenerationError,
    JauntQuotaGenerationError,
    JauntTransientGenerationError,
)
from jaunt.generate.base import (
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
)
from jaunt.journal import JournalEvent, append_events as append_journal_events
from jaunt.targets.base import (
    TargetArtifact,
    TargetBuildReport,
    TargetCheckReport,
    TargetDiagnostic,
    TargetStatus,
)
from jaunt.typescript import builder as ts_builder
from jaunt.typescript import design as ts_design
from jaunt.typescript import tester as ts_tester
from jaunt.typescript.builder import (
    MISSING_INPUT,
    _acquire_transaction_lease,
    _build_units,
    _build_request,
    _CommittedBatteryInfrastructureError,
    _dependency_module_ids,
    _gate_prose_change,
    _generation_fingerprint,
    _split_context_source,
    _topological_modules,
    _Write,
    TypeScriptAnalysis,
    analyze,
    atomic_write_manifest,
    run_build,
    run_sync,
    worker_session,
)
from jaunt.typescript.cli_bridge import (
    check_payload,
    human_lines,
    status_payload,
    test_payload as typescript_test_payload,
)
from jaunt.typescript.contracts import (
    _add_contract_tag,
    _battery_request,
    _battery_path,
    _declaration_only_contract,
    _magic_eject_status_reason,
    _ordinary_ejected_source,
    _projection_offset,
    _remove_contract_tag,
    _with_header,
    _with_strength_metadata,
    run_adopt,
    run_eject,
)
from jaunt.typescript.design import (
    _abort_design_manifest,
    _complete_design_manifest,
    _design_output_errors,
    _design_ranges,
    _materialize_magic_stubs,
    _prepare_design_manifest,
    _validate_declaration,
    run_design,
)
from jaunt.typescript.migrate import apply_typescript_migration, plan_typescript_migration
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    PROTOCOL_VERSION,
    WorkspaceStamp,
)
from jaunt.typescript.status import run_check, run_clean, run_status
from jaunt.typescript.tester import (
    _assert_no_held_out_leak,
    _canonical_digest,
    _fixture_resolution_preconditions,
    _HeldOutLeakError,
    _implementation_repair_feedback,
    _imported_type_context_files,
    _implicit_class_test_specs,
    _isolated_test_workspace,
    _is_reviewable_example_battery,
    _module_resolved_test_dependency,
    _redact_runner_result,
    _recover_pending_test_repairs,
    _preserve_managed_files,
    _rejected_test_diagnostic,
    _rejected_test_paths,
    _rejected_test_token,
    _runner_fingerprint,
    _runner_validation_errors,
    _run_test_runner,
    _static_test_validation,
    _strip_test_header,
    _terminate_runner_process,
    _test_header_metadata,
    _test_dependency_runtime_identity,
    _test_provenance,
    _test_request,
    _valid_runner_dto,
    _validate_test_owner_dependencies,
    _with_test_header,
    _write_rejected_test_candidate,
    _clear_rejected_test_candidate,
    run_test,
)
from jaunt.typescript.worker import (
    REQUIRED_WORKER_CAPABILITIES,
    WorkerClient,
    WorkerInstallation,
    WorkerRemoteError,
    WorkerToolchainChangedError,
)


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _utf16_offset(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _projection_ranges(source: str, symbol: str) -> dict[str, int]:
    declaration = source.rfind(f"export function {symbol}")
    assert declaration >= 0
    declaration_end = len(source)
    result = {
        "declarationStart": _utf16_offset(source[:declaration]),
        "declarationEnd": _utf16_offset(source[:declaration_end]),
    }
    docs_start = source.rfind("/**", 0, declaration)
    if docs_start >= 0:
        docs_end = source.find("*/", docs_start, declaration)
        if docs_end >= 0 and not source[docs_end + 2 : declaration].strip():
            result["docsStart"] = _utf16_offset(source[:docs_start])
            result["docsEnd"] = _utf16_offset(source[: docs_end + 2])
    return result


def _config(root: Path) -> JauntConfig:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "fast-check": "^4.0.0",
                    "vitest": "^4.0.0",
                }
            }
        )
        + "\n"
    )
    for package, version in (("fast-check", "4.9.0"), ("vitest", "4.1.10")):
        package_root = root / "node_modules" / package
        package_root.mkdir(parents=True)
        (package_root / "package.json").write_text(
            json.dumps({"name": package, "version": version}) + "\n",
            encoding="utf-8",
        )
        runtime = package_root / "dist/index.js"
        runtime.parent.mkdir()
        runtime.write_text(f"export const packageVersion = {version!r};\n", encoding="utf-8")
    (root / "tsconfig.json").write_text("{}\n")
    (root / "tsconfig.test.json").write_text("{}\n")
    (root / "jaunt.toml").write_text(
        """version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]

[codex]
model = "gpt-5.6-sol"
"""
    )
    return load_config(root=root)


def test_test_owner_requires_direct_vitest_and_fast_check_dependencies(tmp_path: Path) -> None:
    owner = tmp_path / "packages" / "web"
    test_file = owner / "tests" / "value.test.ts"
    test_file.parent.mkdir(parents=True)
    (owner / "tsconfig.test.json").write_text("{}\n", encoding="utf-8")
    (owner / "package.json").write_text("{}\n", encoding="utf-8")
    hoisted = tmp_path / "node_modules" / "vitest"
    hoisted.mkdir(parents=True)
    (hoisted / "package.json").write_text('{"version":"4.1.10"}\n', encoding="utf-8")
    fast_check = tmp_path / "node_modules" / "fast-check"
    fast_check.mkdir(parents=True)
    (fast_check / "package.json").write_text('{"version":"4.9.0"}\n', encoding="utf-8")
    relative_test = test_file.relative_to(tmp_path).as_posix()
    workspace = {
        "projects": [
            {
                "id": "packages/web/tsconfig.test.json",
                "packageOwner": "packages/web",
            }
        ]
    }
    grouped = {"packages/web/tsconfig.test.json": (relative_test,)}

    with pytest.raises(JauntConfigError, match="directly declare.*fast-check, vitest"):
        _validate_test_owner_dependencies(
            tmp_path,
            workspace,
            grouped,
            overlays={relative_test: 'import fc from "fast-check";\n'},
            require_fast_check=True,
        )

    (owner / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "fast-check": "^4.0.0",
                    "vitest": "^4.0.0",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _validate_test_owner_dependencies(
        tmp_path,
        workspace,
        grouped,
        overlays={relative_test: 'import fc from "fast-check";\n'},
        require_fast_check=True,
    )

    (fast_check / "package.json").write_text('{"version":"5.0.0"}\n', encoding="utf-8")
    with pytest.raises(JauntConfigError, match="unsupported fast-check 5.0.0"):
        _validate_test_owner_dependencies(
            tmp_path,
            workspace,
            grouped,
            overlays={relative_test: 'import fc from "fast-check";\n'},
            require_fast_check=True,
        )


def test_vitest_runtime_identity_uses_the_actual_owner_resolved_package(tmp_path: Path) -> None:
    root_owner = tmp_path
    child_owner = tmp_path / "packages/web"
    child_owner.mkdir(parents=True)

    def install(owner: Path, marker: str) -> None:
        package = owner / "node_modules/vitest"
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            '{"name":"vitest","version":"4.1.10","exports":"./dist/index.js"}\n',
            encoding="utf-8",
        )
        (package / "dist/index.js").write_text(
            f"export const owner = {marker!r};\n",
            encoding="utf-8",
        )

    install(root_owner, "root")
    install(child_owner, "child")

    root_identity = _test_dependency_runtime_identity(tmp_path, root_owner, "vitest")
    child_identity = _test_dependency_runtime_identity(tmp_path, child_owner, "vitest")

    assert child_identity != root_identity
    shutil.rmtree(child_owner / "node_modules/vitest")
    assert _test_dependency_runtime_identity(tmp_path, child_owner, "vitest") == root_identity


def test_runner_vitest_resolution_matches_node_peer_context(tmp_path: Path) -> None:
    runner = tmp_path / "node_modules/@usejaunt/ts/dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    runner.write_text("export {};\n", encoding="utf-8")

    def install(package: Path, marker: str) -> None:
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "vitest",
                    "version": "4.1.10",
                    "exports": {"./node": "./dist/node.js"},
                }
            ),
            encoding="utf-8",
        )
        (package / "dist/node.js").write_text(
            f"export const owner = {marker!r};\n",
            encoding="utf-8",
        )

    install(tmp_path / "node_modules/vitest", "owner")
    peer_vitest = tmp_path / "node_modules/@usejaunt/ts/node_modules/vitest"
    install(peer_vitest, "runner-peer")

    resolved = _module_resolved_test_dependency(runner, "vitest")
    assert resolved is not None
    node = shutil.which("node")
    assert node is not None
    node_entry = subprocess.run(
        [
            node,
            "-e",
            (
                "const {createRequire}=require('node:module');"
                "const r=createRequire(process.argv[1]);"
                "process.stdout.write(r.resolve('vitest/node'));"
            ),
            str(runner),
        ],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert Path(node_entry).resolve().is_relative_to(resolved.resolve())
    assert resolved.resolve() == peer_vitest.resolve()


class FakeWorker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.spec_source = (
            'import * as jaunt from "@usejaunt/ts/spec";\n'
            "jaunt.magicModule();\n"
            "/** Double a number. */\n"
            "export function double(value: number): number { return jaunt.magic(); }\n"
        )
        spec = root / "src" / "math.jaunt.ts"
        spec.write_text(self.spec_source)
        self.input_hashes = {"src/math.jaunt.ts": _digest(self.spec_source)}
        self.api = (
            "/** Double a number. */\nexport declare function double(value: number): number;\n"
        )
        semantic_contract = {
            "moduleId": "ts:src/math",
            "specPath": "src/math.jaunt.ts",
            "facadePath": "src/math.ts",
            "apiMirrorPath": "src/__generated__/math.api.ts",
            "implementationPath": "src/__generated__/math.ts",
            "project": "tsconfig.json",
            "packageOwner": ".",
            "symbols": [{"name": "double", "kind": "function"}],
            "options": {},
            "typeDeclarations": [],
            "typeImports": [],
            "contextDocs": [],
            "semanticEnvironmentDigest": "sha256:semantic-environment",
            "dependencies": [],
        }
        self.sidecar = (
            json.dumps(
                {
                    **semantic_contract,
                    "structuralDigest": "sha256:structural",
                    "proseDigest": "sha256:prose",
                    "apiDigest": "sha256:api",
                    "fingerprint": "draft.1",
                },
                sort_keys=True,
            )
            + "\n"
        )
        self.module = {
            **semantic_contract,
            "schema": "contract-ir/1-draft.3",
            "sidecarPath": "src/__generated__/math.jaunt.json",
            "structuralDigest": "sha256:structural",
            "proseDigest": "sha256:prose",
            "apiDigest": "sha256:api",
            "apiSource": self.api,
            "placeholderSource": (
                "// jaunt: state=unbuilt\nexport function double(): never { throw new Error(); }\n"
            ),
            "sidecar": self.sidecar,
            "specSource": self.spec_source,
        }
        self.installation = SimpleNamespace(
            compiler_module_path=root / "node_modules/typescript/lib/typescript.js",
            package_root=root,
            node="node",
        )
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.contracts: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeWorker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialize(self, _params: InitializeParams) -> InitializeResult:
        return InitializeResult(
            worker_version="0.1.0",
            protocol=PROTOCOL_VERSION,
            typescript_version="6.0.2",
            capabilities=REQUIRED_WORKER_CAPABILITIES,
            stamp=WorkspaceStamp("test-session", 1, "snapshot", self.input_hashes),
        )

    def _stamp(self) -> dict[str, Any]:
        return {
            "sessionId": "test-session",
            "epoch": 1,
            "snapshot": "snapshot",
            "inputHashes": self.input_hashes,
        }

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "analyzeWorkspace":
            return {
                **self._stamp(),
                "projects": [{"id": "tsconfig.json"}],
                "routes": [{"moduleId": "ts:src/math", "packageOwner": "."}],
                "specs": [{"moduleId": "ts:src/math"}],
                "testSpecs": [],
                "contracts": self.contracts,
                "diagnostics": [],
            }
        if method == "analyzeContracts":
            return {**self._stamp(), "modules": [self.module]}
        if method == "projectContract":
            source = str(params["source"])
            symbol = str(params["symbol"])
            return {
                "source": f"export function {symbol}(value: number): number;\n",
                "sourceDigest": _digest(source),
                "symbol": symbol,
                "kind": "function",
                **_projection_ranges(source, symbol),
            }
        if method == "findOrphans":
            return {**self._stamp(), "artifacts": []}
        if method == "validateOverlay":
            candidates = params.get("candidates", {})
            real = candidates.get("ts:src/math") if isinstance(candidates, dict) else None
            restamp_mode = bool(params.get("restampModuleIds")) and real is None
            recompose_mode = bool(params.get("recomposeModuleIds")) and real is None
            sync_mode = bool(params.get("syncModuleIds")) and real is None
            preserve_mode = sync_mode or restamp_mode or recompose_mode
            existing_path = self.root / "src/__generated__/math.ts"
            existing = (
                existing_path.read_text() if preserve_mode and existing_path.is_file() else None
            )
            implementation = real or existing or self.module["placeholderSource"]
            implementation_kind = (
                "placeholder" if "state=unbuilt" in str(implementation) else "implementation"
            )
            if implementation_kind == "implementation" and real is not None:
                implementation = (
                    "// ⛓️ jaunt:generated — generated; do not edit.\n"
                    "// jaunt:state=built\n"
                    "// jaunt:module=ts:src/math\n"
                    f"// jaunt:structural={self.module['structuralDigest']}\n"
                    f"// jaunt:prose={self.module['proseDigest']}\n"
                    f"// jaunt:api={self.module['apiDigest']}\n"
                    'import type * as __JauntApi from "./math.api.js";\n\n'
                    f"{implementation}\n"
                    "export const double: typeof __JauntApi.double = "
                    "__jaunt_impl_double;\n"
                )
            elif implementation_kind == "implementation" and (restamp_mode or recompose_mode):
                for key, value in (
                    ("structural", self.module["structuralDigest"]),
                    ("prose", self.module["proseDigest"]),
                    ("api", self.module["apiDigest"]),
                ):
                    implementation = re.sub(
                        rf"(?m)^// jaunt:{key}=.*$",
                        f"// jaunt:{key}={value}",
                        str(implementation),
                    )
                if recompose_mode and "Object.defineProperty(__jaunt_impl_double" not in str(
                    implementation
                ):
                    implementation = str(implementation).replace(
                        "export const double:",
                        'Object.defineProperty(__jaunt_impl_double, "name", '
                        '{ value: "double", configurable: true });\n'
                        "export const double:",
                    )
            facade = 'export * from "./__generated__/math.js";\n'
            sidecar_payload = json.loads(str(self.module["sidecar"]))
            sidecar_payload.update(
                {
                    "state": "unbuilt" if implementation_kind == "placeholder" else "built",
                    "artifactHashes": {
                        "src/math.ts": _digest(facade),
                        "src/__generated__/math.api.ts": _digest(self.api),
                        "src/__generated__/math.ts": _digest(str(implementation)),
                    },
                }
            )
            committed_sidecar = json.dumps(sidecar_payload, sort_keys=True) + "\n"
            artifacts = [
                self._artifact("src/math.ts", facade, "facade"),
                self._artifact("src/__generated__/math.api.ts", self.api, "api-mirror"),
                self._artifact("src/__generated__/math.jaunt.json", committed_sidecar, "sidecar"),
            ]
            if real is not None or existing is None or restamp_mode or recompose_mode:
                artifacts.append(
                    self._artifact(
                        "src/__generated__/math.ts", str(implementation), implementation_kind
                    )
                )
            return {
                **self._stamp(),
                "valid": True,
                "artifacts": artifacts,
                "diagnostics": [],
                "affectedProjects": ["tsconfig.json"],
            }
        raise AssertionError(method)

    @staticmethod
    def _artifact(path: str, content: str, kind: str) -> dict[str, str]:
        return {
            "path": path,
            "content": content,
            "sha256": _digest(content),
            "kind": kind,
            "moduleId": "ts:src/math",
        }


class _RuntimeMutationWorker(FakeWorker):
    runtime_source = "export const workerRuntime = 1;\n"

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        package_root = root / ".tooling/@usejaunt/ts"
        runtime_entry = package_root / "dist/worker.js"
        runtime_entry.parent.mkdir(parents=True)
        runtime_entry.write_text(self.runtime_source, encoding="utf-8")
        for relative in (
            "dist/test/runner.js",
            "dist/test/permission_guard.cjs",
            "dist/test/reporter.js",
            "dist/test/heldout.js",
            "dist/analyzer/artifacts.js",
            "dist/analyzer/diagnostics.js",
            "dist/analyzer/canonical.js",
            "dist/analyzer/provenance.js",
            "dist/protocol/errors.js",
        ):
            support = package_root / relative
            support.parent.mkdir(parents=True, exist_ok=True)
            support.write_text("export {};\n", encoding="utf-8")
        (package_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "@usejaunt/ts",
                    "version": "0.1.1",
                    "exports": {
                        "./worker": "./dist/worker.js",
                        "./test-runner": "./dist/test/runner.js",
                    },
                }
            ),
            encoding="utf-8",
        )
        compiler_root = root / "node_modules/typescript"
        compiler_entry = compiler_root / "lib/typescript.js"
        compiler_entry.parent.mkdir(parents=True, exist_ok=True)
        compiler_entry.write_text("export const version = '6.0.2';\n", encoding="utf-8")
        (compiler_root / "lib/lib.es2024.d.ts").write_text(
            "interface Array<T> { readonly length: number; }\n",
            encoding="utf-8",
        )
        (compiler_root / "package.json").write_text(
            json.dumps(
                {
                    "name": "typescript",
                    "version": "6.0.2",
                    "main": "./lib/typescript.js",
                }
            ),
            encoding="utf-8",
        )
        self.installation = WorkerInstallation(
            node=sys.executable,
            worker_entry=runtime_entry,
            compiler_module_path=compiler_entry,
            package_root=package_root,
            tool_owner=root,
            package_managed=True,
        )
        self._identity_guard = WorkerClient(root=root, installation=self.installation)
        self._runtime_rewrite: str | None = None
        self._remove_runtime = False
        self._mutation_trigger = "recomposeModuleIds"
        self._mutate_after_verification = False
        self._runtime_identity_sealed = False

    async def __aenter__(self) -> _RuntimeMutationWorker:
        self._identity_guard.reset_full_runtime_identity()
        return self

    def arm_runtime_rewrite(
        self,
        source: str,
        *,
        trigger: str = "recomposeModuleIds",
    ) -> None:
        self._runtime_rewrite = source
        self._mutation_trigger = trigger

    def arm_runtime_removal(self, *, trigger: str = "recomposeModuleIds") -> None:
        self._remove_runtime = True
        self._mutation_trigger = trigger

    def arm_runtime_removal_after_next_verification(self) -> None:
        """Remove the runtime after one successful transaction-boundary check."""

        self._remove_runtime = True
        self._mutate_after_verification = True

    def verify_runtime_identity(self) -> str:
        identity = self._identity_guard.verify_runtime_identity()
        if self._mutate_after_verification:
            if self._remove_runtime:
                self.installation.worker_entry.unlink()
                self._remove_runtime = False
            else:
                assert self._runtime_rewrite is not None
                self.installation.worker_entry.write_text(
                    self._runtime_rewrite,
                    encoding="utf-8",
                )
            self._runtime_rewrite = None
            self._mutate_after_verification = False
        return identity

    def pin_full_runtime_identity(self) -> str:
        return self._identity_guard.pin_full_runtime_identity()

    def pin_package_runtime_identity(
        self,
        label: str,
        package_root: Path,
        *,
        expected_name: str | None = None,
    ) -> str:
        return self._identity_guard.pin_package_runtime_identity(
            label,
            package_root,
            expected_name=expected_name,
        )

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
        return self._identity_guard.pin_package_resolution_identity(
            label,
            start,
            package,
            boundary=boundary,
            module_path=module_path,
            expected_name=expected_name,
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
        return self._identity_guard.pin_package_resolution_closure(
            label,
            start,
            package,
            boundary=boundary,
            module_path=module_path,
            expected_name=expected_name,
        )

    def seal_runtime_identity(self) -> str:
        identity = self.verify_runtime_identity()
        self._runtime_identity_sealed = True
        return identity

    async def initialize(self, _params: InitializeParams) -> InitializeResult:
        self.verify_runtime_identity()
        return await super().initialize(_params)

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._runtime_identity_sealed = False
        result = await super().request(method, params)
        if (
            method == "validateOverlay"
            and params.get(self._mutation_trigger)
            and (self._remove_runtime or self._runtime_rewrite is not None)
        ):
            self._mutate_after_verification = True
        return result


class FakeGenerator(GeneratorBackend):
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-ts"

    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any) -> tuple[str, None]:
        raise AssertionError(ctx)

    async def generate_request(
        self, request: GenerationRequest, **_kwargs: Any
    ) -> tuple[str, TokenUsage, tuple[str, ...]]:
        assert request.language == "ts"
        return (
            "const __jaunt_impl_double = (value: number): number => value * 2;\n",
            TokenUsage(20, 10, "fake-ts", "fake"),
            (),
        )


class MutatingGenerator(FakeGenerator):
    def __init__(self, spec: Path) -> None:
        self.spec = spec

    async def generate_request(
        self, request: GenerationRequest, **kwargs: Any
    ) -> tuple[str, TokenUsage, tuple[str, ...]]:
        self.spec.write_text(self.spec.read_text() + "// changed concurrently\n")
        return await super().generate_request(request, **kwargs)


class ExplodingGenerator(FakeGenerator):
    async def generate_request(self, request: GenerationRequest, **kwargs: Any) -> Any:
        raise AssertionError(f"unexpected model call for {request.target_path}")


class _SchedulingWorker:
    def __init__(
        self,
        root: Path,
        modules: list[dict[str, Any]],
        projects: list[dict[str, Any]],
        *,
        reject_combined: bool = False,
    ) -> None:
        self.root = root
        self.modules = modules
        self.projects = projects
        self.reject_combined = reject_combined
        self.epoch = 1
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.input_hashes: dict[str, str] = {}
        for module in modules:
            spec_path = root / str(module["specPath"])
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            source = str(module["specSource"])
            spec_path.write_text(source)
            self.input_hashes[str(module["specPath"])] = _digest(source)
        self.installation = SimpleNamespace(
            compiler_module_path=root / "node_modules/typescript/lib/typescript.js",
            package_root=root,
            node="node",
        )

    async def __aenter__(self) -> _SchedulingWorker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialize(self, _params: InitializeParams) -> InitializeResult:
        return InitializeResult(
            worker_version="0.1.0",
            protocol=PROTOCOL_VERSION,
            typescript_version="6.0.2",
            capabilities=REQUIRED_WORKER_CAPABILITIES,
            stamp=WorkspaceStamp("schedule", self.epoch, "snapshot", self.input_hashes),
        )

    def _stamp(self) -> dict[str, Any]:
        return {
            "sessionId": "schedule",
            "epoch": self.epoch,
            "snapshot": "snapshot",
            "inputHashes": self.input_hashes,
        }

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.requests.append((method, params))
        if method == "analyzeWorkspace":
            return {
                **self._stamp(),
                "projects": self.projects,
                "routes": self.modules,
                "specs": self.modules,
                "testSpecs": [],
                "contracts": [],
                "diagnostics": [],
            }
        if method == "analyzeContracts":
            return {**self._stamp(), "modules": self.modules}
        if method == "invalidate":
            self.epoch += 1
            return {
                **self._stamp(),
                "invalidated": list(params.get("paths", [])),
            }
        if method != "validateOverlay":
            raise AssertionError(method)
        candidates = dict(params.get("candidates", {}))
        if any("__FAIL__" in str(source) for source in candidates.values()) or (
            self.reject_combined and len(candidates) > 1
        ):
            return {
                **self._stamp(),
                "valid": False,
                "artifacts": [],
                "diagnostics": [
                    {
                        "code": "TS_FAIL",
                        "severity": "error",
                        "message": "invalid generated candidate",
                    }
                ],
                "affectedProjects": [],
            }
        artifacts: list[dict[str, str]] = []
        by_id = {str(module["moduleId"]): module for module in self.modules}
        for module_id in params.get("moduleIds", []):
            module = by_id[str(module_id)]
            implementation = str(candidates.get(module_id, "// restamped\n"))
            values = (
                (str(module["facadePath"]), "export {};\n", "facade"),
                (str(module["apiMirrorPath"]), "export {};\n", "api-mirror"),
                (str(module["implementationPath"]), implementation, "implementation"),
                (str(module["sidecarPath"]), "{}\n", "sidecar"),
            )
            artifacts.extend(
                {
                    "path": path,
                    "content": content,
                    "sha256": _digest(content),
                    "kind": kind,
                    "moduleId": str(module_id),
                }
                for path, content, kind in values
            )
        return {
            **self._stamp(),
            "valid": True,
            "artifacts": artifacts,
            "diagnostics": [],
            "affectedProjects": sorted(
                {str(by_id[str(module_id)]["project"]) for module_id in params["moduleIds"]}
            ),
        }


class _SchedulingGenerator(FakeGenerator):
    def __init__(self, *, fail_paths: set[str] | None = None) -> None:
        self.fail_paths = fail_paths or set()
        self.calls: list[str] = []
        self.active = 0
        self.max_active = 0

    async def generate_request(
        self, request: GenerationRequest, **_kwargs: Any
    ) -> tuple[str, TokenUsage, tuple[str, ...]]:
        self.calls.append(request.target_path)
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        source = (
            "const __FAIL__ = true;\n" if request.target_path in self.fail_paths else "export {};\n"
        )
        return source, TokenUsage(1, 1, "fake-ts", "fake"), ()


def _scheduled_module(
    name: str,
    *,
    owner: str,
    project: str = "tsconfig.json",
    dependencies: list[str] | None = None,
) -> dict[str, Any]:
    stem = f"{owner}/src/{name}" if owner != "." else f"src/{name}"
    module_id = f"ts:{stem}"
    return {
        "schema": "contract-ir/1-draft.3",
        "moduleId": module_id,
        "specPath": f"{stem}.jaunt.ts",
        "facadePath": f"{stem}.ts",
        "apiMirrorPath": f"{owner}/src/__generated__/{name}.api.ts"
        if owner != "."
        else f"src/__generated__/{name}.api.ts",
        "implementationPath": f"{owner}/src/__generated__/{name}.ts"
        if owner != "."
        else f"src/__generated__/{name}.ts",
        "sidecarPath": f"{owner}/src/__generated__/{name}.jaunt.json"
        if owner != "."
        else f"src/__generated__/{name}.jaunt.json",
        "project": project,
        "packageOwner": owner,
        "symbols": [{"name": name, "kind": "function"}],
        "dependencies": dependencies or [],
        "structuralDigest": f"sha256:structural-{name}",
        "proseDigest": f"sha256:prose-{name}",
        "apiDigest": f"sha256:api-{name}",
        "apiSource": "export {};\n",
        "placeholderSource": "// jaunt: state=unbuilt\nexport {};\n",
        "sidecar": "{}\n",
        "specSource": f"export declare function {name}(): void;\n",
    }


class DesignGenerator(FakeGenerator):
    async def generate_request(
        self, request: GenerationRequest, **_kwargs: Any
    ) -> tuple[str, TokenUsage, tuple[str, ...]]:
        assert request.kind == "design"
        return (
            "/** Convert a value to a stable slug. */\n"
            "export declare function planned(value: string): string;\n",
            TokenUsage(10, 10, "fake-ts", "fake"),
            (),
        )


class _TestSpecWorker(FakeWorker):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.test_spec_path = "tests/math.jaunt-test.ts"
        (root / self.test_spec_path).write_text("// Verify the public double function.\n")

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await super().request(method, params)
        if method == "analyzeWorkspace":
            result["testSpecs"] = [
                {
                    "path": self.test_spec_path,
                    "project": "tsconfig.test.json",
                    "targets": ["ts:src/math#double"],
                }
            ]
        return result


class _RuntimeMutationTestWorker(_RuntimeMutationWorker):
    def __init__(self, root: Path) -> None:
        self.session_exited = False
        self.verification_session_states: list[bool] = []
        super().__init__(root)
        self.test_spec_path = "tests/math.jaunt-test.ts"
        (root / self.test_spec_path).write_text("// Verify the public double function.\n")

    async def __aexit__(self, *_args: object) -> None:
        self.session_exited = True

    def verify_runtime_identity(self) -> str:
        self.verification_session_states.append(self.session_exited)
        return super().verify_runtime_identity()

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await super().request(method, params)
        if method == "analyzeWorkspace":
            result["testSpecs"] = [
                {
                    "path": self.test_spec_path,
                    "project": "tsconfig.test.json",
                    "targets": ["ts:src/math#double"],
                }
            ]
        return result


def _cost(
    *,
    prompt: int,
    completion: int,
    cached: int = 0,
    estimated: float = 0.0,
) -> dict[str, int | float]:
    return {
        "api_calls": 1,
        "cache_hits": 0,
        "prompt_tokens": prompt,
        "cached_prompt_tokens": cached,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "estimated_cost_usd": estimated,
    }


@pytest.mark.asyncio
async def test_targeted_analysis_ignores_unrelated_file_diagnostics(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    target = config.typescript_target
    assert target is not None
    initialized = await worker.initialize(
        InitializeParams(
            root=str(tmp_path),
            projects=tuple(target.projects),
            test_projects=tuple(target.test_projects),
            source_roots=tuple(target.source_roots),
            test_roots=tuple(target.test_roots),
            generated_dir=target.generated_dir,
            tool_owner=target.tool_owner,
            compiler_module_path="typescript.js",
            client_version="test",
            tool_version="test",
        )
    )
    initialized = replace(
        initialized,
        capabilities=(
            *initialized.capabilities,
            "scoped-diagnostics",
            "scoped-analysis",
            "scoped-validation",
        ),
    )
    original_request = worker.request
    public_import = _scheduled_module("public_type", owner=".")

    async def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        result = await original_request(method, params)
        if method == "analyzeWorkspace":
            result["diagnostics"] = (
                []
                if params.get("moduleIds")
                else [
                    {
                        "code": "JAUNT_TS_UNDECLARED_PACKAGE",
                        "severity": "error",
                        "message": "unrelated package error",
                        "path": "src/unrelated.ts",
                    }
                ]
            )
        if method == "analyzeContracts" and params.get("moduleIds"):
            result["modules"] = [*result["modules"], public_import]
        return result

    worker.request = request  # type: ignore[method-assign]

    targeted = await analyze(cast(Any, worker), initialized, target_ids=("ts:src/math",))
    assert targeted.workspace["diagnostics"] == []
    assert {str(module["moduleId"]) for module in targeted.modules} == {
        "ts:src/math",
        "ts:src/public_type",
    }
    assert worker.requests[-1] == (
        "analyzeContracts",
        {"moduleIds": ["ts:src/math"]},
    )
    with pytest.raises(JauntConfigError, match="unrelated package error"):
        await analyze(cast(Any, worker), initialized)


@pytest.mark.parametrize(
    "source",
    [
        'import { hidden } from "../src/value.jaunt.ts";\n',
        'export { hidden } from "../src/__generated__/value.js";\n',
        'await import(/* deliberate */ "../src/value.jaunt-test.tsx");\n',
        'const hidden = require("../src/__generated__/value.js");\n',
        'import hidden = require("../src/value.jaunt.js");\n',
        'const path = require.resolve("../src/__generated__/value.js");\n',
        'const rendered = `${require("../src/__generated__/value.js")}`;\n',
        'const escaped = require("../src/__genera\\u0074ed__/value.js");\n',
        "const template = require(`../src/__generated__/value.js`);\n",
        'const encoded = require("file:///src/%5F%5Fgenerated%5F%5F/value.js");\n',
        (
            "declare const require: any; const ratio = <any>{} / 2; "
            'const hidden = require("../src/__generated__/value.js");\n'
        ),
        'declare const require: any; const hidden = require?.("../src/__generated__/value.js");\n',
        'declare const require: any; const hidden = (require!)("../src/__generated__/value.js");\n',
    ],
)
def test_static_test_validation_rejects_every_literal_private_module_form(source: str) -> None:
    errors = _static_test_validation(source)
    assert errors
    assert any("private" in error or "public facade" in error for error in errors)


def test_static_test_validation_ignores_inert_or_nonliteral_module_text() -> None:
    source = r"""
// require("../src/__generated__/comment.js")
/* import hidden = require("../src/value.jaunt.ts"); */
const prose = 'require("../src/__generated__/string.js")';
const template = `require("../src/value.jaunt.ts")`;
const pattern = /require("..\/src\/__generated__\/regex.js")/;
const selected = "../src/__generated__/runtime.js";
require(selected);
"""

    assert _static_test_validation(source) == []


@pytest.mark.parametrize(
    "source",
    [
        r'const value = new /ignored import("..\/src\/__generated__\/ghost.js")/'
        r'.constructor("actual");',
        r'export default /require("..\/src\/__generated__\/ghost.js")/;',
        r'class Runner extends /require("..\/src\/__generated__\/ghost.js")/'
        r".constructor {}",
        r'const rendered = `${new /require("..\/src\/__generated__\/ghost.js")/'
        r'.constructor("actual")}`;',
    ],
)
def test_static_test_validation_ignores_regex_after_expression_prefix_keyword(
    source: str,
) -> None:
    assert _static_test_validation(source) == []


@pytest.mark.parametrize("member", ["target.new", "target?.default", "target.extends"])
def test_static_test_validation_keeps_division_after_keyword_named_member(member: str) -> None:
    source = f'const ratio = {member} / require("../src/__generated__/value.js") / divisor;\n'

    assert _static_test_validation(source) == [
        "generated tests must import the public facade, not private generated files"
    ]


def test_static_test_validation_honors_custom_generated_directory() -> None:
    errors = _static_test_validation(
        'const hidden = require("../src/machine/value.js");\n',
        generated_dirs=("machine",),
    )
    assert errors == ["generated tests must import the public facade, not private generated files"]


def test_artifact_transaction_rolls_back_and_clears_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "out/a.ts"
    second = tmp_path / "out/b.ts"
    first.parent.mkdir()
    first.write_text("old-a\n")
    second.write_text("old-b\n")
    original_replace = os.replace

    def fail_second(source: str | Path, destination: str | Path, **kwargs: Any) -> None:
        if Path(destination).name == second.name:
            raise OSError("simulated second replacement failure")
        original_replace(source, destination, **kwargs)

    monkeypatch.setattr("jaunt.typescript.builder.os.replace", fail_second)
    with pytest.raises(OSError, match="simulated"):
        atomic_write_manifest(
            tmp_path,
            (
                _Write("out/a.ts", "new-a\n", "implementation", "ts:a"),
                _Write("out/b.ts", "new-b\n", "implementation", "ts:b"),
            ),
        )

    assert first.read_text() == "old-a\n"
    assert second.read_text() == "old-b\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


def test_artifact_transaction_rolls_back_unconverged_touched_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "out/a.ts"
    output.parent.mkdir()
    output.write_text("old-a\n", encoding="utf-8")
    from jaunt.typescript import builder as typescript_builder

    original_path_hash = typescript_builder._PinnedDirectory.path_hash
    reads = 0

    def stale_post_write_hash(pinned: Any, name: str) -> str | None:
        nonlocal reads
        digest = original_path_hash(pinned, name)
        if pinned.path == output.parent and name == output.name:
            reads += 1
            if reads == 2:
                return "sha256:" + "0" * 64
        return digest

    monkeypatch.setattr(
        typescript_builder._PinnedDirectory,
        "path_hash",
        stale_post_write_hash,
    )
    with pytest.raises(
        JauntGenerationError,
        match="did not converge after commit for ts:a",
    ):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/a.ts", "new-a\n", "implementation", "ts:a"),),
        )

    assert output.read_text(encoding="utf-8") == "old-a\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


def test_artifact_transaction_final_seal_rolls_back_replaced_bytes(tmp_path: Path) -> None:
    output = tmp_path / "out/a.ts"
    output.parent.mkdir()
    output.write_text("old-a\n", encoding="utf-8")
    pre_commit_calls = 0
    seal_calls = 0

    def pre_commit_guard() -> None:
        nonlocal pre_commit_calls
        pre_commit_calls += 1

    def commit_seal() -> None:
        nonlocal seal_calls
        seal_calls += 1
        raise WorkerToolchainChangedError("runtime changed at the final commit boundary")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        atomic_write_manifest(
            tmp_path,
            (_Write("out/a.ts", "new-a\n", "implementation", "ts:a"),),
            pre_commit_guard=pre_commit_guard,
            commit_seal=commit_seal,
        )

    assert pre_commit_calls == 1
    assert seal_calls == 1
    assert output.read_text(encoding="utf-8") == "old-a\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.asyncio
async def test_next_typescript_operation_recovers_killed_test_repair(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    implementation = tmp_path / "src/__generated__/math.ts"
    battery = tmp_path / "tests/__generated__/math.derived.test.ts"
    implementation.parent.mkdir(parents=True)
    battery.parent.mkdir(parents=True)
    implementation.write_text("prior implementation\n", encoding="utf-8")
    battery.write_text("prior derived battery\n", encoding="utf-8")
    script = """
import os
import sys
from pathlib import Path
from jaunt.typescript.builder import _Write
from jaunt.typescript.tester import _preserve_managed_files

root = Path(sys.argv[1])
with _preserve_managed_files(root, []) as transaction:
    transaction.publish(
        (
            _Write(
                "src/__generated__/math.ts",
                "unaccepted repair\\n",
                "implementation",
                "ts:math",
            ),
            _Write(
                "tests/__generated__/math.derived.test.ts",
                "partial candidate battery\\n",
                "test",
                "ts-test:math",
            ),
        ),
        expected_inputs={},
    )
    os._exit(99)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == 99, crashed.stderr
    assert implementation.read_text(encoding="utf-8") == "unaccepted repair\n"
    assert battery.read_text(encoding="utf-8") == "partial candidate battery\n"
    assert tuple((tmp_path / ".jaunt/transactions").glob("test-repair-*.json"))

    await run_status(
        tmp_path,
        config,
        worker_factory=lambda *_: FakeWorker(tmp_path),
    )

    assert implementation.read_text(encoding="utf-8") == "prior implementation\n"
    assert battery.read_text(encoding="utf-8") == "prior derived battery\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("test-repair-*.json"))


def test_test_repair_recovery_waits_for_global_transaction_lease(tmp_path: Path) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("unaccepted repair\n", encoding="utf-8")

    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    manifest = directory / "test-repair-crashed.json"
    manifest.write_text(
        json.dumps(
            {
                "scheme": "jaunt-ts-test-repair/2",
                # A failed retirement from this same process must be recoverable
                # after it releases the authoritative transaction lease.
                "ownerPid": os.getpid(),
                "snapshots": [
                    {
                        "path": "src/__generated__/math.ts",
                        "content": base64.b64encode(b"prior implementation\n").decode("ascii"),
                        "mode": 0o644,
                        "after": _digest("unaccepted repair\n"),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    holder_ready = threading.Event()
    release_holder = threading.Event()
    started = threading.Event()

    def hold_workspace_and_lease() -> None:
        with ts_builder._PinnedWorkspace(tmp_path) as workspace:
            transaction_directory = workspace.directory(directory, create=False)
            lease = _acquire_transaction_lease(
                directory,
                blocking=True,
                pinned_directory=transaction_directory,
                authority_directory=workspace.root_directory,
            )
            assert lease is not None
            holder_ready.set()
            try:
                assert release_holder.wait(timeout=5)
            finally:
                lease.release()

    def recover() -> tuple[str, ...]:
        started.set()
        return _recover_pending_test_repairs(tmp_path)

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_workspace_and_lease)
        assert holder_ready.wait(timeout=5)
        try:
            pending = executor.submit(recover)
            assert started.wait(timeout=5)
            assert not pending.done()
            assert output.read_text(encoding="utf-8") == "unaccepted repair\n"
            assert manifest.is_file()
        finally:
            release_holder.set()
        holder.result(timeout=5)
        assert pending.result(timeout=5) == ("src/__generated__/math.ts",)

    assert output.read_text(encoding="utf-8") == "prior implementation\n"
    expected_lock_files = {".atomic-write.lock"} if os.name == "nt" else set()
    assert {path.name for path in directory.iterdir()} == expected_lock_files


@pytest.mark.skipif(os.name == "nt", reason="Windows root handles serialize before this gap")
def test_test_repair_recovery_waits_when_writer_has_lease_before_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("prior implementation\n", encoding="utf-8")
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    manifest = directory / "test-repair-live.json"
    holder_ready = threading.Event()
    publish_marker = threading.Event()
    marker_published = threading.Event()
    release_holder = threading.Event()
    recovery_attempted_lease = threading.Event()
    role = threading.local()
    original_acquire = ts_tester._acquire_transaction_lease

    def observed_acquire(*args, **kwargs):
        if getattr(role, "recovery", False):
            recovery_attempted_lease.set()
        return original_acquire(*args, **kwargs)

    def hold_then_publish() -> None:
        with ts_builder._PinnedWorkspace(tmp_path) as workspace:
            transaction_directory = workspace.directory(directory, create=False)
            lease = _acquire_transaction_lease(
                directory,
                blocking=True,
                pinned_directory=transaction_directory,
                authority_directory=workspace.root_directory,
            )
            assert lease is not None
            holder_ready.set()
            try:
                assert publish_marker.wait(timeout=5)
                output.write_text("unaccepted repair\n", encoding="utf-8")
                manifest.write_text(
                    json.dumps(
                        {
                            "scheme": "jaunt-ts-test-repair/2",
                            "ownerPid": os.getpid(),
                            "snapshots": [
                                {
                                    "path": "src/__generated__/math.ts",
                                    "content": base64.b64encode(b"prior implementation\n").decode(
                                        "ascii"
                                    ),
                                    "mode": 0o644,
                                    "after": _digest("unaccepted repair\n"),
                                }
                            ],
                        }
                    )
                    + "\n",
                    encoding="utf-8",
                )
                marker_published.set()
                assert release_holder.wait(timeout=5)
            finally:
                lease.release()

    def recover() -> tuple[str, ...]:
        role.recovery = True
        return _recover_pending_test_repairs(tmp_path)

    monkeypatch.setattr(ts_tester, "_acquire_transaction_lease", observed_acquire)
    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_then_publish)
        assert holder_ready.wait(timeout=5)
        try:
            pending = executor.submit(recover)
            assert recovery_attempted_lease.wait(timeout=5)
            assert not pending.done()
            publish_marker.set()
            assert marker_published.wait(timeout=5)
            assert output.read_text(encoding="utf-8") == "unaccepted repair\n"
        finally:
            release_holder.set()
            publish_marker.set()
        holder.result(timeout=5)
        assert pending.result(timeout=5) == ("src/__generated__/math.ts",)

    assert output.read_text(encoding="utf-8") == "prior implementation\n"
    assert not manifest.exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_test_repair_publication_does_not_follow_managed_file_symlink(tmp_path: Path) -> None:
    output_directory = tmp_path / "src/__generated__"
    output_directory.mkdir(parents=True)
    victim = output_directory / "victim.ts"
    victim.write_text("victim bytes\n", encoding="utf-8")
    managed = output_directory / "math.ts"
    managed.symlink_to(victim.name)

    with pytest.raises(JauntConfigError, match="Could not snapshot managed repair path"):
        with _preserve_managed_files(
            tmp_path,
            ["src/__generated__/math.ts"],
        ) as transaction:
            transaction.publish(
                (
                    _Write(
                        "src/__generated__/math.ts",
                        "candidate bytes\n",
                        "implementation",
                        "ts:math",
                    ),
                ),
                expected_inputs={},
            )

    assert managed.is_symlink()
    assert victim.read_text(encoding="utf-8") == "victim bytes\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_test_repair_recovery_does_not_follow_managed_file_symlink(tmp_path: Path) -> None:
    output_directory = tmp_path / "src/__generated__"
    output_directory.mkdir(parents=True)
    victim = output_directory / "victim.ts"
    victim.write_text("unaccepted repair\n", encoding="utf-8")
    managed = output_directory / "math.ts"
    managed.symlink_to(victim.name)
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    manifest = directory / "test-repair-crashed.json"
    manifest.write_text(
        json.dumps(
            {
                "scheme": "jaunt-ts-test-repair/2",
                "ownerPid": os.getpid(),
                "snapshots": [
                    {
                        "path": "src/__generated__/math.ts",
                        "content": base64.b64encode(b"prior implementation\n").decode("ascii"),
                        "mode": 0o644,
                        "after": _digest("unaccepted repair\n"),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="Could not inspect TypeScript test-repair path"):
        _recover_pending_test_repairs(tmp_path)

    assert managed.is_symlink()
    assert victim.read_text(encoding="utf-8") == "unaccepted repair\n"
    assert manifest.is_file()


def test_test_repair_recovery_keeps_marker_when_retirement_is_not_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("unaccepted repair\n", encoding="utf-8")
    terminated = subprocess.Popen([sys.executable, "-c", "pass"])
    owner_pid = terminated.pid
    assert terminated.wait(timeout=5) == 0
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    manifest = directory / "test-repair-crashed.json"
    manifest.write_text(
        json.dumps(
            {
                "scheme": "jaunt-ts-test-repair/2",
                "ownerPid": owner_pid,
                "snapshots": [
                    {
                        "path": "src/__generated__/math.ts",
                        "content": base64.b64encode(b"prior implementation\n").decode("ascii"),
                        "mode": 0o644,
                        "after": _digest("unaccepted repair\n"),
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    original_fsync = ts_builder._PinnedDirectory.fsync_required

    def fail_transaction_directory_sync(pinned: Any) -> None:
        if pinned.path.resolve() == directory.resolve():
            raise OSError("simulated directory sync failure")
        original_fsync(pinned)

    monkeypatch.setattr(
        ts_builder._PinnedDirectory,
        "fsync_required",
        fail_transaction_directory_sync,
    )
    with pytest.raises(JauntConfigError, match="durably retire.*test-repair marker"):
        _recover_pending_test_repairs(tmp_path)

    assert output.read_text(encoding="utf-8") == "prior implementation\n"
    assert manifest.is_file()


def test_test_repair_outer_transaction_holds_lease_until_commit(tmp_path: Path) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("original\n", encoding="utf-8")
    started = threading.Event()

    def publish_newer() -> None:
        started.set()
        atomic_write_manifest(
            tmp_path,
            (_Write("src/__generated__/math.ts", "newer\n", "implementation", "ts:math"),),
        )

    executor = ThreadPoolExecutor(max_workers=1)
    pending = None
    try:
        with _preserve_managed_files(tmp_path, ["src/__generated__/math.ts"]) as transaction:
            transaction.publish(
                (
                    _Write(
                        "src/__generated__/math.ts",
                        "repair\n",
                        "implementation",
                        "ts:math",
                    ),
                ),
                expected_inputs={},
            )
            pending = executor.submit(publish_newer)
            assert started.wait(timeout=5)
            assert not pending.done()
            assert output.read_text(encoding="utf-8") == "repair\n"
            transaction.commit()
        pending.result(timeout=5)
    finally:
        executor.shutdown(wait=True)

    assert output.read_text(encoding="utf-8") == "newer\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


def test_test_repair_refuses_an_unresolved_foreign_transaction(tmp_path: Path) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("original\n", encoding="utf-8")
    transaction_directory = tmp_path / ".jaunt/transactions"
    transaction_directory.mkdir(parents=True)
    marker = transaction_directory / "legacy.json"
    marker.write_text('{"state":"prepared"}\n', encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="legacy.json"):
        with _preserve_managed_files(tmp_path, ["src/__generated__/math.ts"]):
            raise AssertionError("unresolved transaction must block entry")

    assert output.read_text(encoding="utf-8") == "original\n"
    assert marker.is_file()


def test_test_repair_rollback_cas_preserves_newer_artifact(tmp_path: Path) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("original\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="post-publication failure"):
        with _preserve_managed_files(tmp_path, ["src/__generated__/math.ts"]) as transaction:
            transaction.publish(
                (
                    _Write(
                        "src/__generated__/math.ts",
                        "repair\n",
                        "implementation",
                        "ts:math",
                    ),
                ),
                expected_inputs={},
            )
            # Model an external writer that ignores Jaunt's advisory lease.
            output.write_text("newer implementation\n", encoding="utf-8")
            raise RuntimeError("post-publication failure")

    assert output.read_text(encoding="utf-8") == "newer implementation\n"
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


def test_test_repair_reuses_pinned_directories_for_publish_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outputs = (
        tmp_path / "src/__generated__/one.ts",
        tmp_path / "src/__generated__/nested/two.ts",
    )
    for output in outputs:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(f"original {output.stem}\n", encoding="utf-8")

    original_directory = ts_tester._PinnedWorkspace.directory
    pins: list[tuple[object, Path, object]] = []

    def track_directory(
        workspace: Any,
        directory: Path,
        *,
        create: bool = True,
    ) -> Any:
        pinned = original_directory(workspace, directory, create=create)
        pins.append((workspace, directory, pinned))
        return pinned

    monkeypatch.setattr(ts_tester._PinnedWorkspace, "directory", track_directory)

    with pytest.raises(RuntimeError, match="rollback after publication"):
        with _preserve_managed_files(
            tmp_path,
            [path.relative_to(tmp_path).as_posix() for path in outputs],
        ) as transaction:
            transaction.publish(
                tuple(
                    _Write(
                        path.relative_to(tmp_path).as_posix(),
                        f"repair {path.stem}\n",
                        "implementation",
                        f"ts:{path.stem}",
                    )
                    for path in outputs
                ),
                expected_inputs={},
            )
            raise RuntimeError("rollback after publication")

    assert len({id(workspace) for workspace, _path, _pinned in pins}) == 1
    pins_by_path: dict[Path, set[int]] = {}
    for _workspace, path, pinned in pins:
        pins_by_path.setdefault(path.resolve(), set()).add(id(pinned))
    assert all(len(identities) == 1 for identities in pins_by_path.values())
    assert {path.parent.resolve() for path in outputs} <= pins_by_path.keys()
    assert [path.read_text(encoding="utf-8") for path in outputs] == [
        "original one\n",
        "original two\n",
    ]


def test_test_repair_removes_registered_temp_when_staging_fsync_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("original\n", encoding="utf-8")
    original_create_temp = ts_tester._PinnedDirectory.create_temp
    original_fsync = ts_tester.os.fsync
    staged_descriptor: int | None = None

    def track_repair_temp(
        pinned: Any,
        prefix: str,
        suffix: str = "",
    ) -> tuple[int, str]:
        nonlocal staged_descriptor
        descriptor, name = original_create_temp(pinned, prefix, suffix)
        if "jaunt-repair" in prefix:
            staged_descriptor = descriptor
        return descriptor, name

    def fail_staged_fsync(descriptor: int) -> None:
        if descriptor == staged_descriptor:
            raise OSError("simulated staging fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(ts_tester._PinnedDirectory, "create_temp", track_repair_temp)
    monkeypatch.setattr(ts_tester.os, "fsync", fail_staged_fsync)

    with _preserve_managed_files(tmp_path, ["src/__generated__/math.ts"]) as transaction:
        with pytest.raises(OSError, match="staging fsync failure"):
            transaction.publish(
                (
                    _Write(
                        "src/__generated__/math.ts",
                        "repair\n",
                        "implementation",
                        "ts:math",
                    ),
                ),
                expected_inputs={},
            )

    assert output.read_text(encoding="utf-8") == "original\n"
    assert not tuple(output.parent.glob(".math.ts.jaunt-repair-*"))


def test_test_repair_recovery_cas_preserves_newer_artifact(tmp_path: Path) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    output.parent.mkdir(parents=True)
    output.write_text("newer implementation\n", encoding="utf-8")
    terminated = subprocess.Popen([sys.executable, "-c", "pass"])
    owner_pid = terminated.pid
    assert terminated.wait(timeout=5) == 0
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    manifest = directory / "test-repair-crashed.json"
    manifest.write_text(
        json.dumps(
            {
                "scheme": "jaunt-ts-test-repair/2",
                "ownerPid": owner_pid,
                "snapshots": [
                    {
                        "path": "src/__generated__/math.ts",
                        "content": base64.b64encode(b"original\n").decode("ascii"),
                        "mode": 0o644,
                        "after": _digest("repair\n"),
                    },
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    assert _recover_pending_test_repairs(tmp_path) == ()
    assert output.read_text(encoding="utf-8") == "newer implementation\n"
    assert not manifest.exists()


def test_test_repair_outer_retirement_failure_rolls_back_and_keeps_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output = tmp_path / "src/__generated__/math.ts"
    directory = tmp_path / ".jaunt/transactions"
    output.parent.mkdir(parents=True)
    output.write_text("original\n", encoding="utf-8")

    with pytest.raises(JauntConfigError, match="durably retire.*test-repair marker"):
        with _preserve_managed_files(tmp_path, ["src/__generated__/math.ts"]) as transaction:
            transaction.publish(
                (
                    _Write(
                        "src/__generated__/math.ts",
                        "repair\n",
                        "implementation",
                        "ts:math",
                    ),
                ),
                expected_inputs={},
            )
            original_fsync = ts_builder._PinnedDirectory.fsync_required

            def fail_transaction_directory_sync(pinned: Any) -> None:
                if pinned.path.resolve() == directory.resolve():
                    raise OSError("simulated directory sync failure")
                original_fsync(pinned)

            monkeypatch.setattr(
                ts_builder._PinnedDirectory,
                "fsync_required",
                fail_transaction_directory_sync,
            )
            transaction.commit()

    assert output.read_text(encoding="utf-8") == "original\n"
    assert tuple((tmp_path / ".jaunt/transactions").glob("test-repair-*.json"))


def test_sigkill_during_isolated_model_repair_never_mutates_or_exposes_held_out_files(
    tmp_path: Path,
) -> None:
    implementation = tmp_path / "src/__generated__/math.ts"
    example = tmp_path / "tests/__generated__/math.example.test.ts"
    derived = tmp_path / "tests/__generated__/math.derived.test.ts"
    dependency = tmp_path / "node_modules/example/index.js"
    implementation.parent.mkdir(parents=True)
    example.parent.mkdir(parents=True)
    dependency.parent.mkdir(parents=True)
    implementation.write_text("prior implementation\n", encoding="utf-8")
    example.write_text("prior example\n", encoding="utf-8")
    derived.write_text("HELD-OUT-SECRET\n", encoding="utf-8")
    dependency.write_text("export {};\n", encoding="utf-8")
    before = {path: path.read_bytes() for path in (implementation, example, derived)}
    script = """
import os
import signal
import sys
from pathlib import Path
from jaunt.typescript.tester import _isolated_test_repair_workspace, _with_test_header

root = Path(sys.argv[1])
candidate = _with_test_header(
    "export {};\\n",
    tier="example",
    source_path="tests/math.jaunt-test.ts",
)
files = (
    "tests/__generated__/math.example.test.ts",
    "tests/__generated__/math.derived.test.ts",
)
with _isolated_test_repair_workspace(
    root,
    files,
    {files[0]: candidate, files[1]: "HELD-OUT-CANDIDATE\\n"},
) as repair_root:
    print(repair_root, flush=True)
    assert (repair_root / files[0]).read_text(encoding="utf-8") == candidate
    assert not (repair_root / files[1]).exists()
    assert not (repair_root / "node_modules").is_symlink()
    assert not (
        repair_root / "node_modules" / ".." / "tests" / "__generated__" /
        "math.derived.test.ts"
    ).exists()
    (repair_root / "src/__generated__/math.ts").write_text(
        "unaccepted isolated repair\\n", encoding="utf-8"
    )
    os.kill(os.getpid(), signal.SIGKILL)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert crashed.returncode == -signal.SIGKILL, crashed.stderr
    for path, content in before.items():
        assert path.read_bytes() == content
    repair_root = Path(crashed.stdout.strip())
    assert not (repair_root / "tests/__generated__/math.derived.test.ts").exists()
    shutil.rmtree(repair_root.parent, ignore_errors=True)


@pytest.mark.asyncio
async def test_bubblewrap_tier_view_hides_absolute_and_proc_root_paths(tmp_path: Path) -> None:
    config = _config(tmp_path)
    bubblewrap = shutil.which("bwrap")
    if bubblewrap is None:
        pytest.skip("bubblewrap is not installed")
    example = "tests/__generated__/math.example.test.ts"
    derived = "tests/__generated__/math.derived.test.ts"
    example_source = _with_test_header(
        "export {};\n",
        tier="example",
        source_path="tests/math.jaunt-test.ts",
    )
    (tmp_path / example).parent.mkdir(parents=True)
    (tmp_path / example).write_text(example_source, encoding="utf-8")
    sentinel = "HELD-OUT-BWRAP-SENTINEL"
    (tmp_path / derived).write_text(sentinel, encoding="utf-8")
    runner = tmp_path / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    original_battery = str((tmp_path / derived).resolve())
    proc_battery = f"/proc/1/root{original_battery}"
    runner.write_text(
        """
import fs from "node:fs";
process.stdin.resume();
let input = "";
process.stdin.on("data", chunk => { input += chunk; });
process.stdin.on("end", () => {
  const candidates = %s;
  const leaked = candidates.some(path => {
    try { return fs.readFileSync(path, "utf8").includes(%s); }
    catch { return false; }
  });
  const test = leaked
    ? {file: %s, tier: "example", status: "failed", durationMs: 0,
       caseId: "0123456789abcdef", category: "runtime", message: %s}
    : {file: %s, tier: "example", status: "passed", durationMs: 0};
  process.stdout.write(JSON.stringify({
    ok: !leaked, mode: "run", diagnostics: [], tests: [test],
    captured: {stdout: "", stderr: ""}
  }));
});
"""
        % (
            json.dumps([original_battery, proc_battery]),
            json.dumps(sentinel),
            json.dumps(example),
            json.dumps(sentinel),
            json.dumps(example),
        ),
        encoding="utf-8",
    )
    compiler = tmp_path / "node_modules/typescript/lib/typescript.js"
    compiler.parent.mkdir(parents=True, exist_ok=True)
    compiler.write_text("export {};\n", encoding="utf-8")
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node=shutil.which("node") or "node",
            package_root=tmp_path,
            compiler_module_path=compiler,
        )
    )

    with _isolated_test_workspace(
        tmp_path,
        (example, derived),
        {},
        tier="example",
    ) as isolated:
        result = await _run_test_runner(
            client,
            isolated,
            config,
            files=(example,),
            tier="example",
            isolated_from=tmp_path,
        )

    assert result["ok"] is True
    assert sentinel not in json.dumps(result)


@pytest.mark.asyncio
async def test_sync_is_model_free_and_keeps_unbuilt_status(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    report = await run_sync(tmp_path, config, worker_factory=lambda *_: worker)

    assert report.ok
    assert report.mirrors == ("src/__generated__/math.api.ts",)
    assert report.placeholders == ("src/__generated__/math.ts",)
    assert report.created_facades == ("src/math.ts",)
    assert "state=unbuilt" in (tmp_path / "src/__generated__/math.ts").read_text()

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert status.unbuilt == frozenset({"ts:src/math"})
    assert not status.fresh


@pytest.mark.asyncio
async def test_build_validates_candidate_before_atomic_write(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    report = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert "__jaunt_impl_double" in (tmp_path / "src/__generated__/math.ts").read_text()
    validate_calls = [params for method, params in worker.requests if method == "validateOverlay"]
    assert len(validate_calls) == 2  # retry validator, then final manifested overlay
    assert validate_calls[0]["candidates"]["ts:src/math"].startswith("const __jaunt_impl")

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert status.fresh == frozenset({"ts:src/math"})


@pytest.mark.asyncio
async def test_fresh_build_does_not_construct_default_generator(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    initial_worker = FakeWorker(tmp_path)
    initial = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: initial_worker,
    )
    assert initial.exit_code == 0

    factory_calls = 0

    def unexpected_generator() -> GeneratorBackend:
        nonlocal factory_calls
        factory_calls += 1
        raise AssertionError("a fresh TypeScript build must not construct a generator")

    fresh_worker = FakeWorker(tmp_path)
    fresh = await run_build(
        tmp_path,
        config,
        generator_factory=unexpected_generator,
        worker_factory=lambda *_: fresh_worker,
    )

    assert fresh.exit_code == 0
    assert fresh.skipped == frozenset({"ts:src/math"})
    assert factory_calls == 0


@pytest.mark.asyncio
async def test_build_rejects_cycle_before_semantic_gate_or_generator(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class CyclicWorker(FakeWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            first = {
                **self.module,
                "dependencies": ["ts:src/other#other"],
            }
            second = {
                **self.module,
                "moduleId": "ts:src/other",
                "specPath": "src/other.jaunt.ts",
                "facadePath": "src/other.ts",
                "apiMirrorPath": "src/__generated__/other.api.ts",
                "implementationPath": "src/__generated__/other.ts",
                "sidecarPath": "src/__generated__/other.jaunt.json",
                "dependencies": ["ts:src/math#double"],
                "symbols": [{"name": "other", "kind": "function"}],
            }
            self.modules = [first, second]

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "analyzeWorkspace":
                self.requests.append((method, params))
                return {
                    **self._stamp(),
                    "projects": [{"id": "tsconfig.json"}],
                    "routes": [
                        {"moduleId": module["moduleId"], "packageOwner": "."}
                        for module in self.modules
                    ],
                    "specs": [{"moduleId": module["moduleId"]} for module in self.modules],
                    "testSpecs": [],
                    "contracts": [],
                    "diagnostics": [],
                }
            if method == "analyzeContracts":
                self.requests.append((method, params))
                return {**self._stamp(), "modules": self.modules}
            return await super().request(method, params)

    async def unexpected_semantic_gate(**_kwargs: Any) -> object:
        raise AssertionError("cycle detection must precede semantic model calls")

    def unexpected_generator() -> GeneratorBackend:
        raise AssertionError("cycle detection must precede generator construction")

    with pytest.raises(JauntConfigError, match="TypeScript dependency cycle"):
        await run_build(
            tmp_path,
            config,
            generator_factory=unexpected_generator,
            semantic_gate_exec=unexpected_semantic_gate,
            worker_factory=lambda *_: CyclicWorker(tmp_path),
        )


@pytest.mark.asyncio
async def test_build_reuses_accepted_candidate_after_final_overlay_internal_error(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class FailingFinalOverlayWorker(FakeWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.overlay_calls = 0

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "validateOverlay":
                self.overlay_calls += 1
                if self.overlay_calls == 2:
                    raise WorkerRemoteError(
                        code="INTERNAL_ERROR",
                        message=(
                            "validateOverlay failed during phase=module-overlays: "
                            "synthetic compiler reuse failure"
                        ),
                        retryable=False,
                        diagnostics=(),
                    )
            return await super().request(method, params)

    worker = FailingFinalOverlayWorker(tmp_path)
    response_cache = ResponseCache(tmp_path / ".jaunt" / "cache")
    with pytest.raises(WorkerRemoteError, match="phase=module-overlays"):
        await run_build(
            tmp_path,
            config,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
            response_cache=response_cache,
            repo_map_enabled=False,
            auto_skills_enabled=False,
        )

    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
        response_cache=response_cache,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert report.metadata["cost"]["cache_hits"] == 1
    assert worker.overlay_calls == 4


@pytest.mark.asyncio
async def test_build_repairs_candidate_rejected_by_final_unit_conformance(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class FinalConformanceWorker(FakeWorker):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if (
                method == "validateOverlay"
                and params.get("baselineUnselected")
                and "__FINAL_FAIL__" in str(params.get("candidates", {}))
            ):
                return {
                    **self._stamp(),
                    "valid": False,
                    "artifacts": [],
                    "diagnostics": [
                        {
                            "code": "TS2322",
                            "severity": "error",
                            "message": "optional content_blocks was narrowed to required",
                            "path": "src/__generated__/math.ts",
                        },
                        {
                            "code": "TS2322",
                            "severity": "error",
                            "message": "optional content_blocks was narrowed to required",
                            "path": "src/__generated__/math.ts",
                        },
                    ],
                    "affectedProjects": ["tsconfig.json"],
                }
            return await super().request(method, params)

    class RepairingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0
            self.feedback: list[list[str] | None] = []

        async def generate_request(
            self,
            request: GenerationRequest,
            **kwargs: Any,
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            extra_error_context = kwargs.get("extra_error_context")
            self.calls += 1
            self.feedback.append(
                extra_error_context if isinstance(extra_error_context, list) else None
            )
            source = (
                "const __FINAL_FAIL__ = true;\n"
                if self.calls == 1
                else "const __jaunt_impl_double = (value: number): number => value * 2;\n"
            )
            return source, TokenUsage(2, 1, "fake-ts", "fake"), ()

    generator = RepairingGenerator()
    report = await run_build(
        tmp_path,
        config,
        generator=generator,
        worker_factory=lambda *_: FinalConformanceWorker(tmp_path),
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert report.exit_code == 0
    assert generator.calls == 2
    assert generator.feedback[1] == [
        "previous output errors: TS2322: optional content_blocks was narrowed to required "
        "(src/__generated__/math.ts)"
    ]
    outcome = report.metadata["candidate_outcomes"]["ts:src/math"]
    assert outcome == {
        "attempts": 2,
        "retry_count": 1,
        "retry_reasons": (
            "TS2322: optional content_blocks was narrowed to required (src/__generated__/math.ts)",
        ),
        "phase": "committed",
    }


@pytest.mark.asyncio
async def test_build_retries_candidate_rejected_by_committed_target_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    committed = implementation.read_bytes()

    async def candidate_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        overlays = kwargs.get("overlays", {})
        candidate = str(overlays.get("src/__generated__/math.ts", ""))
        if kwargs.get("typecheck_only") or "value + 1" not in candidate:
            return {
                "ok": True,
                "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
                "tests": [],
                "diagnostics": [],
            }
        assert implementation.read_bytes() == committed
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "path": "tests/__generated__/math.example.test.ts",
                    "ok": False,
                }
            ],
            "failures": [
                {
                    "category": "assertion",
                    "path": "tests/__generated__/math.example.test.ts",
                }
            ],
        }

    class BadThenGoodGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            operator = "+ 1" if self.calls == 1 else "* 2"
            return (
                f"const __jaunt_impl_double = (value: number): number => value {operator};\n",
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", candidate_batches)
    generator = BadThenGoodGenerator()
    report = await run_build(
        tmp_path,
        config,
        force=True,
        max_attempts=2,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".candidate-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert generator.calls == 2
    assert "value * 2" in implementation.read_text(encoding="utf-8")
    outcome = report.metadata["candidate_outcomes"]["ts:src/math"]
    assert outcome["retry_count"] == 1
    assert any("JAUNT_TS_COMMITTED_BATTERY" in reason for reason in outcome["retry_reasons"])


@pytest.mark.asyncio
async def test_build_retries_candidate_rejected_by_committed_battery_typecheck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    async def candidate_typecheck(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        candidate = str(kwargs.get("overlays", {}).get("src/__generated__/math.ts", ""))
        if kwargs.get("typecheck_only") and "value + 1" in candidate:
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "diagnostics": [
                    {
                        "code": "TS2322",
                        "message": "candidate breaks the committed battery type surface",
                        "severity": "error",
                    }
                ],
            }
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class BadThenGoodGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            operator = "+ 1" if self.calls == 1 else "* 2"
            return (
                f"const __jaunt_impl_double = (value: number): number => value {operator};\n",
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", candidate_typecheck)
    generator = BadThenGoodGenerator()
    report = await run_build(
        tmp_path,
        config,
        force=True,
        max_attempts=2,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".candidate-typecheck-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert generator.calls == 2
    outcome = report.metadata["candidate_outcomes"]["ts:src/math"]
    assert outcome["retry_count"] == 1
    assert any("TS2322" in reason for reason in outcome["retry_reasons"])


@pytest.mark.asyncio
async def test_build_validates_committed_battery_after_runner_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    runner = worker.installation.package_root / "dist/test/runner.js"
    runner.write_text("export const changedRunner = true;\n", encoding="utf-8")

    validation_modes: list[str] = []

    async def validate_with_current_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        mode = "typecheck" if kwargs.get("typecheck_only") else "run"
        validation_modes.append(mode)
        overlays = kwargs.get("overlays", {})
        regressed = isinstance(overlays, Mapping) and any(
            "+ 1" in str(source) for source in overlays.values()
        )
        if mode == "run" and regressed:
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        "file": "tests/__generated__/math.example.test.ts",
                        "tier": "example",
                        "status": "failed",
                        "caseId": "runner-rebuild-gate",
                        "category": "assertion",
                    }
                ],
                "diagnostics": [],
            }
        return {"ok": True, "mode": mode, "tests": [], "diagnostics": []}

    class BadThenGoodGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **_kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            operator = "+ 1" if self.calls == 1 else "* 2"
            return (
                f"const __jaunt_impl_double = (value: number): number => value {operator};\n",
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", validate_with_current_runner)
    generator = BadThenGoodGenerator()
    report = await run_build(
        tmp_path,
        config,
        force=True,
        max_attempts=2,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".runner-rebuild-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert generator.calls == 2
    assert validation_modes == ["typecheck", "run"] * 3


@pytest.mark.asyncio
async def test_build_rolls_back_when_runner_changes_after_committed_battery_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert built.exit_code == seeded.exit_code == 0
    implementation = tmp_path / "src/__generated__/math.ts"
    before = implementation.read_bytes()
    runner = worker.installation.package_root / "dist/test/runner.js"
    mutated = False

    async def mutate_after_gate(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal mutated
        mode = "typecheck" if kwargs.get("typecheck_only") else "run"
        if mode == "run" and not mutated:
            runner.write_text("export const rebuiltAfterGate = true;\n", encoding="utf-8")
            mutated = True
        return {"ok": True, "mode": mode, "tests": [], "diagnostics": []}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", mutate_after_gate)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_build(
            tmp_path,
            config,
            force=True,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert mutated is True
    assert implementation.read_bytes() == before


@pytest.mark.asyncio
async def test_build_binds_committed_battery_and_vitest_closure_to_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.typescript_target is not None
    config = replace(
        config,
        typescript_target=replace(
            config.typescript_target,
            vitest_config="vitest.config.ts",
        ),
    )
    (tmp_path / "vitest.config.ts").write_text(
        'export default { test: { setupFiles: ["tests/setup.ts"] } };\n',
        encoding="utf-8",
    )
    setup = tmp_path / "tests/setup.ts"
    setup.write_text('export const setupVersion = "v1";\n', encoding="utf-8")
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert built.exit_code == seeded.exit_code == 0
    battery_relative = "tests/__generated__/math.example.test.ts"
    battery = tmp_path / battery_relative
    battery_source = battery.read_text(encoding="utf-8")
    setup_source = setup.read_text(encoding="utf-8")
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation_before = implementation.read_bytes()
    observed_captured_overlays = False

    async def observe_captured_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal observed_captured_overlays
        overlays = kwargs.get("overlays", {})
        if battery_relative in overlays:
            assert overlays[battery_relative] == battery_source
            assert overlays["tests/setup.ts"] == setup_source
            observed_captured_overlays = True
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        observe_captured_batches,
    )
    real_atomic_write = ts_builder.atomic_write_manifest
    mutated = False

    def mutate_inputs_before_publication(*args: Any, **kwargs: Any) -> tuple[Any, ...]:
        nonlocal mutated
        expected_inputs = kwargs.get("expected_inputs", {})
        assert battery_relative in expected_inputs
        assert "vitest.config.ts" in expected_inputs
        assert "tests/setup.ts" in expected_inputs
        if not mutated:
            battery.write_text(battery_source + "// concurrent edit\n", encoding="utf-8")
            setup.write_text('export const setupVersion = "v2";\n', encoding="utf-8")
            mutated = True
        return real_atomic_write(*args, **kwargs)

    monkeypatch.setattr(ts_builder, "atomic_write_manifest", mutate_inputs_before_publication)
    with pytest.raises(JauntGenerationError, match="inputs changed after analysis"):
        await run_build(
            tmp_path,
            config,
            force=True,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert observed_captured_overlays
    assert mutated
    assert implementation.read_bytes() == implementation_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "category",
    ["runner", "runner-protocol", "timeout", "typecheck-runner-protocol", "typecheck-timeout"],
)
async def test_committed_battery_runner_failure_does_not_retry_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    committed = implementation.read_bytes()

    async def unavailable_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            if category.startswith("typecheck-"):
                failure_category = category.removeprefix("typecheck-")
                return {
                    "ok": False,
                    "mode": "typecheck",
                    "tests": [],
                    "failures": [{"category": failure_category}],
                    **({"timedOut": True} if failure_category == "timeout" else {}),
                }
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        assert not category.startswith("typecheck-")
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": category}],
            **({"timedOut": True} if category == "timeout" else {}),
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unavailable_runner)
    generator = CountingGenerator()
    report = await run_build(
        tmp_path,
        config,
        force=True,
        max_attempts=3,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".runner-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 3
    assert generator.calls == 1
    assert implementation.read_bytes() == committed
    assert {item.code for item in report.failed["ts:src/math"]} == {
        "JAUNT_TS_COMMITTED_BATTERY_INFRASTRUCTURE"
    }
    outcome = report.metadata["candidate_outcomes"]["ts:src/math"]
    assert outcome["attempts"] == 1
    assert outcome["retry_count"] == 0
    assert outcome["retry_reasons"] == ()


@pytest.mark.asyncio
async def test_final_committed_battery_runner_failure_skips_unit_repair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    runtime_calls = 0

    async def final_runner_failure(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_calls += 1
        if runtime_calls == 1:
            return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": "runner-protocol"}],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", final_runner_failure)
    generator = CountingGenerator()
    report = await run_build(
        tmp_path,
        config,
        force=True,
        max_attempts=3,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".final-runner-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 3
    assert runtime_calls == 2
    assert generator.calls == 1
    assert {item.code for item in report.failed["ts:src/math"]} == {
        "JAUNT_TS_COMMITTED_BATTERY_INFRASTRUCTURE"
    }
    outcome = report.metadata["candidate_outcomes"]["ts:src/math"]
    assert outcome["attempts"] == 1
    assert outcome["retry_count"] == 0


@pytest.mark.asyncio
async def test_committed_battery_infrastructure_caches_paid_candidate_for_next_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.unlink()
    runner_available = False

    async def battery_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        if runner_available:
            return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": "runner-protocol"}],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", battery_runner)
    generator = CountingGenerator()
    response_cache = ResponseCache(tmp_path / ".preserved-build-candidate-cache")
    first = await run_build(
        tmp_path,
        config,
        max_attempts=3,
        generator=generator,
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert first.exit_code == 3
    assert generator.calls == 1
    assert response_cache.info()["entries"] == 1
    assert not implementation.exists()
    assert first.metadata["candidate_outcomes"]["ts:src/math"]["attempts"] == 1

    runner_available = True
    second = await run_build(
        tmp_path,
        config,
        generator=generator,
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert second.exit_code == 0
    assert generator.calls == 1
    assert implementation.is_file()
    assert second.metadata["cost"]["cache_hits"] == 1
    assert second.metadata["candidate_outcomes"]["ts:src/math"]["attempts"] == 0


@pytest.mark.asyncio
async def test_final_repair_battery_infrastructure_preserves_attempt_and_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    class FinalConformanceWorker(_TestSpecWorker):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if (
                method == "validateOverlay"
                and params.get("baselineUnselected")
                and "__FINAL_FAIL__" in str(params.get("candidates", {}))
            ):
                return {
                    **self._stamp(),
                    "valid": False,
                    "artifacts": [],
                    "diagnostics": [
                        {
                            "code": "TS2322",
                            "severity": "error",
                            "message": "final unit candidate is incompatible",
                            "path": "src/__generated__/math.ts",
                        }
                    ],
                    "affectedProjects": ["tsconfig.json"],
                }
            return await super().request(method, params)

    worker = FinalConformanceWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.unlink()
    runner_available = False
    runtime_calls = 0

    async def repair_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_calls += 1
        if runner_available or runtime_calls == 1:
            return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": "runner"}],
        }

    class RepairingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            source = (
                "const __FINAL_FAIL__ = true;\n"
                if self.calls == 1
                else "const __jaunt_impl_double = (value: number): number => value * 2;\n"
            )
            return source, TokenUsage(20, 10, "fake-ts", "fake"), ()

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", repair_runner)
    generator = RepairingGenerator()
    response_cache = ResponseCache(tmp_path / ".preserved-repair-candidate-cache")
    first = await run_build(
        tmp_path,
        config,
        max_attempts=3,
        generator=generator,
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert first.exit_code == 3
    assert generator.calls == 2
    assert first.metadata["cost"]["api_calls"] == 2
    assert first.metadata["candidate_outcomes"]["ts:src/math"]["attempts"] == 2
    assert first.metadata["candidate_outcomes"]["ts:src/math"]["retry_count"] == 1
    assert response_cache.info()["entries"] == 1
    assert not implementation.exists()

    runner_available = True
    second = await run_build(
        tmp_path,
        config,
        generator=generator,
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert second.exit_code == 0
    assert generator.calls == 2
    assert second.metadata["cost"]["cache_hits"] == 1
    assert implementation.is_file()


@pytest.mark.asyncio
async def test_targeted_build_expands_analysis_for_independent_multi_target_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    class MultiTargetWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.reject_full_workspace = False
            triple_source = (
                'import * as jaunt from "@usejaunt/ts/spec";\n'
                "jaunt.magicModule();\n"
                "/** Triple a number. */\n"
                "export function triple(value: number): number { return jaunt.magic(); }\n"
            )
            triple_spec = root / "src/triple.jaunt.ts"
            triple_spec.write_text(triple_source, encoding="utf-8")
            self.input_hashes["src/triple.jaunt.ts"] = _digest(triple_source)
            triple_sidecar = json.loads(self.sidecar)
            triple_sidecar.update(
                {
                    "moduleId": "ts:src/triple",
                    "specPath": "src/triple.jaunt.ts",
                    "facadePath": "src/triple.ts",
                    "apiMirrorPath": "src/__generated__/triple.api.ts",
                    "implementationPath": "src/__generated__/triple.ts",
                    "symbols": [{"name": "triple", "kind": "function"}],
                    "structuralDigest": "sha256:triple-structural",
                    "proseDigest": "sha256:triple-prose",
                    "apiDigest": "sha256:triple-api",
                }
            )
            self.triple = {
                **self.module,
                **triple_sidecar,
                "sidecarPath": "src/__generated__/triple.jaunt.json",
                "apiSource": (
                    "/** Triple a number. */\n"
                    "export declare function triple(value: number): number;\n"
                ),
                "sidecar": json.dumps(triple_sidecar, sort_keys=True) + "\n",
                "specSource": triple_source,
            }
            quad_source = triple_source.replace("Triple", "Quadruple").replace(
                "triple", "quadruple"
            )
            quad_spec = root / "src/quadruple.jaunt.ts"
            quad_spec.write_text(quad_source, encoding="utf-8")
            self.input_hashes["src/quadruple.jaunt.ts"] = _digest(quad_source)
            quad_sidecar = json.loads(self.sidecar)
            quad_sidecar.update(
                {
                    "moduleId": "ts:src/quadruple",
                    "specPath": "src/quadruple.jaunt.ts",
                    "facadePath": "src/quadruple.ts",
                    "apiMirrorPath": "src/__generated__/quadruple.api.ts",
                    "implementationPath": "src/__generated__/quadruple.ts",
                    "symbols": [{"name": "quadruple", "kind": "function"}],
                    "structuralDigest": "sha256:quadruple-structural",
                    "proseDigest": "sha256:quadruple-prose",
                    "apiDigest": "sha256:quadruple-api",
                }
            )
            self.quadruple = {
                **self.module,
                **quad_sidecar,
                "sidecarPath": "src/__generated__/quadruple.jaunt.json",
                "apiSource": (
                    "/** Quadruple a number. */\n"
                    "export declare function quadruple(value: number): number;\n"
                ),
                "sidecar": json.dumps(quad_sidecar, sort_keys=True) + "\n",
                "specSource": quad_source,
            }
            self.test_spec_path = "tests/multi.jaunt-test.ts"
            (root / self.test_spec_path).write_text("// Verify double, triple, and quadruple.\n")

        async def initialize(self, _params: InitializeParams) -> InitializeResult:
            initialized = await super().initialize(_params)
            return replace(
                initialized,
                capabilities=tuple(
                    dict.fromkeys(
                        (
                            *initialized.capabilities,
                            "scoped-diagnostics",
                            "scoped-analysis",
                        )
                    )
                ),
            )

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "analyzeWorkspace":
                requested = set(params.get("moduleIds", []))
                modules = [
                    module
                    for module in (self.module, self.triple, self.quadruple)
                    if not requested or str(module["moduleId"]) in requested
                ]
                result["routes"] = modules
                result["specs"] = modules
                result["testSpecs"] = [
                    {
                        "path": self.test_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": [
                            "ts:src/math#double",
                            "ts:src/triple#triple",
                            "ts:src/quadruple#quadruple",
                        ],
                    }
                ]
                if self.reject_full_workspace and not requested:
                    result["diagnostics"] = [
                        {
                            "code": "TS_UNRELATED_C",
                            "severity": "error",
                            "message": "Unrelated module C is invalid.",
                            "path": "src/unrelated-c.jaunt.ts",
                        }
                    ]
            elif method == "analyzeContracts":
                requested = set(params.get("moduleIds", []))
                available = (self.module, self.triple, self.quadruple)
                result["modules"] = [
                    module
                    for module in available
                    if not requested or str(module["moduleId"]) in requested
                ]
                if self.reject_full_workspace and {
                    "ts:src/triple",
                    "ts:src/quadruple",
                }.issubset(requested):
                    result["modules"] = [self.triple]
            return result

    worker = MultiTargetWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    built = await run_build(
        tmp_path,
        config,
        target_ids=("ts:src/math",),
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    triple_sidecar = tmp_path / "src/__generated__/triple.jaunt.json"
    triple_sidecar.parent.mkdir(parents=True, exist_ok=True)
    triple_sidecar.write_text(str(worker.triple["sidecar"]), encoding="utf-8")
    quadruple_sidecar = tmp_path / "src/__generated__/quadruple.jaunt.json"
    quadruple_sidecar.write_text(str(worker.quadruple["sidecar"]), encoding="utf-8")
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    worker.reject_full_workspace = True
    worker.requests.clear()
    report = await run_build(
        tmp_path,
        config,
        target_ids=("ts:src/math",),
        force=True,
        generator=FakeGenerator(),
        response_cache=ResponseCache(tmp_path / ".multi-target-gate-cache"),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert built.exit_code == 0
    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert any(
        method == "analyzeContracts"
        and set(params.get("moduleIds", [])) == {"ts:src/triple", "ts:src/quadruple"}
        for method, params in worker.requests
    )
    assert any(
        method == "analyzeContracts" and set(params.get("moduleIds", [])) == {"ts:src/quadruple"}
        for method, params in worker.requests
    )
    assert all(
        set(params.get("moduleIds", [])) == {"ts:src/math"}
        for method, params in worker.requests
        if method == "analyzeWorkspace"
    )


@pytest.mark.asyncio
async def test_build_bounds_parallel_generation_by_jobs_and_owner_units(tmp_path: Path) -> None:
    config = _config(tmp_path)
    modules = [
        _scheduled_module("alpha", owner="packages/a"),
        _scheduled_module("beta", owner="packages/b"),
        _scheduled_module("gamma", owner="packages/c"),
    ]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )
    generator = _SchedulingGenerator()

    report = await run_build(
        tmp_path,
        config,
        force=True,
        jobs=2,
        max_attempts=1,
        generator=generator,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset(str(module["moduleId"]) for module in modules)
    assert generator.max_active == 2
    assert report.metadata["jobs"] == 2
    assert len(report.metadata["build_units"]) == 3


@pytest.mark.asyncio
async def test_parallel_battery_infrastructure_attempt_accounting_is_task_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    modules = [
        _scheduled_module("alpha", owner="packages/a"),
        _scheduled_module("beta", owner="packages/b"),
    ]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )
    beta_validated = asyncio.Event()

    async def committed_gate(*_args: Any, **kwargs: Any) -> list[str]:
        module_ids = tuple(kwargs.get("module_ids", ()))
        if module_ids == ("ts:packages/b/src/beta",):
            beta_validated.set()
            return []
        if module_ids == ("ts:packages/a/src/alpha",):
            await beta_validated.wait()
            raise _CommittedBatteryInfrastructureError(("runner unavailable",))
        return []

    monkeypatch.setattr(
        "jaunt.typescript.tester._validate_committed_target_batteries",
        committed_gate,
    )
    response_cache = ResponseCache(tmp_path / ".parallel-infrastructure-cache")
    report = await run_build(
        tmp_path,
        config,
        force=True,
        jobs=2,
        generator=_SchedulingGenerator(),
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert report.exit_code == 3
    assert report.generated == frozenset({"ts:packages/b/src/beta"})
    alpha = report.metadata["candidate_outcomes"]["ts:packages/a/src/alpha"]
    beta = report.metadata["candidate_outcomes"]["ts:packages/b/src/beta"]
    assert alpha["attempts"] == 1
    assert alpha["retry_count"] == 0
    assert beta["attempts"] == 1
    assert report.metadata["cost"]["api_calls"] == 2
    assert response_cache.info()["entries"] == 1


@pytest.mark.asyncio
async def test_analysis_batches_large_contract_responses(tmp_path: Path) -> None:
    modules = [_scheduled_module(f"module_{index}", owner=".") for index in range(9)]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )
    initialized = await worker.initialize(
        InitializeParams(
            root=str(tmp_path),
            projects=("tsconfig.json",),
            test_projects=(),
            source_roots=("src",),
            test_roots=("tests",),
            generated_dir="__generated__",
            tool_owner=".",
            compiler_module_path="typescript.js",
            client_version="test",
            tool_version="test",
        )
    )

    analysis = await analyze(cast(Any, worker), initialized)

    batches = [
        params["moduleIds"] for method, params in worker.requests if method == "analyzeContracts"
    ]
    assert [len(batch) for batch in batches] == [4, 4, 1]
    assert len(analysis.modules) == 9


@pytest.mark.asyncio
async def test_sync_validates_dependency_ordered_bounded_batches(tmp_path: Path) -> None:
    config = _config(tmp_path)
    dependency = _scheduled_module("z_dependency", owner=".")
    dependent = _scheduled_module(
        "a_dependent",
        owner=".",
        dependencies=[f"{dependency['moduleId']}#value"],
    )
    modules = [dependent, *[_scheduled_module(f"module_{index}", owner=".") for index in range(7)]]
    modules.append(dependency)
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )

    report = await run_sync(tmp_path, config, worker_factory=lambda *_: worker)

    assert report.ok
    batches = [
        params["moduleIds"] for method, params in worker.requests if method == "validateOverlay"
    ]
    assert [len(batch) for batch in batches] == [4, 4, 1]
    ordered = [module_id for batch in batches for module_id in batch]
    assert ordered.index(dependency["moduleId"]) < ordered.index(dependent["moduleId"])


@pytest.mark.asyncio
async def test_build_propagates_dependency_failure_but_commits_unrelated_owner(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    dependency = _scheduled_module("dependency", owner="packages/a")
    dependent = _scheduled_module(
        "dependent",
        owner="packages/b",
        dependencies=[f"{dependency['moduleId']}#dependency"],
    )
    unrelated = _scheduled_module("unrelated", owner="packages/c")
    modules = [dependency, dependent, unrelated]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )
    generator = _SchedulingGenerator(fail_paths={str(dependency["implementationPath"])})

    report = await run_build(
        tmp_path,
        config,
        force=True,
        jobs=3,
        max_attempts=1,
        generator=generator,
        worker_factory=lambda *_: worker,
    )

    dependency_id = str(dependency["moduleId"])
    dependent_id = str(dependent["moduleId"])
    unrelated_id = str(unrelated["moduleId"])
    assert report.exit_code == 3
    assert report.generated == frozenset({unrelated_id})
    assert report.failed[dependency_id][0].code == "JAUNT_TS_GENERATION"
    assert report.failed[dependent_id][0].code == "JAUNT_TS_DEPENDENCY_FAILED"
    assert str(dependent["implementationPath"]) not in generator.calls
    assert not (tmp_path / str(dependency["implementationPath"])).exists()
    assert not (tmp_path / str(dependent["implementationPath"])).exists()
    assert (tmp_path / str(unrelated["implementationPath"])).is_file()


@pytest.mark.asyncio
async def test_build_commits_independent_same_owner_modules(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    first = _scheduled_module("first", owner="packages/together")
    second = _scheduled_module("second", owner="packages/together")
    unrelated = _scheduled_module("unrelated", owner="packages/apart")
    modules = [first, second, unrelated]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
        reject_combined=True,
    )

    report = await run_build(
        tmp_path,
        config,
        force=True,
        jobs=3,
        max_attempts=1,
        generator=_SchedulingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset(str(module["moduleId"]) for module in modules)
    assert report.failed == {}
    assert (tmp_path / str(first["implementationPath"])).is_file()
    assert (tmp_path / str(second["implementationPath"])).is_file()
    assert (tmp_path / str(unrelated["implementationPath"])).is_file()
    validations = [params for method, params in worker.requests if method == "validateOverlay"]
    landing_validations = [params for params in validations if params.get("baselineUnselected")]
    assert len(landing_validations) == len(modules)
    assert any(method == "invalidate" for method, _params in worker.requests)


@pytest.mark.asyncio
async def test_invalid_candidate_does_not_abort_independent_same_owner_modules(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    failing = _scheduled_module("failing", owner="packages/together")
    sibling = _scheduled_module("sibling", owner="packages/together")
    modules = [failing, sibling]
    worker = _SchedulingWorker(
        tmp_path,
        modules,
        [{"id": "tsconfig.json", "references": []}],
    )
    generator = _SchedulingGenerator(fail_paths={str(failing["implementationPath"])})

    report = await run_build(
        tmp_path,
        config,
        force=True,
        jobs=2,
        max_attempts=1,
        generator=generator,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert report.generated == frozenset({str(sibling["moduleId"])})
    assert set(report.failed) == {str(failing["moduleId"])}
    assert not (tmp_path / str(failing["implementationPath"])).exists()
    assert (tmp_path / str(sibling["implementationPath"])).is_file()


def test_build_units_union_only_explicit_dependencies(tmp_path: Path) -> None:
    initialized = InitializeResult(
        worker_version="0.1.0",
        protocol=PROTOCOL_VERSION,
        typescript_version="6.0.2",
        capabilities=REQUIRED_WORKER_CAPABILITIES,
        stamp=WorkspaceStamp("units", 1, "snapshot", {}),
    )
    core = _scheduled_module(
        "core",
        owner="packages/core",
        project="packages/core/tsconfig.json",
    )
    app = _scheduled_module(
        "app",
        owner="packages/app",
        project="packages/app/tsconfig.json",
        dependencies=[f"{core['moduleId']}#core"],
    )
    lone = _scheduled_module("lone", owner="packages/lone", project="tsconfig.lone.json")
    consumer = _scheduled_module(
        "consumer",
        owner="packages/consumer",
        project="tsconfig.consumer.json",
        dependencies=[f"{lone['moduleId']}#lone"],
    )
    independent = _scheduled_module(
        "independent",
        owner="packages/app",
        project="packages/app/tsconfig.json",
    )
    analysis = TypeScriptAnalysis(
        initialized=initialized,
        workspace={
            "projects": [
                {
                    "id": "tsconfig.solution.json",
                    "role": "solution",
                    "references": [
                        "packages/core/tsconfig.json",
                        "packages/app/tsconfig.json",
                        "tsconfig.lone.json",
                        "tsconfig.consumer.json",
                    ],
                },
                {"id": "packages/core/tsconfig.json", "references": []},
                {
                    "id": "packages/app/tsconfig.json",
                    "references": ["packages/core/tsconfig.json"],
                },
                {"id": "tsconfig.lone.json", "references": []},
                {"id": "tsconfig.consumer.json", "references": []},
            ]
        },
        contracts={"modules": [core, app, lone, consumer, independent]},
    )

    units = _build_units(analysis, analysis.modules)

    assert {frozenset(unit.module_ids) for unit in units} == {
        frozenset({str(core["moduleId"]), str(app["moduleId"])}),
        frozenset({str(lone["moduleId"]), str(consumer["moduleId"])}),
        frozenset({str(independent["moduleId"])}),
    }


def test_module_order_ignores_same_module_symbol_dependencies() -> None:
    module = {
        "moduleId": "ts:src/tokens/index",
        "dependencies": [
            "ts:src/tokens/index#createToken",
            "ts:src/shared/index#normalize",
        ],
    }
    shared = {"moduleId": "ts:src/shared/index", "dependencies": []}

    assert _dependency_module_ids(module) == ("ts:src/shared/index",)
    assert [item["moduleId"] for item in _topological_modules((module, shared))] == [
        "ts:src/shared/index",
        "ts:src/tokens/index",
    ]


def test_ephemeral_build_feedback_changes_only_the_invocation_prompt(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)

    async def validator(_source: str) -> list[str]:
        return []

    ordinary = _build_request(
        tmp_path,
        config,
        worker.module,
        {"ts:src/math": worker.module},
        validator,
    )
    repair = _build_request(
        tmp_path,
        config,
        worker.module,
        {"ts:src/math": worker.module},
        validator,
        ephemeral_prompt="opaque runner feedback",
    )

    assert "opaque runner feedback" not in ordinary.prompt
    assert "Ephemeral validation feedback" in repair.prompt
    assert "opaque runner feedback" in repair.prompt
    assert repair.cache_payload == ordinary.cache_payload
    assert repair.context_files == ordinary.context_files
    assert _generation_fingerprint(config, root=tmp_path) == _generation_fingerprint(
        config,
        root=tmp_path,
    )


def test_build_request_keeps_imported_type_transport_out_of_authored_context(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    module = {
        **worker.module,
        "contextSource": (
            'export const authored = "preserved";\n\n'
            "// <jaunt:imported-type-context version=1>\n"
            '// <jaunt:imported-type-source {"id":"workspace:src/types.ts",'
            '"priority":"requested"}>\n'
            "export interface InternalTransport { id: string; }\n"
            "// </jaunt:imported-type-source>\n"
            "// </jaunt:imported-type-context>\n"
        ),
    }

    async def validator(_source: str) -> list[str]:
        return []

    request = _build_request(
        tmp_path,
        config,
        module,
        {str(module["moduleId"]): module},
        validator,
    )

    assert request.context_files["_context/context.ts"] == (
        'export const authored = "preserved";\n'
    )
    assert "InternalTransport" not in request.context_files["_context/context.ts"]
    assert "jaunt:imported-type-context" not in request.context_files["_context/context.ts"]


@pytest.mark.asyncio
async def test_build_refuses_to_commit_after_input_snapshot_changes(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)

    with pytest.raises(JauntGenerationError, match="inputs changed after analysis"):
        await run_build(
            tmp_path,
            config,
            generator=MutatingGenerator(tmp_path / "src/math.jaunt.ts"),
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "src/__generated__/math.ts").exists()


@pytest.mark.asyncio
async def test_sync_never_replaces_a_real_implementation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.parent.mkdir()
    implementation.write_text("// jaunt:state=built\nconst real = true;\n")

    report = await run_sync(tmp_path, config, worker_factory=lambda *_: worker)

    assert report.ok
    assert report.placeholders == ()
    assert implementation.read_text() == "// jaunt:state=built\nconst real = true;\n"


@pytest.mark.asyncio
async def test_status_classifies_contract_ir_change_as_structural(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    sidecar = json.loads(worker.sidecar)
    sidecar["structuralDigest"] = "sha256:new-structure"
    sidecar["symbols"] = [{"name": "triple", "kind": "function"}]
    worker.module["structuralDigest"] = "sha256:new-structure"
    worker.module["symbols"] = sidecar["symbols"]
    worker.module["sidecar"] = json.dumps(sidecar, sort_keys=True) + "\n"

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)

    assert status.stale == {"ts:src/math": "structural"}


@pytest.mark.asyncio
async def test_toolchain_digest_drift_recomposes_without_model(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    expected = json.loads(worker.sidecar)
    expected["structuralDigest"] = "sha256:new-digest-scheme"
    expected["apiDigest"] = "sha256:new-api-digest-scheme"
    worker.module["structuralDigest"] = expected["structuralDigest"]
    worker.module["apiDigest"] = expected["apiDigest"]
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert status.stale == {"ts:src/math": "toolchain"}
    assert report.generated == frozenset()
    assert report.refrozen == frozenset({"ts:src/math"})
    assert report.metadata["recomposed"] == ("ts:src/math",)
    validation = [params for method, params in worker.requests if method == "validateOverlay"]
    assert validation[-1]["recomposeModuleIds"] == ["ts:src/math"]
    implementation = (tmp_path / "src/__generated__/math.ts").read_text()
    assert 'Object.defineProperty(__jaunt_impl_double, "name"' in implementation


@pytest.mark.asyncio
async def test_worker_session_preserves_body_error_during_runtime_change(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class BodyError(RuntimeError):
        pass

    class VerifyingExitWorker(_RuntimeMutationWorker):
        exit_exception_type: object = None

        async def __aexit__(self, *args: object) -> None:
            exc_type = args[0] if args else None
            self.exit_exception_type = exc_type
            if exc_type is None and not self._runtime_identity_sealed:
                self.verify_runtime_identity()

    worker = VerifyingExitWorker(tmp_path)
    with pytest.raises(BodyError, match="operation failed"):
        async with worker_session(
            tmp_path,
            config,
            worker_factory=lambda *_: worker,
        ):
            worker.installation.worker_entry.unlink()
            raise BodyError("operation failed")

    assert worker.exit_exception_type is BodyError


@pytest.mark.asyncio
async def test_build_rolls_back_when_worker_runtime_changes_during_artifact_commit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationWorker(tmp_path)
    worker.arm_runtime_removal(trigger="moduleIds")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ) as raised:
        await run_build(
            tmp_path,
            config,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert raised.value.code == "JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD"
    assert not any(
        path.exists()
        for path in (
            tmp_path / "src/math.ts",
            tmp_path / "src/__generated__/math.api.ts",
            tmp_path / "src/__generated__/math.ts",
            tmp_path / "src/__generated__/math.jaunt.json",
        )
    )


@pytest.mark.asyncio
async def test_build_seals_final_commit_before_clean_worker_exit(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class PostSealMutationWorker(_RuntimeMutationWorker):
        exit_was_sealed = False

        def seal_runtime_identity(self) -> str:
            identity = super().seal_runtime_identity()
            self.installation.worker_entry.write_text(
                self.runtime_source + "// rebuilt after commit seal\n",
                encoding="utf-8",
            )
            return identity

        async def __aexit__(self, *args: object) -> None:
            exc_type = args[0] if args else None
            self.exit_was_sealed = self._runtime_identity_sealed
            if exc_type is None and not self._runtime_identity_sealed:
                self.verify_runtime_identity()

    worker = PostSealMutationWorker(tmp_path)
    report = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert worker.exit_was_sealed is True
    assert (tmp_path / "src/__generated__/math.ts").is_file()


@pytest.mark.asyncio
async def test_test_battery_commit_rolls_back_when_runner_runtime_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    class RunnerMutationWorker(_RuntimeMutationTestWorker):
        def seal_runtime_identity(self) -> str:
            identity = super().seal_runtime_identity()
            heldout = self.installation.package_root / "dist/test/heldout.js"
            heldout.write_text("export const heldout = 'rebuilt';\n", encoding="utf-8")
            return identity

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    worker = RunnerMutationWorker(tmp_path)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()


@pytest.mark.asyncio
async def test_sync_rolls_back_when_worker_runtime_changes_during_artifact_commit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationWorker(tmp_path)
    worker.arm_runtime_removal(trigger="syncModuleIds")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ) as raised:
        await run_sync(
            tmp_path,
            config,
            worker_factory=lambda *_: worker,
        )

    assert raised.value.code == "JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD"
    assert not any(
        path.exists()
        for path in (
            tmp_path / "src/math.ts",
            tmp_path / "src/__generated__/math.api.ts",
            tmp_path / "src/__generated__/math.ts",
            tmp_path / "src/__generated__/math.jaunt.json",
        )
    )


@pytest.mark.asyncio
async def test_refreeze_rolls_back_when_worker_runtime_changes_during_artifact_commit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    artifact_paths = (
        tmp_path / "src/math.ts",
        tmp_path / "src/__generated__/math.api.ts",
        tmp_path / "src/__generated__/math.ts",
        tmp_path / "src/__generated__/math.jaunt.json",
    )
    committed_artifacts = {path: path.read_bytes() for path in artifact_paths}
    expected = json.loads(worker.sidecar)
    expected["fingerprint"] = "draft.2"
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"
    worker.arm_runtime_removal()

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ) as raised:
        await run_build(
            tmp_path,
            config,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert raised.value.code == "JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD"
    assert {path: path.read_bytes() for path in artifact_paths} == committed_artifacts


@pytest.mark.asyncio
async def test_refreeze_allows_identical_worker_runtime_bytes_during_commit(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    expected = json.loads(worker.sidecar)
    expected["fingerprint"] = "draft.2"
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"
    worker.arm_runtime_rewrite(worker.runtime_source)

    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == frozenset({"ts:src/math"})


@pytest.mark.asyncio
async def test_toolchain_digest_drift_without_persisted_environment_requires_rebuild(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    committed = json.loads(sidecar_path.read_text(encoding="utf-8"))
    committed.pop("semanticEnvironmentDigest")
    sidecar_path.write_text(json.dumps(committed, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(worker.sidecar)
    expected["structuralDigest"] = "sha256:new-digest-scheme"
    expected["apiDigest"] = "sha256:new-api-digest-scheme"
    worker.module["structuralDigest"] = expected["structuralDigest"]
    worker.module["apiDigest"] = expected["apiDigest"]
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    report = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert status.stale == {"ts:src/math": "structural"}
    assert report.generated == frozenset({"ts:src/math"})
    assert report.metadata.get("recomposed", ()) == ()


@pytest.mark.asyncio
async def test_proofless_legacy_sidecar_rebuilds_when_contract_digests_match(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    committed = json.loads(sidecar_path.read_text(encoding="utf-8"))
    committed.pop("semanticEnvironmentDigest")
    sidecar_path.write_text(json.dumps(committed, sort_keys=True) + "\n", encoding="utf-8")

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    report = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert status.stale == {"ts:src/math": "structural"}
    assert report.generated == frozenset({"ts:src/math"})
    assert report.metadata.get("recomposed", ()) == ()


@pytest.mark.asyncio
async def test_symbol_qualified_target_rejects_unknown_symbol(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)

    with pytest.raises(JauntConfigError, match="Unknown TypeScript target"):
        await run_status(
            tmp_path,
            config,
            target_ids=("ts:src/math#typo",),
            worker_factory=lambda *_: worker,
        )


@pytest.mark.asyncio
async def test_fingerprint_drift_is_revalidated_and_recomposed_without_model(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    expected = json.loads(worker.sidecar)
    expected["fingerprint"] = "draft.2"
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert status.stale == {"ts:src/math": "toolchain"}
    assert report.generated == frozenset()
    assert report.refrozen == frozenset({"ts:src/math"})
    assert report.metadata["recomposed"] == ("ts:src/math",)
    validation = [params for method, params in worker.requests if method == "validateOverlay"]
    assert validation[-1]["recomposeModuleIds"] == ["ts:src/math"]
    assert "__jaunt_impl_double" in (tmp_path / "src/__generated__/math.ts").read_text()


@pytest.mark.asyncio
async def test_equivalent_prose_is_refrozen_after_semantic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    expected = json.loads(worker.sidecar)
    expected["proseDigest"] = "sha256:reworded"
    expected["apiDigest"] = "sha256:reworded-api"
    worker.module["proseDigest"] = "sha256:reworded"
    worker.module["apiDigest"] = "sha256:reworded-api"
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    async def equivalent(*_args: Any, **_kwargs: Any) -> bool:
        return True

    monkeypatch.setattr("jaunt.typescript.builder._gate_prose_change", equivalent)
    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.refrozen == frozenset({"ts:src/math"})


@pytest.mark.asyncio
async def test_direct_build_shares_quota_wait_between_semantic_gate_and_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config = replace(config, codex=replace(config.codex, quota_wait_minutes=1.5))
    worker = FakeWorker(tmp_path)
    initial = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert initial.exit_code == 0

    current_sidecar = json.loads(worker.sidecar)
    current_symbols = [
        {
            "name": "double",
            "kind": "function",
            "docs": "Return the input multiplied by two.",
        }
    ]
    current_sidecar.update(
        {
            "symbols": current_symbols,
            "proseDigest": "sha256:reworded",
            "apiDigest": "sha256:reworded-api",
        }
    )
    worker.module.update(
        {
            "symbols": current_symbols,
            "proseDigest": "sha256:reworded",
            "apiDigest": "sha256:reworded-api",
            "sidecar": json.dumps(current_sidecar, sort_keys=True) + "\n",
        }
    )

    semantic_calls = 0
    sleeps: list[float] = []

    async def judge(**_kwargs: Any) -> SimpleNamespace:
        nonlocal semantic_calls
        semantic_calls += 1
        if semantic_calls == 1:
            raise JauntQuotaGenerationError("semantic usage limit")
        return SimpleNamespace(
            final_message="MEANINGFUL",
            usage_input=2,
            usage_output=1,
            usage_cached=0,
        )

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    class QuotaGenerationBackend(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        @property
        def quota_wait_minutes(self) -> float:
            return 1.5

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            if self.calls == 1:
                raise JauntQuotaGenerationError("generation usage limit")
            return await super().generate_request(request, **kwargs)

    backend = QuotaGenerationBackend()
    monkeypatch.setattr("jaunt.typescript.builder.run_codex_exec", judge)
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)

    report = await run_build(
        tmp_path,
        config,
        generator=backend,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert semantic_calls == 2
    assert backend.calls == 2
    assert sleeps == [60.0, 30.0]


@pytest.mark.asyncio
async def test_context_docs_prose_change_requires_semantic_judgment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    sidecar = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar.parent.mkdir(parents=True)
    old_docs = [
        {
            "id": "workspace:src/math.context.ts",
            "exports": [{"symbol": "clock", "docs": "Whole seconds."}],
        }
    ]
    new_docs = [
        {
            "id": "workspace:src/math.context.ts",
            "exports": [{"symbol": "clock", "docs": "Strictly increasing whole seconds."}],
        }
    ]
    sidecar.write_text(
        json.dumps(
            {
                "symbols": [{"name": "double", "docs": "Double a number."}],
                "contextDocs": old_docs,
            }
        )
    )
    prompts: list[str] = []

    async def judge(**kwargs: Any) -> SimpleNamespace:
        prompts.append(str(kwargs["prompt"]))
        return SimpleNamespace(
            final_message="EQUIVALENT",
            usage_input=11,
            usage_output=2,
            usage_cached=3,
        )

    monkeypatch.setattr("jaunt.typescript.builder.run_codex_exec", judge)
    module = {
        "moduleId": "ts:src/math",
        "sidecarPath": "src/__generated__/math.jaunt.json",
        "symbols": [{"name": "double", "docs": "Double a number."}],
        "contextDocs": new_docs,
    }

    cost = CostTracker()
    assert await _gate_prose_change(tmp_path, module, config, cost=cost) is True
    assert len(prompts) == 1
    assert "OLD CONTEXT DOCS" in prompts[0]
    assert "Strictly increasing whole seconds" in prompts[0]
    assert cost.summary_dict()["api_calls"] == 1
    assert cost.summary_dict()["cached_prompt_tokens"] == 3


@pytest.mark.asyncio
async def test_unexplained_prose_digest_change_fails_closed(tmp_path: Path) -> None:
    config = _config(tmp_path)
    sidecar = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(json.dumps({"symbols": [{"name": "double", "docs": "Double a number."}]}))
    module = {
        "moduleId": "ts:src/math",
        "sidecarPath": "src/__generated__/math.jaunt.json",
        "symbols": [{"name": "double", "docs": "Double a number."}],
    }

    assert await _gate_prose_change(tmp_path, module, config) is False


@pytest.mark.asyncio
async def test_status_rejects_hand_edited_generated_artifact(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.write_text(implementation.read_text() + "// hand edit\n")

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)

    assert status.invalid["ts:src/math"][0].code == "JAUNT_TS_IMPLEMENTATION_DRIFT"


@pytest.mark.asyncio
async def test_status_preserves_mixed_api_mirror_line_endings(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    worker.api = (
        "/** Mixed line endings stay byte-significant. */\n"
        "export interface Nested {\r\n"
        "  readonly value: string;\r\n"
        "}\n"
    )
    worker.module["apiSource"] = worker.api
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)

    assert status.fresh == frozenset({"ts:src/math"})
    assert status.invalid == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative", "expected_code"),
    [
        ("src/__generated__/math.api.ts", "JAUNT_TS_API_DRIFT"),
        ("src/math.ts", "JAUNT_TS_FACADE_DRIFT"),
    ],
)
async def test_build_repairs_deterministic_artifact_drift_without_model(
    tmp_path: Path,
    relative: str,
    expected_code: str,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    original_implementation = implementation.read_bytes()
    artifact = tmp_path / relative
    expected_artifact = artifact.read_bytes()
    artifact.write_text("// hand edited deterministic artifact\n")

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert status.invalid["ts:src/math"][0].code == expected_code

    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == frozenset({"ts:src/math"})
    assert report.generated == frozenset()
    assert artifact.read_bytes() == expected_artifact
    assert implementation.read_bytes() == original_implementation


@pytest.mark.asyncio
async def test_build_restores_implementation_drift_from_validated_cache(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.write_text(implementation.read_text() + "// hand edit\n")

    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset({"ts:src/math"})
    assert report.refrozen == frozenset()
    assert report.metadata["cost"]["cache_hits"] == 1
    assert "// hand edit" not in implementation.read_text()


@pytest.mark.asyncio
async def test_magic_eject_leaves_plain_source_and_removes_jaunt_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    runner_calls: list[dict[str, Any]] = []

    async def green_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        runner_calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", green_runner)
    report = await run_eject(
        tmp_path,
        config,
        target="ts:src/math",
        worker_factory=lambda *_: worker,
    )

    ordinary = (tmp_path / "src/math.ts").read_text()
    assert report.ok
    assert "export const double" in ordinary
    assert "__jaunt_impl" not in ordinary
    assert "jaunt:generated" not in ordinary
    assert not (tmp_path / "src/math.jaunt.ts").exists()
    assert not (tmp_path / "src/__generated__/math.ts").exists()
    assert not (tmp_path / "src/__generated__/math.api.ts").exists()
    assert not (tmp_path / "src/__generated__/math.jaunt.json").exists()
    emit = next(call for call in runner_calls if call.get("normal_emit"))
    assert emit["declaration_emit"] is True
    assert emit["deleted_files"] == (
        "src/math.jaunt.ts",
        "src/__generated__/math.ts",
        "src/__generated__/math.api.ts",
    )
    assert emit["package_root"] == "."
    assert emit["project_config_paths"] == ("tsconfig.json",)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (TargetStatus(language="ts", stale={"ts:src/math": "structural"}), "structural"),
        (TargetStatus(language="ts", unbuilt=frozenset({"ts:src/math"})), "unbuilt"),
        (
            TargetStatus(
                language="ts",
                invalid={
                    "ts:src/math": (
                        TargetDiagnostic(
                            code="JAUNT_TS_API_DRIFT",
                            message="The API mirror changed.",
                        ),
                    )
                },
            ),
            "JAUNT_TS_API_DRIFT: The API mirror changed.",
        ),
    ],
)
def test_magic_eject_status_reason_preserves_specific_failure(
    status: TargetStatus,
    expected: str,
) -> None:
    assert _magic_eject_status_reason(status, "ts:src/math", ()) == expected


def test_magic_eject_status_reason_prioritizes_blocking_workspace_diagnostic() -> None:
    status = TargetStatus(language="ts", unbuilt=frozenset({"ts:src/math"}))
    diagnostic = TargetDiagnostic(
        code="JAUNT_TS_CONFIG_INVALID",
        message="The workspace is invalid.",
    )

    assert _magic_eject_status_reason(status, "ts:src/math", (diagnostic,)) == (
        "JAUNT_TS_CONFIG_INVALID: The workspace is invalid."
    )


def test_status_payload_and_human_output_include_npm_skill_plan() -> None:
    status = TargetStatus(
        language="ts",
        metadata={
            "npm_skills": {
                "enabled": True,
                "plan": {"file_count": 77, "total_bytes": 800_000},
            }
        },
    )

    payload = status_payload(status)
    assert payload["npm_skills"]["plan"] == {
        "file_count": 77,
        "total_bytes": 800_000,
    }
    assert "  npm skill plan: 77 files, 800000 bytes" in human_lines(payload)


@pytest.mark.asyncio
async def test_magic_eject_emit_failure_preserves_the_entire_managed_module(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    managed = (
        "src/math.jaunt.ts",
        "src/math.ts",
        "src/__generated__/math.ts",
        "src/__generated__/math.api.ts",
        "src/__generated__/math.jaunt.json",
    )
    before = {path: (tmp_path / path).read_bytes() for path in managed}

    async def runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("normal_emit"):
            return {
                "ok": False,
                "diagnostics": [
                    {
                        "code": "JAUNT_TS_EJECT_UNSAFE_OUTPUT",
                        "severity": "error",
                        "message": "emitted JavaScript retained a private spec import",
                    }
                ],
            }
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", runner)
    report = await run_eject(
        tmp_path,
        config,
        target="ts:src/math",
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert report.diagnostics[0].code == "JAUNT_TS_EJECT_EMIT"
    assert {path: (tmp_path / path).read_bytes() for path in managed} == before


@pytest.mark.asyncio
async def test_contract_check_typechecks_then_runs_committed_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    source = tmp_path / "src/util.ts"
    source_text = (
        "/** Add one.\n * @jauntContract\n */\n"
        "export function addOne(value: number): number { return value + 1; }\n"
    )
    source.write_text(source_text)
    worker.contracts = [{"path": "src/util.ts", "project": "tsconfig.json", "symbols": ["addOne"]}]
    battery = _battery_path(tmp_path, config, source, "addOne")
    battery.parent.mkdir(parents=True)
    battery.write_text(
        _with_strength_metadata(
            _with_header(
                'import { test, expect } from "vitest";\n'
                'import { addOne } from "../../../src/util.js";\n'
                'test("adds", () => expect(addOne(1)).toBe(2));\n',
                "src/util.ts",
                _digest(source_text),
                fixture_path=MISSING_INPUT,
                fixture_digest=MISSING_INPUT,
                fixture_topology=json.dumps(
                    dict(
                        sorted(
                            _fixture_resolution_preconditions(
                                tmp_path,
                                battery.relative_to(tmp_path).as_posix(),
                            ).items()
                        )
                    ),
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            ),
            {
                "protocol": "jaunt-ts-mutation/1",
                "sourcePath": "src/util.ts",
                "symbol": "addOne",
                "concurrency": 1,
                "complete": True,
                "score": {
                    "killed": 1,
                    "applicable": 1,
                    "survived": 0,
                    "excluded": 0,
                    "ratio": 1.0,
                },
                "killed": [
                    {
                        "id": "001:return:4:48",
                        "kind": "return",
                        "line": 4,
                        "column": 48,
                        "description": "replace a returned value",
                        "outcome": "killed",
                        "reason": "test-failed",
                    }
                ],
                "survived": [],
                "excluded": [],
            },
        )
    )
    calls: list[bool] = []

    async def green_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(bool(kwargs.get("typecheck_only")))
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", green_runner)
    report = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert len(report.checked) == 1
    assert calls == [True, False]


def test_design_range_and_declaration_safety() -> None:
    source = """/**
 * A bounded cache API.
 * @jauntDesign
 */
export class Cache {}
"""
    ranges = _design_ranges(source)
    assert len(ranges) == 1
    assert ranges[0].name == "Cache"
    assert (
        _validate_declaration(
            "/** Cache. */\nexport declare class Cache { get(k: string): string; }"
        )
        == []
    )
    assert "executable" in " ".join(
        _validate_declaration("export class Cache { get(k: string) { return k; } }")
    )
    materialized = _materialize_magic_stubs(
        "/** Cache. */\nexport declare class Cache { get(k: string): string; }"
    )
    assert "export class Cache" in materialized
    assert "return jaunt.magic()" in materialized
    assert (
        _design_output_errors(
            'import { type Input } from "./model.js";\n'
            "/** Planned. */\n"
            "export declare function planned(value: Input): string;\n",
            expected_name="planned",
        )
        == []
    )
    for extra in (
        "interface Extra {}\n",
        "type Extra = string;\n",
        "const extra = 1;\n",
        "export interface Extra {}\n",
    ):
        errors = _design_output_errors(
            "/** Planned. */\nexport declare function planned(value: string): string;\n" + extra,
            expected_name="planned",
        )
        assert "only associated type imports" in " ".join(errors)

    two_declarations = (
        "/** Preserve this contract. */\n"
        "export declare function stable(value: string): string;\n\n"
        "/** Design only this contract. @jauntDesign */\n"
        "export declare function planned(value: string): string;\n"
    )
    selected = _design_ranges(two_declarations)
    assert len(selected) == 1
    assert selected[0].start == two_declarations.index("/** Design only")
    assert "Preserve this contract" not in selected[0].source

    with pytest.raises(JauntConfigError, match="complete TSDoc"):
        _design_ranges('const marker = "@jauntDesign";\n')
    with pytest.raises(JauntConfigError, match="unterminated block comment"):
        _design_ranges("/** @jauntDesign\nexport declare function broken(): void;\n")


@pytest.mark.asyncio
async def test_build_blocks_pending_design_before_model_generation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    worker.module["specSource"] = (
        worker.spec_source
        + "\n/** Design another API. @jauntDesign */\n"
        + "export declare function planned(value: string): string;\n"
    )

    with pytest.raises(JauntConfigError, match="jaunt design"):
        await run_build(
            tmp_path,
            config,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )


@pytest.mark.asyncio
async def test_design_dry_run_then_apply_confines_and_materializes_stub(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    existing_declaration = (
        "/** Keep this existing declaration and its contract byte-for-byte. */\n"
        "export function existing(value: string): string { return jaunt.magic(); }\n\n"
    )
    design_source = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        + existing_declaration
        + "/**\n * Design a slug API.\n * @jauntDesign\n */\n"
        "export declare function planned(value: string): string;\n"
    )
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(design_source)
    worker.spec_source = design_source
    worker.input_hashes = {"src/math.jaunt.ts": _digest(design_source)}
    worker.module["specSource"] = design_source
    worker.module["symbols"] = [{"name": "planned", "kind": "function"}]
    preview = await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        generator=DesignGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert preview.ok
    assert preview.applied is False
    assert "@jauntDesign" in spec.read_text()
    assert "+export function planned(value: string): string" in preview.patch

    applied = await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        apply=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    updated = spec.read_text()
    assert applied.ok
    assert applied.applied is True
    assert "@jauntDesign" not in updated
    assert "export function planned(value: string): string" in updated
    assert "return jaunt.magic();" in updated
    assert existing_declaration in updated
    assert applied.usage is not None
    assert applied.usage["api_calls"] == 0
    assert not tuple((tmp_path / ".jaunt/design-proposals").glob("*.json"))


@pytest.mark.asyncio
async def test_design_apply_requires_a_reviewed_proposal(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    design_source = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        "/** Design a slug API. @jauntDesign */\n"
        "export declare function planned(value: string): string;\n"
    )
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(design_source)
    worker.spec_source = design_source
    worker.input_hashes = {"src/math.jaunt.ts": _digest(design_source)}
    worker.module["specSource"] = design_source
    worker.module["symbols"] = [{"name": "planned", "kind": "function"}]
    stale_skill = tmp_path / ".agents/skills/npm-stale/SKILL.md"
    stale_skill.parent.mkdir(parents=True)
    stale_skill.write_text(
        "---\nname: npm-stale\nx-jaunt-npm-package: stale\n"
        "x-jaunt-npm-version: 1.0.0\n---\nmanaged\n"
    )
    stale_skill_before = stale_skill.read_bytes()

    with pytest.raises(JauntConfigError, match="No reviewed design proposal"):
        await run_design(
            tmp_path,
            config,
            target_id="ts:src/math#planned",
            apply=True,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )
    assert spec.read_text() == design_source
    assert stale_skill.read_bytes() == stale_skill_before


@pytest.mark.asyncio
async def test_design_apply_refuses_source_changed_since_reviewed_proposal(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    design_source = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        "/** Design a slug API. @jauntDesign */\n"
        "export declare function planned(value: string): string;\n"
    )
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(design_source)
    worker.spec_source = design_source
    worker.input_hashes = {"src/math.jaunt.ts": _digest(design_source)}
    worker.module["specSource"] = design_source
    worker.module["symbols"] = [{"name": "planned", "kind": "function"}]

    await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        generator=DesignGenerator(),
        worker_factory=lambda *_: worker,
    )
    changed = design_source.replace("Design a slug API.", "Design a revised slug API.")
    spec.write_text(changed)
    worker.spec_source = changed
    worker.input_hashes = {"src/math.jaunt.ts": _digest(changed)}
    worker.module["specSource"] = changed

    with pytest.raises(JauntGenerationError, match="changed since the reviewed proposal"):
        await run_design(
            tmp_path,
            config,
            target_id="ts:src/math#planned",
            apply=True,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )
    assert spec.read_text() == changed


def test_design_marker_creation_waits_for_an_atomic_publisher_lease(tmp_path: Path) -> None:
    output = tmp_path / "out/value.ts"
    output.parent.mkdir()
    output.write_text("old\n", encoding="utf-8")
    publisher_scanned = threading.Event()
    release_publisher = threading.Event()
    design_started = threading.Event()

    def hold_after_manifest_scan() -> None:
        publisher_scanned.set()
        assert release_publisher.wait(timeout=5)

    def publish() -> None:
        atomic_write_manifest(
            tmp_path,
            (_Write("out/value.ts", "new\n", "implementation", "ts:value"),),
            pre_commit_guard=hold_after_manifest_scan,
        )

    def prepare_design() -> Path:
        design_started.set()
        return _prepare_design_manifest(
            tmp_path,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        publisher = executor.submit(publish)
        assert publisher_scanned.wait(timeout=5)
        pending_design = executor.submit(prepare_design)
        assert design_started.wait(timeout=5)
        try:
            assert not pending_design.done()
            assert not tuple((tmp_path / ".jaunt/transactions").glob("design-*.json"))
        finally:
            release_publisher.set()
        publisher.result(timeout=5)
        manifest = pending_design.result(timeout=5)

    assert output.read_text(encoding="utf-8") == "new\n"
    assert manifest.is_file()
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    _complete_design_manifest(
        tmp_path,
        manifest,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )


def test_design_marker_retirement_waits_for_the_global_transaction_lease(
    tmp_path: Path,
) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    directory = tmp_path / ".jaunt/transactions"
    holder_ready = threading.Event()
    release_holder = threading.Event()
    completion_started = threading.Event()

    def hold_lease() -> None:
        with ts_builder._PinnedWorkspace(tmp_path) as workspace:
            pinned_directory = workspace.directory(directory, create=False)
            lease = _acquire_transaction_lease(
                directory,
                blocking=True,
                pinned_directory=pinned_directory,
                authority_directory=workspace.root_directory,
            )
            assert lease is not None
            holder_ready.set()
            try:
                assert release_holder.wait(timeout=5)
            finally:
                lease.release()

    def complete_design() -> None:
        completion_started.set()
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        holder = executor.submit(hold_lease)
        assert holder_ready.wait(timeout=5)
        completion = executor.submit(complete_design)
        assert completion_started.wait(timeout=5)
        try:
            assert not completion.done()
            assert manifest.is_file()
        finally:
            release_holder.set()
        holder.result(timeout=5)
        completion.result(timeout=5)

    assert not manifest.exists()


def test_design_marker_lifecycle_blocks_every_foreign_transaction_marker(
    tmp_path: Path,
) -> None:
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    foreign = directory / "legacy.json"
    foreign.write_text("{}\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="legacy.json"):
        _prepare_design_manifest(
            tmp_path,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )
    assert not tuple(directory.glob("design-*.json"))

    foreign.unlink()
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    foreign.write_text("{}\n", encoding="utf-8")
    with pytest.raises(JauntGenerationError, match="legacy.json"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )
    assert manifest.is_file()

    foreign.unlink()
    _complete_design_manifest(
        tmp_path,
        manifest,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    assert not manifest.exists()


def test_design_completion_rejects_a_replaced_marker_identity(tmp_path: Path) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["writes"][0]["moduleId"] = "ts:src/replacement"
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="marker is invalid"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert manifest.is_file()
    assert source.read_text(encoding="utf-8") == "after\n"


def test_design_completion_authenticates_both_recorded_digests(tmp_path: Path) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["writes"][0]["before"] = _digest("forged original\n")
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="marker is invalid"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert manifest.is_file()


def test_design_abort_authenticates_both_recorded_digests(tmp_path: Path) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("before\n", encoding="utf-8")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["writes"][0]["after"] = _digest("forged proposal\n")
    manifest.write_text(json.dumps(payload) + "\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="marker is invalid"):
        _abort_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert manifest.is_file()


def test_design_completion_claim_never_clobbers_a_concurrent_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    foreign_bytes = b'{"operation":"foreign"}\n'
    original_retire = ts_design._retire_transaction_manifest
    retired_names: list[str] = []

    def replace_public_marker_before_claim_retirement(
        claim: Path,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> bool:
        retired_names.append(claim.name)
        manifest.write_bytes(foreign_bytes)
        return original_retire(claim, payload, **kwargs)

    monkeypatch.setattr(
        ts_design,
        "_retire_transaction_manifest",
        replace_public_marker_before_claim_retirement,
    )

    with pytest.raises(JauntGenerationError, match="concurrent transaction marker appeared"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert retired_names and retired_names[0].startswith("design-claim-")
    assert manifest.read_bytes() == foreign_bytes
    recovery_markers = tuple((tmp_path / ".jaunt/transactions").glob("design-recovery-*.json"))
    assert len(recovery_markers) == 1
    assert json.loads(recovery_markers[0].read_text(encoding="utf-8"))["operation"] == "design"


def test_design_completion_fails_closed_when_the_private_claim_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    original_retire = ts_design._retire_transaction_manifest

    def remove_claim_before_retirement(
        claim: Path,
        payload: Mapping[str, Any],
        **kwargs: Any,
    ) -> bool:
        claim.unlink()
        return original_retire(claim, payload, **kwargs)

    monkeypatch.setattr(
        ts_design,
        "_retire_transaction_manifest",
        remove_claim_before_retirement,
    )

    with pytest.raises(JauntGenerationError, match="could not be durably retired"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert json.loads(manifest.read_text(encoding="utf-8"))["operation"] == "design"
    assert tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


def test_windows_design_claim_restore_is_non_clobbering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory = tmp_path / ".jaunt/transactions"
    directory.mkdir(parents=True)
    claim = directory / "design-claim-owned.json"
    public = directory / "design-owned.json"
    claim.write_text('{"operation":"design"}\n', encoding="utf-8")
    public.write_text('{"operation":"foreign"}\n', encoding="utf-8")
    original_rename = os.rename

    def windows_non_replacing_rename(source: Path, destination: Path) -> None:
        if destination.exists():
            raise FileExistsError(destination)
        original_rename(source, destination)

    monkeypatch.setattr(ts_design.os, "name", "nt")
    monkeypatch.setattr(ts_design.os, "rename", windows_non_replacing_rename)
    monkeypatch.setattr(ts_builder, "_windows_flush_pinned_handle", lambda *_args: None)
    pinned = ts_builder._PinnedDirectory(path=directory, windows_handle=object())

    assert not ts_design._restore_design_manifest_claim(
        pinned,
        claim_name=claim.name,
        manifest_name=public.name,
    )
    assert json.loads(public.read_text(encoding="utf-8"))["operation"] == "foreign"
    assert claim.is_file()

    public.unlink()
    assert ts_design._restore_design_manifest_claim(
        pinned,
        claim_name=claim.name,
        manifest_name=public.name,
    )
    assert json.loads(public.read_text(encoding="utf-8"))["operation"] == "design"
    assert not claim.exists()


def test_design_completion_retains_marker_after_an_editor_replaces_validated_source(
    tmp_path: Path,
) -> None:
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before="before\n",
        after="after\n",
    )
    source = tmp_path / "src/math.jaunt.ts"
    source.parent.mkdir()
    source.write_text("after\n", encoding="utf-8")
    # This is the exact source the fresh TypeScript validation accepted. An
    # editor then wins the race before the transaction can be completed.
    assert _digest(source.read_text(encoding="utf-8")) == _digest("after\n")
    source.write_text("editor replacement\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="changed before transaction completion"):
        _complete_design_manifest(
            tmp_path,
            manifest,
            path="src/math.jaunt.ts",
            module_id="ts:src/math",
            before="before\n",
            after="after\n",
        )

    assert manifest.is_file()
    assert source.read_text(encoding="utf-8") == "editor replacement\n"


@pytest.mark.asyncio
async def test_design_apply_keeps_a_durable_marker_through_fresh_validation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class AuditedDesignWorker(FakeWorker):
        initialize_count = 0

        async def initialize(self, _params: InitializeParams) -> InitializeResult:
            self.initialize_count += 1
            if self.initialize_count == 3:
                manifests = tuple((tmp_path / ".jaunt/transactions").glob("design-*.json"))
                assert len(manifests) == 1
                assert "@jauntDesign" not in (tmp_path / "src/math.jaunt.ts").read_text()
            return await super().initialize(_params)

    worker = AuditedDesignWorker(tmp_path)
    design_source = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        "/** Design a slug API. @jauntDesign */\n"
        "export declare function planned(value: string): string;\n"
    )
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(design_source)
    worker.spec_source = design_source
    worker.input_hashes = {"src/math.jaunt.ts": _digest(design_source)}
    worker.module["specSource"] = design_source
    worker.module["symbols"] = [{"name": "planned", "kind": "function"}]

    await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        generator=DesignGenerator(),
        worker_factory=lambda *_: worker,
    )
    report = await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        apply=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert report.ok
    assert not tuple((tmp_path / ".jaunt/transactions").glob("design-*.json"))


@pytest.mark.asyncio
async def test_interrupted_design_transaction_is_blocking_not_silently_committed(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    spec = tmp_path / "src/math.jaunt.ts"
    before = spec.read_text()
    after = before + "\n// proposed design bytes\n"
    manifest = _prepare_design_manifest(
        tmp_path,
        path="src/math.jaunt.ts",
        module_id="ts:src/math",
        before=before,
        after=after,
    )
    atomic_write_manifest(
        tmp_path,
        (_Write("src/math.jaunt.ts", after, "design", "ts:src/math"),),
        expected_inputs={"src/math.jaunt.ts": _digest(before)},
        allowed_transaction_manifests=(manifest.name,),
    )

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert spec.read_text() == after
    assert manifest.is_file()
    assert any(
        diagnostic.code == "JAUNT_TS_INCOMPLETE_TRANSACTION" for diagnostic in status.diagnostics
    )


@pytest.mark.asyncio
async def test_failed_design_validation_rolls_back_source_and_marker(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class FailingValidationWorker(FakeWorker):
        initialize_count = 0

        async def initialize(self, _params: InitializeParams) -> InitializeResult:
            self.initialize_count += 1
            if self.initialize_count == 3:
                raise RuntimeError("fresh validation failed")
            return await super().initialize(_params)

    worker = FailingValidationWorker(tmp_path)
    design_source = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        "/** Design a slug API. @jauntDesign */\n"
        "export declare function planned(value: string): string;\n"
    )
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(design_source)
    worker.spec_source = design_source
    worker.input_hashes = {"src/math.jaunt.ts": _digest(design_source)}
    worker.module["specSource"] = design_source
    worker.module["symbols"] = [{"name": "planned", "kind": "function"}]

    await run_design(
        tmp_path,
        config,
        target_id="ts:src/math#planned",
        generator=DesignGenerator(),
        worker_factory=lambda *_: worker,
    )
    with pytest.raises(RuntimeError, match="fresh validation failed"):
        await run_design(
            tmp_path,
            config,
            target_id="ts:src/math#planned",
            apply=True,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )
    assert spec.read_text() == design_source
    assert not tuple((tmp_path / ".jaunt/transactions").glob("design-*.json"))


def test_contract_marker_surgery_round_trips() -> None:
    source = (
        "/** Add one. */\nexport function addOne(value: number): number { return value + 1; }\n"
    )
    projection = {
        "source": "export function addOne(value: number): number;\n",
        "sourceDigest": _digest(source),
        "symbol": "addOne",
        "kind": "function",
        **_projection_ranges(source, "addOne"),
    }
    adopted = _add_contract_tag(source, "addOne", projection)
    assert "@jauntContract" in adopted
    adopted_projection = {
        **projection,
        "sourceDigest": _digest(adopted),
        **_projection_ranges(adopted, "addOne"),
    }
    assert _remove_contract_tag(adopted, "addOne", adopted_projection) == source


def test_contract_marker_uses_worker_ranges_not_declaration_text_in_templates() -> None:
    source = (
        "const decoy = `\nexport function addOne(value: number): number { return 999; }\n`;\n"
        'const emoji = "🧭";\n'
        "/** Keep  authored  spacing. */\n"
        "export function addOne(value: number): number { return value + 1; }\n"
    )
    projection = {
        "source": (
            "/** Keep  authored  spacing. */\nexport function addOne(value: number): number;\n"
        ),
        "sourceDigest": _digest(source),
        "symbol": "addOne",
        "kind": "function",
        **_projection_ranges(source, "addOne"),
    }

    adopted = _add_contract_tag(source, "addOne", projection)

    assert adopted.count("@jauntContract") == 1
    assert adopted.index("@jauntContract") > adopted.index("Keep  authored  spacing")
    assert "const decoy = `\nexport function addOne" in adopted
    adopted_projection = {
        **projection,
        "sourceDigest": _digest(adopted),
        **_projection_ranges(adopted, "addOne"),
    }
    assert _remove_contract_tag(adopted, "addOne", adopted_projection) == source


def test_contract_marker_utf16_ranges_handle_astral_text_inside_tsdoc() -> None:
    source = (
        'const preface = "🧭🧭";\n'
        "/** Keep the compass 🧭 and the rocket 🚀 exactly here. */\n"
        "export function navigate(value: string): string { return value; }\n"
    )
    projection = {
        "source": (
            "/** Keep the compass 🧭 and the rocket 🚀 exactly here. */\n"
            "export function navigate(value: string): string;\n"
        ),
        "sourceDigest": _digest(source),
        "symbol": "navigate",
        "kind": "function",
        **_projection_ranges(source, "navigate"),
    }

    adopted = _add_contract_tag(source, "navigate", projection)

    assert adopted.startswith('const preface = "🧭🧭";\n/** Keep the compass 🧭')
    assert adopted.count("@jauntContract") == 1
    assert adopted.index("@jauntContract") < adopted.index("export function navigate")
    adopted_projection = {
        **projection,
        "sourceDigest": _digest(adopted),
        **_projection_ranges(adopted, "navigate"),
    }
    assert _remove_contract_tag(adopted, "navigate", adopted_projection) == source


def test_contract_marker_utf16_ranges_handle_astral_prefix_without_tsdoc() -> None:
    source = (
        'const route = "north 🧭";\n'
        "export function navigate(value: string): string { return value; }\n"
    )
    projection = {
        "source": "export function navigate(value: string): string;\n",
        "sourceDigest": _digest(source),
        "symbol": "navigate",
        "kind": "function",
        **_projection_ranges(source, "navigate"),
    }

    adopted = _add_contract_tag(source, "navigate", projection)

    assert adopted == (
        'const route = "north 🧭";\n'
        "/** @jauntContract */\n"
        "export function navigate(value: string): string { return value; }\n"
    )


def test_projection_offset_rejects_middle_of_utf16_surrogate_pair() -> None:
    source = "a🧭b"
    projection = {"before": 1, "inside": 2, "after": 3}

    assert _projection_offset(projection, "before", source) == 1
    assert _projection_offset(projection, "after", source) == 2
    with pytest.raises(JauntConfigError, match="invalid inside offset"):
        _projection_offset(projection, "inside", source)


def test_contract_test_context_does_not_expose_implementation_body() -> None:
    source = (
        "export interface Value { amount: number; }\n"
        "/** Return the value. */\n"
        "export function reveal(value: Value): number { return 8675309 + value.amount; }\n"
    )

    context = _declaration_only_contract(
        source,
        "reveal",
        {
            "source": (
                "export interface Value { amount: number; }\n\n"
                "/** Return the value. */\n"
                "export function reveal(value: Value): number;\n"
            ),
            "sourceDigest": _digest(source),
            "symbol": "reveal",
            "kind": "function",
        },
    )

    assert "8675309" not in context
    assert "export interface Value" in context
    assert "export function reveal(value: Value): number;" in context


def test_contract_property_request_pins_fast_check_seed_and_run_count(tmp_path: Path) -> None:
    config = _config(tmp_path)
    source = tmp_path / "src/property.ts"
    source_text = (
        "/** Round-trips values.\n * @jauntContract\n"
        " * @prop given value: string :: roundTrip(value) equals value\n */\n"
        "export function roundTrip(value: string): string { return value; }\n"
    )
    source.write_text(source_text)
    battery = _battery_path(tmp_path, config, source, "roundTrip")

    request = _battery_request(
        tmp_path,
        config,
        source,
        "roundTrip",
        battery,
        source_text,
        declaration_context=(
            "/** Round-trips values.\n * @jauntContract\n"
            " * @prop given value: string :: roundTrip(value) equals value\n */\n"
            "export function roundTrip(value: string): string;\n"
        ),
    )

    seed = request.cache_payload["propertySeed"]
    assert "_context/properties.json" in request.prompt
    assert request.cache_payload["propertyCount"] == 1
    property_block = request.cache_payload["propertyBlock"]
    assert isinstance(property_block, str)
    assert "const __jauntPropertyArbitrary_" in property_block
    assert "fc.Arbitrary<string> = fc.string();" in property_block
    assert f"seed: {seed}, numRuns: 50" in property_block
    assert ".roundTrip(value)" in property_block
    assert request.validator is not None
    valid = request.validator(
        'import { expect, test } from "vitest";\ntest("example", () => expect(true).toBe(true));\n'
    )
    assert valid == []
    invalid = request.validator('import fc from "fast-check";\n')
    assert isinstance(invalid, list)
    invalid_messages = "\n".join(str(error) for error in invalid)
    assert "deterministic @prop rendering" in invalid_messages


@pytest.mark.asyncio
async def test_adopt_executes_proposed_battery_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    source = tmp_path / "src/util.ts"
    source.write_text(
        "/** Add one. */\nexport function addOne(value: number): number { return value + 1; }\n"
    )
    calls: list[bool] = []

    async def green_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(bool(kwargs.get("typecheck_only")))
        return {"ok": True}

    async def no_mutable_site(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {
            "protocol": "jaunt-ts-mutation/1",
            "sourcePath": "src/util.ts",
            "symbol": "addOne",
            "concurrency": 1,
            "complete": True,
            "killed": [],
            "survived": [],
            "excluded": [
                {
                    "id": "000:unsupported:0:0",
                    "kind": "unsupported",
                    "line": 0,
                    "column": 0,
                    "description": "no mutable site",
                    "outcome": "excluded",
                    "reason": "no-mutable-site",
                }
            ],
            "score": {
                "killed": 0,
                "applicable": 0,
                "survived": 0,
                "excluded": 1,
                "ratio": None,
            },
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", green_runner)
    monkeypatch.setattr("jaunt.typescript.contracts._run_mutation_strength", no_mutable_site)
    report = await run_adopt(
        tmp_path,
        config,
        target="src/util.ts#addOne",
        apply=False,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.ok
    assert calls == [True, False]


@pytest.mark.asyncio
async def test_adopt_rejects_vitest_config_change_after_proposed_battery_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.typescript_target is not None
    config = replace(
        config,
        contract=replace(config.contract, strength=False),
        typescript_target=replace(
            config.typescript_target,
            vitest_config="vitest.config.ts",
        ),
    )
    (tmp_path / "vitest.config.ts").write_text(
        'export default { test: { setupFiles: ["tests/setup.ts"] } };\n',
        encoding="utf-8",
    )
    setup = tmp_path / "tests/setup.ts"
    setup.write_text('export const setupVersion = "v1";\n', encoding="utf-8")
    worker = FakeWorker(tmp_path)
    source = tmp_path / "src/util.ts"
    source.write_text(
        "/** Add one. */\nexport function addOne(value: number): number { return value + 1; }\n",
        encoding="utf-8",
    )
    source_before = source.read_bytes()
    mutated = False

    async def mutate_config_after_run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal mutated
        setup.write_text('export const setupVersion = "v2";\n', encoding="utf-8")
        mutated = True
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    monkeypatch.setattr(
        "jaunt.typescript.contracts._run_test_batches",
        mutate_config_after_run,
    )
    with pytest.raises(
        JauntGenerationError,
        match=r"(?:inputs changed.*setup\.ts|Vitest configuration changed)",
    ):
        await run_adopt(
            tmp_path,
            config,
            target="src/util.ts#addOne",
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert mutated
    assert source.read_bytes() == source_before
    assert not (tmp_path / "tests/contract/src/util.addOne.contract.test.ts").exists()


def test_magic_eject_inlines_standalone_types_and_publicizes_reserved_bindings() -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [
            {"name": "create", "kind": "function", "docs": "Create a token."},
            {
                "name": "Store",
                "kind": "class",
                "docs": "Store tokens.",
                "members": [
                    {
                        "name": "get",
                        "kind": "method",
                        "static": False,
                        "docs": "Read a stored token.",
                    },
                    {
                        "name": "size",
                        "kind": "getter",
                        "static": False,
                        "docs": "Count stored tokens.",
                    },
                ],
            },
        ],
        "typeImports": [],
        "typeDeclarations": [
            {
                "name": "Claims",
                "kind": "interface",
                "source": "export interface Claims { sub: string; }",
                "docs": "Claims.",
            },
            {
                "name": "Code",
                "kind": "type",
                "source": 'export type Code = "bad";',
                "docs": "",
            },
        ],
        "apiSource": """/** Claims. */
export interface Claims { sub: string; }
export type Code = "bad";
export declare function create(value: Claims): string;
export declare class Store { get(): string; }
""",
    }
    implementation = """// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
import type { Claims } from "./token.api.js";
const __jaunt_impl_create = (value: Claims): string => value.sub;
class __jaunt_impl_Store {
  get(): string { return "x"; }
  get size(): number { return 1; }
}
Object.defineProperty(__jaunt_impl_create, "name", { value: "create", configurable: true });
/**
 * Create a token.
 */
export const create: typeof __JauntApi.create = __jaunt_impl_create;
/**
 * Store tokens.
 */
export const Store: typeof __JauntApi.Store = __jaunt_impl_Store;
export type Store = __JauntApi.Store;
"""

    ordinary = _ordinary_ejected_source(module, implementation)

    assert "export interface Claims" in ordinary
    assert 'export type Code = "bad"' in ordinary
    assert "export const create" in ordinary
    assert "export class Store" in ordinary
    assert "export declare function" not in ordinary
    assert "__jaunt_impl" not in ordinary
    assert "__JauntApi" not in ordinary
    assert "Object.defineProperty" not in ordinary
    assert ordinary.count("Create a token.") == 1
    assert ordinary.count("Store tokens.") == 1
    assert ordinary.count("Read a stored token.") == 1
    assert ordinary.count("Count stored tokens.") == 1
    assert "Create a token.\n */\nexport const create" in ordinary
    assert "Store tokens.\n */\nexport class Store" in ordinary
    assert "Read a stored token.\n   */\n  get(): string" in ordinary
    assert "Count stored tokens.\n   */\n  get size(): number" in ordinary


def test_magic_eject_renames_only_code_identifiers_and_preserves_literal_data() -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function", "docs": "Create text."}],
        "typeDeclarations": [],
        "typeImports": [],
        "apiSource": "export declare function create(): string;\n",
    }
    boundary = "export const create: typeof __JauntApi.create = __jaunt_impl_create;"
    implementation = f'''// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
const __jaunt_impl_create = (): string => {{
  const literal = "__jaunt_impl_create __JauntApi jaunt:generated";
  const boundaryText = "{boundary}";
  // __jaunt_impl_create and __JauntApi are documentation, not bindings.
  // jaunt:generated is also harmless away from the managed header.
  const regex = /__jaunt_impl_create|__JauntApi|jaunt:generated/g;
  const template = `raw __jaunt_impl_create __JauntApi ${{__jaunt_impl_create.name}}`;
  return `${{literal}}:${{boundaryText}}:${{regex.source}}:${{template}}`;
}};
{boundary}
'''

    ordinary = _ordinary_ejected_source(module, implementation)

    assert '"__jaunt_impl_create __JauntApi jaunt:generated"' in ordinary
    assert f'"{boundary}"' in ordinary
    assert "// __jaunt_impl_create and __JauntApi are documentation" in ordinary
    assert "// jaunt:generated is also harmless" in ordinary
    assert "/__jaunt_impl_create|__JauntApi|jaunt:generated/g" in ordinary
    assert "`raw __jaunt_impl_create __JauntApi ${create.name}`" in ordinary
    assert "export const create = (): string" in ordinary
    assert ordinary.count(boundary) == 1  # the literal survives; the code boundary is removed


@pytest.mark.parametrize(
    "expression",
    [
        '({ __jaunt_impl_create: "data" })',
        '({ data: "value" }).__jaunt_impl_create',
        "({ __jaunt_impl_create })",
    ],
)
def test_magic_eject_fails_closed_for_reserved_names_used_as_properties(
    expression: str,
) -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function"}],
        "typeDeclarations": [],
        "typeImports": [],
        "apiSource": "export declare function create(): string;\n",
    }
    implementation = f"""// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
const __jaunt_impl_create = (): string => String({expression});
export const create: typeof __JauntApi.create = __jaunt_impl_create;
"""

    with pytest.raises(JauntConfigError, match="non-binding property position"):
        _ordinary_ejected_source(module, implementation)


def test_magic_eject_fails_closed_before_identifier_rename_can_capture() -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function"}],
        "typeDeclarations": [],
        "typeImports": [],
        "apiSource": "export declare function create(): string;\n",
    }
    implementation = """// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
const __jaunt_impl_create = (): string => {
  const create = "capturing local";
  return __jaunt_impl_create.name || create;
};
export const create: typeof __JauntApi.create = __jaunt_impl_create;
"""

    with pytest.raises(JauntConfigError, match="could capture a renamed binding"):
        _ordinary_ejected_source(module, implementation)


def test_magic_eject_preserves_and_retargets_public_type_imports() -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function"}],
        "typeDeclarations": [
            {
                "name": "Payload",
                "kind": "interface",
                "source": "export interface Payload { value: External; }",
                "docs": "A wrapped external value.",
            }
        ],
        "typeImports": [
            {
                "specifier": "../model.js",
                "typeOnly": True,
                "runtime": False,
                "namedImports": [
                    {
                        "imported": "External",
                        "local": "External",
                        "typeOnly": True,
                    }
                ],
            }
        ],
        "apiSource": """import type { External } from "../model.js";
/**
 * A wrapped external value.
 */
export interface Payload { value: External; }
export declare function create(value: Payload): string;
""",
    }
    implementation = """// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
import type { Payload } from "./token.api.js";
const __jaunt_impl_create = (value: Payload): string => String(value.value);
export const create: typeof __JauntApi.create = __jaunt_impl_create;
"""

    ordinary = _ordinary_ejected_source(module, implementation)

    assert 'import type { External } from "./model.js";' in ordinary
    assert "export interface Payload { value: External; }" in ordinary
    assert 'from "./token.api.js"' not in ordinary


def test_magic_eject_uses_ast_bounded_structured_type_declarations() -> None:
    structured_interface = """export interface NestedShape<
  T extends { meta: { id: string; }; }
> {
  /** A comment containing a misleading closing brace: } */
  render(value: { text: `prefix;${string}`; }): {
    done: boolean;
    callback: () => { ok: true; };
  };
}"""
    structured_alias = """export type StructuredAlias<T> = {
  nested: {
    run(input: T): { value: `result;${string}`; };
  };
  callback: (input: { note: \"};\"; }) => {
    output: `prefix;${string}`;
  };
  /* A block comment containing misleading tokens: ; } */
  marker: \"/* ; */\";
};"""
    api_source = f"""// generated mirror
/**
 * Nested shape docs.
 */
{structured_interface}

{structured_alias}

export declare function create(value: StructuredAlias<string>): string;
"""
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function"}],
        "typeImports": [],
        "typeDeclarations": [
            {
                "name": "NestedShape",
                "kind": "interface",
                "source": structured_interface,
                "docs": "Nested shape docs.",
            },
            {
                "name": "StructuredAlias",
                "kind": "type",
                "source": structured_alias,
                "docs": "",
            },
        ],
        "apiSource": api_source,
    }
    implementation = """// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
import type { StructuredAlias } from "./token.api.js";
const __jaunt_impl_create = (value: StructuredAlias<string>): string => String(value);
export const create: typeof __JauntApi.create = __jaunt_impl_create;
"""

    ordinary = _ordinary_ejected_source(module, implementation)

    assert structured_interface in ordinary
    assert structured_alias in ordinary
    assert "callback: () => { ok: true; };" in ordinary
    assert "output: `prefix;${string}`;" in ordinary
    assert "misleading tokens: ; }" in ordinary
    assert ordinary.count("export type StructuredAlias") == 1


def test_magic_eject_rejects_type_declaration_not_in_api_mirror() -> None:
    module = {
        "apiMirrorPath": "src/__generated__/token.api.ts",
        "implementationPath": "src/__generated__/token.ts",
        "facadePath": "src/token.ts",
        "symbols": [{"name": "create", "kind": "function"}],
        "typeImports": [],
        "typeDeclarations": [
            {
                "name": "Injected",
                "kind": "type",
                "source": "export type Injected = string; console.log('unexpected');",
                "docs": "",
            }
        ],
        "apiSource": "export declare function create(): string;\n",
    }
    implementation = """// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
import type * as __JauntApi from "./token.api.js";
const __jaunt_impl_create = (): string => "ok";
export const create: typeof __JauntApi.create = __jaunt_impl_create;
"""

    with pytest.raises(JauntConfigError, match="inconsistent type declaration 'Injected'"):
        _ordinary_ejected_source(module, implementation)


def test_derived_runner_results_are_allowlist_redacted() -> None:
    result = _redact_runner_result(
        {
            "ok": False,
            "tests": [
                {
                    "file": "FILENAME-SENTINEL.derived.test.ts",
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "0123456789abcdef",
                    "category": "assertion",
                    "durationMs": "DURATION-SENTINEL",
                    "message": "SECRET",
                    "stack": "SECRET STACK",
                },
                {
                    "file": "passing.derived.test.ts",
                    "tier": "derived",
                    "status": "passed",
                    "durationMs": 2,
                },
            ],
            "captured": {"stdout": "SECRET", "stderr": "SECRET"},
            "diagnostics": [
                {
                    "code": "TS2345",
                    "severity": "error",
                    "path": "secret.derived.test.ts",
                    "message": "SECRET DIAGNOSTIC",
                }
            ],
            "stderr": "SECRET STDERR",
        },
        enabled=True,
    )
    rendered = json.dumps(result)
    assert "SECRET" not in rendered
    assert "FILENAME-SENTINEL" not in rendered
    assert "DURATION-SENTINEL" not in rendered
    assert result["tests"] == [{"caseId": "0123456789abcdef", "category": "assertion"}]


def test_typecheck_redaction_preserves_only_bounded_diagnostic_messages() -> None:
    diagnostic = {
        "code": "TS2532",
        "severity": "error",
        "path": "tests/__generated__/brief.derived.test.ts",
        "line": 12,
        "column": 9,
        "message": "Object is possibly 'undefined'.",
    }
    protected = _redact_runner_result(
        {
            "ok": False,
            "mode": "typecheck",
            "diagnostics": [diagnostic],
            "tests": [],
            "captured": {"stdout": "", "stderr": ""},
        },
        enabled=True,
    )

    assert protected["diagnostics"] == [diagnostic]

    long_message = "X" * 2_500
    bounded = _redact_runner_result(
        {
            "ok": False,
            "mode": "typecheck",
            "diagnostics": [{**diagnostic, "message": long_message}],
            "tests": [],
            "captured": {"stdout": "", "stderr": ""},
        },
        enabled=True,
    )
    message = bounded["diagnostics"][0]["message"]
    assert len(message) == 2_000
    assert message.endswith("[jaunt: diagnostic truncated]")

    run_mode = _redact_runner_result(
        {
            "ok": False,
            "mode": "run",
            "diagnostics": [diagnostic],
            "tests": [],
            "captured": {"stdout": "", "stderr": ""},
        },
        enabled=True,
    )
    assert "message" not in run_mode["diagnostics"][0]


@pytest.mark.parametrize(
    ("forbidden_key", "forbidden_value"),
    [
        ("file", "FILENAME-SENTINEL.derived.test.ts"),
        ("tier", "derived"),
        ("status", "failed"),
        ("durationMs", 1),
        ("message", "MESSAGE-SENTINEL"),
    ],
)
def test_protected_runner_dto_rejects_every_non_allowlisted_derived_field(
    forbidden_key: str,
    forbidden_value: object,
) -> None:
    test = {
        "caseId": "0123456789abcdef",
        "category": "assertion",
        forbidden_key: forbidden_value,
    }
    result = {
        "ok": False,
        "mode": "run",
        "diagnostics": [],
        "tests": [test],
        "captured": {"stdout": "", "stderr": ""},
    }

    assert not _valid_runner_dto(result, expected_mode="run", redact_derived=True)


def test_protected_runner_dto_keeps_aggregate_success_with_no_derived_records() -> None:
    successful = {
        "ok": True,
        "mode": "run",
        "diagnostics": [],
        "tests": [],
        "captured": {"stdout": "", "stderr": ""},
    }
    failed = {
        **successful,
        "ok": False,
        "tests": [{"caseId": "0123456789abcdef", "category": "assertion"}],
    }

    assert _valid_runner_dto(successful, expected_mode="run", redact_derived=True)
    assert _valid_runner_dto(failed, expected_mode="run", redact_derived=True)
    assert not _valid_runner_dto(successful, expected_mode="run", redact_derived=False)


@pytest.mark.parametrize("redact_derived", [True, False])
def test_runner_startup_failure_preserves_only_bounded_actionable_detail(
    redact_derived: bool,
) -> None:
    message = "failed to load vitest config\nError: missing plugin"
    result = {
        "ok": False,
        "mode": "run",
        "diagnostics": [],
        "tests": [
            {
                "caseId": "opaque-runner-failure",
                "category": "runner",
                "message": message,
            }
        ],
        "captured": {"stdout": "", "stderr": ""},
    }

    assert _valid_runner_dto(
        result,
        expected_mode="run",
        redact_derived=redact_derived,
    )
    assert _redact_runner_result(result, enabled=True)["tests"] == result["tests"]
    assert _runner_validation_errors(result) == ["Vitest runner startup failed: " + message]


@pytest.mark.parametrize(
    "record",
    [
        {
            "caseId": "0123456789abcdef",
            "category": "runner",
            "message": "not the reserved startup record",
        },
        {
            "caseId": "opaque-runner-failure",
            "category": "assertion",
            "message": "not a runner failure",
        },
        {
            "caseId": "opaque-runner-failure",
            "category": "runner",
            "message": "x" * 2_001,
        },
    ],
)
def test_runner_dto_rejects_other_message_bearing_protected_records(
    record: dict[str, str],
) -> None:
    result = {
        "ok": False,
        "mode": "run",
        "diagnostics": [],
        "tests": [record],
        "captured": {"stdout": "", "stderr": ""},
    }

    assert not _valid_runner_dto(result, expected_mode="run", redact_derived=True)
    assert not _valid_runner_dto(result, expected_mode="run", redact_derived=False)


@pytest.mark.parametrize(
    "source",
    [
        "\nimport { test } from 'vitest';\n",
        "   import { test } from 'vitest';\n",
        "\r\nimport { test } from 'vitest';\r\n",
    ],
)
def test_generated_test_header_body_digest_uses_one_canonical_form(source: str) -> None:
    rendered = _with_test_header(source, tier="derived", source_path="tests/math.jaunt-test.ts")
    metadata = _test_header_metadata(rendered)

    assert metadata is not None
    assert metadata["body_digest"] == _digest(_strip_test_header(rendered))


def test_runner_fingerprint_is_portable_between_override_and_installed_packages(
    tmp_path: Path,
) -> None:
    override_workspace = tmp_path / "override-workspace"
    installed_workspace = tmp_path / "installed-workspace"
    override_package = tmp_path / "source" / "jaunt-ts"
    installed_package = installed_workspace / "node_modules/@usejaunt/ts"
    for package in (override_package, installed_package):
        (package / "dist/test").mkdir(parents=True)
        (package / "dist/analyzer").mkdir(parents=True)
        (package / "package.json").write_text('{"name":"@usejaunt/ts","version":"0.1.0-alpha.0"}\n')
        (package / "dist/test/runner.js").write_text("export const runner = 1;\n")
        (package / "dist/test/reporter.js").write_text("export const reporter = 1;\n")
        (package / "dist/analyzer/provenance.js").write_text("export const provenance = 1;\n")
    override_workspace.mkdir()
    initialized = SimpleNamespace(
        worker_version="0.1.0-alpha.0",
        typescript_version="6.0.2",
    )
    override_client = SimpleNamespace(
        installation=SimpleNamespace(
            package_root=override_package,
            tool_owner=override_workspace,
        )
    )
    installed_client = SimpleNamespace(
        installation=SimpleNamespace(
            package_root=installed_package,
            tool_owner=installed_workspace,
        )
    )

    override_fingerprint = _runner_fingerprint(override_workspace, override_client, initialized)
    installed_fingerprint = _runner_fingerprint(installed_workspace, installed_client, initialized)
    assert override_fingerprint == installed_fingerprint

    # A same-version rebuild of a transitive analyzer helper is still a new
    # protected runner runtime, even when it was absent from an old hand list.
    (override_package / "dist/analyzer/provenance.js").write_text("export const provenance = 2;\n")
    assert _runner_fingerprint(override_workspace, override_client, initialized) != (
        installed_fingerprint
    )


def test_runner_fingerprint_is_portable_across_supported_node_runtimes(tmp_path: Path) -> None:
    package = tmp_path / "node_modules/@usejaunt/ts"
    (package / "dist/test").mkdir(parents=True)
    (package / "package.json").write_text(
        '{"name":"@usejaunt/ts","version":"0.1.0-alpha.0"}\n', encoding="utf-8"
    )
    (package / "dist/test/runner.js").write_text("export const runner = 1;\n")
    (package / "dist/test/reporter.js").write_text("export const reporter = 1;\n")

    def fake_node(name: str, version: str) -> Path:
        executable = tmp_path / name
        executable.write_text(f"#!{sys.executable}\nprint({version!r})\n", encoding="utf-8")
        executable.chmod(executable.stat().st_mode | 0o111)
        return executable

    node_20 = fake_node("node-20", "v20.19.1")
    node_22 = fake_node("node-22", "v22.15.0")
    initialized = SimpleNamespace(
        worker_version="0.1.0-alpha.0",
        typescript_version="6.0.2",
    )

    def client(node: Path) -> SimpleNamespace:
        return SimpleNamespace(
            installation=SimpleNamespace(
                node=str(node),
                package_root=package,
                tool_owner=tmp_path,
            )
        )

    assert _runner_fingerprint(tmp_path, client(node_20), initialized) == _runner_fingerprint(
        tmp_path, client(node_22), initialized
    )


def test_implementation_repair_feedback_limits_derived_record_to_opaque_contract() -> None:
    feedback = _implementation_repair_feedback(
        {
            "ok": False,
            "tests": [
                {
                    "file": "FILENAME-SENTINEL.derived.test.ts",
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "0123456789abcdef",
                    "category": "assertion",
                    "durationMs": "DURATION-SENTINEL",
                    "message": "MESSAGE-SENTINEL",
                },
                {
                    "file": "passing.derived.test.ts",
                    "tier": "derived",
                    "status": "passed",
                    "durationMs": 2,
                },
            ],
        }
    )

    assert '"caseId": "0123456789abcdef"' in feedback
    assert '"category": "assertion"' in feedback
    for secret in (
        "FILENAME-SENTINEL",
        "DURATION-SENTINEL",
        "MESSAGE-SENTINEL",
        '"tier"',
        '"status"',
        '"durationMs"',
        '"file"',
    ):
        assert secret not in feedback


def test_post_redaction_assertion_covers_adversarial_runner_surfaces() -> None:
    sentinels = {
        "message": "MESSAGE-SENTINEL",
        "stack": "STACK-SENTINEL",
        "diff": "DIFF-SENTINEL",
        "snapshot": "SNAPSHOT-SENTINEL",
        "stdout": "STDOUT-SENTINEL",
        "stderr": "STDERR-SENTINEL",
        "warning": "WARNING-SENTINEL",
        "setup": "SETUP-SENTINEL",
        "teardown": "TEARDOWN-SENTINEL",
        "config": "CONFIG-SENTINEL",
        "cause": "CAUSE-SENTINEL",
        "aggregate": "AGGREGATE-SENTINEL",
        "serialized": "SERIALIZED-SENTINEL",
        "filename": "FILENAME-SENTINEL",
        "duration": "DURATION-SENTINEL",
    }
    raw = {
        "ok": False,
        "mode": "run",
        "tests": [
            {
                "file": sentinels["filename"],
                "tier": "derived",
                "status": "failed",
                "caseId": "0123456789abcdef",
                "category": "assertion",
                "durationMs": sentinels["duration"],
                "message": sentinels["message"],
                "stack": sentinels["stack"],
                "diff": sentinels["diff"],
                "snapshot": sentinels["snapshot"],
                "cause": {"message": sentinels["cause"]},
                "errors": [{"message": sentinels["aggregate"]}],
                "serializedError": {"detail": sentinels["serialized"]},
            }
        ],
        "diagnostics": [
            {
                "code": "JAUNT_TS_RUNNER",
                "severity": "error",
                "message": sentinels["config"],
            }
        ],
        "captured": {
            "stdout": sentinels["stdout"],
            "stderr": sentinels["stderr"],
        },
        "warnings": [sentinels["warning"]],
        "setupFailure": sentinels["setup"],
        "teardownFailure": sentinels["teardown"],
    }

    protected = _redact_runner_result(raw, enabled=True)

    rendered = json.dumps(protected)
    for sentinel in sentinels.values():
        assert sentinel not in rendered
    with pytest.raises(_HeldOutLeakError) as caught:
        _assert_no_held_out_leak(raw, {**protected, "leak": sentinels["aggregate"]})
    assert sentinels["aggregate"] not in str(caught.value)


def test_failed_post_redaction_assertion_returns_minimal_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_closed(*_args: Any, **_kwargs: Any) -> None:
        raise _HeldOutLeakError("generic")

    monkeypatch.setattr("jaunt.typescript.tester._assert_no_held_out_leak", fail_closed)
    protected = _redact_runner_result(
        {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": "secret.derived.test.ts",
                    "tier": "derived",
                    "status": "failed",
                    "message": "FALLBACK-SENTINEL",
                }
            ],
        },
        enabled=True,
    )

    assert protected == {
        "ok": False,
        "mode": "run",
        "failures": [{"category": "runner"}],
        "captured": {"stdout": "", "stderr": ""},
    }
    assert "FALLBACK-SENTINEL" not in json.dumps(protected)


@pytest.mark.parametrize(
    "raw",
    [
        {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "DERIVED-SECRET-MESSAGE",
                    "category": "assertion",
                }
            ],
        },
        {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "0123456789abcdef",
                    "category": "DERIVED-SECRET-CATEGORY",
                }
            ],
        },
        {"ok": "false", "mode": "run"},
    ],
)
def test_untrusted_runner_allowlist_values_fail_closed(raw: dict[str, Any]) -> None:
    protected = _redact_runner_result(raw, enabled=True)
    feedback = _implementation_repair_feedback(raw)

    assert protected["ok"] is False
    assert protected["failures"] == [{"category": "runner"}]
    assert "DERIVED-SECRET" not in json.dumps(protected)
    assert "DERIVED-SECRET" not in feedback


def test_example_tier_requires_complete_generated_provenance() -> None:
    valid = _with_test_header(
        "test('authored', () => {});\n",
        tier="example",
        source_path="tests/math.jaunt-test.ts",
    )
    forged = (
        "// jaunt:tier=example\ntest('held out', () => {});\n",
        "test('held out', () => {});\n// jaunt:tier=example\n",
        valid + "// jaunt:tier=example\n",
        valid.replace(
            "// jaunt:source=tests/math.jaunt-test.ts",
            "// jaunt:tier=example",
        ),
        valid.replace("\n\n", "\n// arbitrary comment before the separator\n\n"),
    )

    assert all(
        not _is_reviewable_example_battery("tests/__generated__/math.example.test.ts", source)
        for source in forged
    )
    assert _is_reviewable_example_battery("tests/__generated__/math.example.test.ts", valid)


@pytest.mark.asyncio
async def test_protected_typecheck_runner_returns_bounded_exact_diagnostics(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    package_root = tmp_path / "tooling"
    runner = package_root / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    runner.write_text(
        "import json, sys\n"
        "payload = json.loads(sys.stdin.read())\n"
        "message = (\"Object is possibly 'undefined'.\" "
        "if payload['redactDerived'] is False else 'Protected TypeScript diagnostic')\n"
        "result = {\n"
        "  'ok': False,\n"
        "  'mode': 'typecheck',\n"
        "  'diagnostics': [{\n"
        "    'code': 'TS2532', 'severity': 'error', 'message': message,\n"
        "    'path': 'tests/generated.derived.test.ts', 'line': 4, 'column': 7,\n"
        "  }],\n"
        "  'tests': [],\n"
        "  'captured': {'stdout': '', 'stderr': ''},\n"
        "}\n"
        "sys.stdout.write(json.dumps(result))\n",
        encoding="utf-8",
    )
    compiler = tmp_path / "typescript.js"
    compiler.write_text("", encoding="utf-8")
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node=sys.executable,
            package_root=package_root,
            compiler_module_path=compiler,
        )
    )

    protected = await _run_test_runner(
        client,
        tmp_path,
        config,
        files=("tests/generated.derived.test.ts",),
        redact_derived=True,
        typecheck_only=True,
        timeout=2,
    )

    assert protected["diagnostics"] == [
        {
            "code": "TS2532",
            "severity": "error",
            "message": "Object is possibly 'undefined'.",
            "path": "tests/generated.derived.test.ts",
            "line": 4,
            "column": 7,
        }
    ]


@pytest.mark.asyncio
async def test_typecheck_batches_keep_config_snapshot_out_of_overlay_roots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    battery = "tests/__generated__/math.example.test.ts"
    candidate = "src/__generated__/math.ts"
    config_path = "vitest.config.ts"
    workspace = {
        "projects": [
            {
                "id": "tsconfig.test.json",
                "configPath": "tsconfig.test.json",
                "role": "test",
                "rootFiles": [battery],
            }
        ]
    }
    captured: list[dict[str, Any]] = []

    async def runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        captured.append(kwargs)
        return {
            "ok": True,
            "mode": "typecheck",
            "diagnostics": [],
            "tests": [],
            "captured": {"stdout": "", "stderr": ""},
        }

    monkeypatch.setattr(ts_tester, "_run_test_runner", runner)
    monkeypatch.setattr(
        ts_tester,
        "_validate_test_owner_dependencies",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ts_tester,
        "_pin_test_dependency_runtimes",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        ts_tester,
        "_pin_vitest_config_dependency_runtimes",
        lambda *_args, **_kwargs: None,
    )

    result = await ts_tester._run_test_batches(
        object(),
        tmp_path,
        config,
        workspace,
        files=(battery,),
        overlays={
            battery: "export {};\n",
            candidate: "export const value = 1;\n",
            config_path: "const invalidConfig: string = 1;\n",
        },
        typecheck_only=True,
        config_snapshot=(
            {config_path: "sha256:config"},
            {config_path: "const invalidConfig: string = 1;\n"},
        ),
    )

    assert result["ok"] is True
    assert len(captured) == 1
    assert set(captured[0]["overlays"]) == {battery, candidate, config_path}
    assert set(captured[0]["root_overlay_paths"]) == {battery, candidate}


@pytest.mark.asyncio
@pytest.mark.parametrize("redact_derived", [True, False])
async def test_runner_startup_failure_survives_child_protocol(
    tmp_path: Path,
    redact_derived: bool,
) -> None:
    config = _config(tmp_path)
    package_root = tmp_path / "tooling"
    runner = package_root / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    detail = "startVitest exploded\nError: startVitest exploded\n" + ("stack-frame\n" * 100)
    result = {
        "ok": False,
        "mode": "run",
        "diagnostics": [],
        "tests": [
            {
                "caseId": "opaque-runner-failure",
                "category": "runner",
                "message": detail,
            }
        ],
        "captured": {"stdout": "", "stderr": ""},
    }
    runner.write_text(
        "import sys\nsys.stdin.read()\n"
        f"sys.stdout.write({json.dumps(json.dumps(result))})\n"
        "raise SystemExit(1)\n",
        encoding="utf-8",
    )
    compiler = tmp_path / "typescript.js"
    compiler.write_text("", encoding="utf-8")
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node=sys.executable,
            package_root=package_root,
            compiler_module_path=compiler,
        )
    )

    protected = await _run_test_runner(
        client,
        tmp_path,
        config,
        files=(),
        redact_derived=redact_derived,
        timeout=2,
    )

    assert protected["ok"] is False
    assert protected["exitCode"] == 1
    assert protected["tests"] == result["tests"]
    assert _runner_validation_errors(protected) == ["Vitest runner startup failed: " + detail]


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["malformed", "crash", "valid-incomplete"])
async def test_runner_protocol_failure_never_exposes_child_output(
    tmp_path: Path,
    mode: str,
) -> None:
    config = _config(tmp_path)
    package_root = tmp_path / "tooling"
    runner = package_root / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    body = "import sys\nsys.stdin.read()\n"
    body += 'sys.stderr.write("CHILD-PROCESS-SENTINEL")\n'
    if mode == "malformed":
        body += 'sys.stdout.write("not-json")\nraise SystemExit(7)\n'
    elif mode == "crash":
        body += "raise SystemExit(7)\n"
    else:
        body += "sys.stdout.write('{\"ok\": true}')\n"
    runner.write_text(body, encoding="utf-8")
    compiler = tmp_path / "typescript.js"
    compiler.write_text("", encoding="utf-8")
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node=sys.executable,
            package_root=package_root,
            compiler_module_path=compiler,
        )
    )

    protected = await _run_test_runner(
        client,
        tmp_path,
        config,
        files=(),
        redact_derived=True,
        timeout=2,
    )

    assert protected["ok"] is False
    assert protected["failures"] == [{"category": "runner-protocol"}]
    assert protected["diagnostics"] == [{"code": "JAUNT_TS_RUNNER_PROTOCOL", "severity": "error"}]
    if mode in {"malformed", "crash"}:
        assert protected["exitCode"] == 7
    assert protected["captured"] == {"stdout": "", "stderr": ""}
    assert "CHILD-PROCESS-SENTINEL" not in json.dumps(protected)


@pytest.mark.asyncio
@pytest.mark.parametrize("reported_ok", [False, True])
async def test_nonzero_runner_exit_preserves_valid_failure_but_rejects_claimed_success(
    tmp_path: Path,
    reported_ok: bool,
) -> None:
    config = _config(tmp_path)
    package_root = tmp_path / "tooling"
    runner = package_root / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
    result = {
        "ok": reported_ok,
        "mode": "run",
        "diagnostics": (
            []
            if reported_ok
            else [
                {
                    "code": "JAUNT_TS_VITEST_COLLECTION",
                    "severity": "error",
                    "message": "collection failed",
                }
            ]
        ),
        "tests": [],
        "captured": {"stdout": "", "stderr": ""},
    }
    runner.write_text(
        "import json, sys\nsys.stdin.read()\n"
        f"sys.stdout.write({json.dumps(json.dumps(result))})\n"
        "raise SystemExit(7)\n",
        encoding="utf-8",
    )
    compiler = tmp_path / "typescript.js"
    compiler.write_text("", encoding="utf-8")
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node=sys.executable,
            package_root=package_root,
            compiler_module_path=compiler,
        )
    )

    protected = await _run_test_runner(
        client,
        tmp_path,
        config,
        files=(),
        redact_derived=True,
        timeout=2,
    )

    assert protected["ok"] is False
    assert protected["exitCode"] == 7
    if reported_ok:
        assert protected["failures"] == [{"category": "runner-protocol"}]
    else:
        assert protected["diagnostics"] == [
            {"code": "JAUNT_TS_VITEST_COLLECTION", "severity": "error"}
        ]


def test_generated_example_test_has_provenance_tier_and_runtime_facade_import(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    spec_path = "tests/tokens.jaunt-test.ts"
    (tmp_path / spec_path).write_text("// Test the public token facade.\n")
    request = _test_request(
        tmp_path,
        config,
        {"path": spec_path, "targets": ["token"]},
        {
            "ts:src/tokens/index": {
                "facadePath": "src/tokens/index.ts",
                "moduleId": "ts:src/tokens/index",
                "symbols": [{"name": "token", "kind": "function"}],
                "toolingProvenanceRecords": [
                    {
                        "id": "tooling:packageManager:package.json",
                        "digest": "sha256:pnpm",
                    }
                ],
                "sidecar": json.dumps(
                    {
                        "moduleId": "ts:src/tokens/index",
                        "toolingProvenanceRecords": [
                            {
                                "id": "tooling:packageManager:package.json",
                                "digest": "sha256:pnpm",
                            }
                        ],
                    }
                ),
                "specSource": "export function token(): string;",
                "apiSource": "export declare function token(): string;",
            }
        },
    )

    assert request.target_path == "tests/__generated__/tokens.example.test.ts"
    assert "../../src/tokens/index.js" in request.prompt
    assert "src/tokens/index.ts" not in request.prompt
    assert "toolingProvenanceRecords" not in request.context_files["_context/contract.json"]

    rendered = _with_test_header(
        'import { token } from "../../src/tokens/index.js";\n',
        tier="example",
        source_path=spec_path,
    )
    assert rendered.startswith("// ⚙️ jaunt:generated")
    assert "// jaunt:tier=example\n" in rendered
    assert f"// jaunt:source={spec_path}\n" in rendered


def test_test_request_supplies_typed_fixtures_and_enforces_typed_properties(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    spec_path = "tests/tokens.jaunt-test.ts"
    (tmp_path / spec_path).write_text("// Test the public token facade.\n")
    (tmp_path / "tests/fixtures.ts").write_text(
        'import { test as base } from "vitest";\n'
        "export const test = base.extend<{ clock: { now(): number } }>({\n"
        "  clock: async ({}, use) => use({ now: () => 1 }),\n"
        "});\n"
    )
    modules = {
        "ts:src/tokens/index": {
            "facadePath": "src/tokens/index.ts",
            "moduleId": "ts:src/tokens/index",
            "symbols": [{"name": "token", "kind": "function"}],
            "specSource": (
                "/** Create one token.\n"
                " * @fixtures clock\n"
                " * @prop given value: fc.string() :: token(value) equals token(value)\n"
                " */\n"
                "export function token(value: string): string;\n"
            ),
            "apiSource": "export declare function token(value: string): string;",
            "contextSource": (
                'export const authored = "preserved";\n\n'
                "// <jaunt:imported-type-context version=1>\n"
                "// <jaunt:imported-type-source "
                '{"id":"workspace:src/components/entity-highlights.tsx",'
                '"priority":"requested"}>\n'
                "export interface MemoryEntityItem {\n"
                "  id: string;\n"
                "  name: string;\n"
                "  one_liner: string;\n"
                "  entity_type: string;\n"
                "}\n"
                "// </jaunt:imported-type-source>\n"
                "// </jaunt:imported-type-context>\n"
            ),
        }
    }

    request = _test_request(
        tmp_path,
        config,
        {"path": spec_path, "targets": ["token"]},
        modules,
        build_instructions=("Populate every required MemoryEntityItem field.",),
    )

    assert request.context_files["_context/fixtures.ts"].startswith("import { test as base }")
    imported_context = request.context_files["_context/imported-types/00-entity-highlights.tsx"]
    assert "interface MemoryEntityItem" in imported_context
    assert "entity_type: string" in imported_context
    assert "Populate every required MemoryEntityItem field." in request.prompt
    assert "_context/imported-types/" in request.prompt
    contract_context = request.context_files["_context/contract.json"]
    assert 'export const authored = "preserved";' in json.loads(contract_context)["contextSource"]
    assert "<jaunt:imported-type-context" not in contract_context
    assert request.cache_payload["buildInstructions"] == (
        "Populate every required MemoryEntityItem field.",
    )
    assert "../fixtures.js" in request.prompt
    assert request.cache_payload["fixturePath"] == "tests/fixtures.ts"
    assert request.validator is not None
    property_block = request.cache_payload["propertyBlock"]
    assert isinstance(property_block, str)
    assert "const __jauntPropertyArbitrary_" in property_block
    assert "fc.Arbitrary<string> = fc.string();" in property_block
    assert 'from "../fixtures.js"' in property_block
    assert "({ clock }) =>" in property_block
    valid = request.validator(
        'import { test } from "../fixtures.js";\n'
        'test("example", ({ clock }) => {\n'
        "  void clock;\n"
        "});\n"
    )
    assert valid == []
    invalid = request.validator(
        'import fc from "fast-check";\n'
        'import { test } from "vitest";\n'
        'test("property", () => {\n'
        "  fc.assert(fc.property(fc.string(), () => true));\n"
        "});\n"
    )
    assert isinstance(invalid, list)
    invalid_messages = "\n".join(str(error) for error in invalid)
    assert "extended test" in invalid_messages
    assert "destructure declared fixture clock" in invalid_messages
    assert "deterministic @prop rendering" in invalid_messages


def _test_imported_type_context_source(fields: str) -> str:
    metadata = json.dumps(
        {"id": "workspace:src/model.ts", "priority": "requested"},
        separators=(",", ":"),
    )
    return (
        'export const authored = "preserved";\n'
        "// <jaunt:imported-type-context version=1>\n"
        f"// <jaunt:imported-type-source {metadata}>\n"
        f"export interface Input {{ {fields} }}\n"
        "// </jaunt:imported-type-source>\n"
        "// </jaunt:imported-type-context>\n"
    )


def test_v2_imported_type_transport_cannot_be_delimiter_injected() -> None:
    authored = (
        'export const marker = "// <jaunt:imported-type-context version=2 '
        'encoding=base64-json>";\n'
        "// </jaunt:imported-type-context>\n"
    )
    declaration = 'export interface InjectedShape { marker: "// </jaunt:imported-type-context>"; }'
    payload = base64.b64encode(
        json.dumps(
            {
                "id": "workspace:src/injected.ts",
                "priority": "requested",
                "source": declaration,
            },
            separators=(",", ":"),
        ).encode("utf-8")
    ).decode("ascii")
    context_source = (
        authored
        + "\n// <jaunt:imported-type-context version=2 encoding=base64-json>\n"
        + f"// jaunt:imported-type-record={payload}\n"
        + "// </jaunt:imported-type-context>\n"
    )

    extracted_authored, extracted_block = _split_context_source(context_source)
    assert extracted_authored == authored
    assert extracted_block is not None
    files = _imported_type_context_files(({"contextSource": context_source},))
    assert len(files) == 1
    assert declaration in next(iter(files.values()))


def test_imported_type_context_files_enforces_one_utf8_multi_target_budget() -> None:
    def marked(*records: tuple[str, str, str]) -> str:
        lines = [
            'export const authored = "not copied";',
            "// <jaunt:imported-type-context version=1>",
        ]
        for source_id, priority, source in records:
            metadata = json.dumps(
                {"id": source_id, "priority": priority},
                separators=(",", ":"),
            )
            lines.extend(
                (
                    f"// <jaunt:imported-type-source {metadata}>",
                    source,
                    "// </jaunt:imported-type-source>",
                )
            )
        lines.append("// </jaunt:imported-type-context>")
        return "\n".join(lines) + "\n"

    huge_support = 'export type HugeSupport = "' + ("🙂" * 20_000) + '";'
    files = _imported_type_context_files(
        (
            {
                "contextSource": marked(
                    (
                        "workspace:src/a.ts",
                        "requested",
                        "export interface DirectA { a: string; }",
                    ),
                    ("workspace:src/a-support.ts", "supporting", huge_support),
                )
            },
            {
                "contextSource": marked(
                    (
                        "workspace:src/z.ts",
                        "requested",
                        "export interface DirectZ { z: number; }",
                    ),
                )
            },
        )
    )

    combined = "".join(files.values())
    assert "interface DirectA" in combined
    assert "interface DirectZ" in combined
    assert "HugeSupport" not in combined
    assert "Jaunt omitted 1 imported type-context records" in combined
    assert "not copied" not in combined
    assert sum(len(source.encode("utf-8")) for source in files.values()) <= 64 * 1024


def test_declared_fixture_without_canonical_surface_fails_before_generation(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    spec_path = "tests/tokens.jaunt-test.ts"
    (tmp_path / spec_path).write_text("// Test the public token facade.\n")
    modules = {
        "ts:src/tokens/index": {
            "facadePath": "src/tokens/index.ts",
            "moduleId": "ts:src/tokens/index",
            "symbols": [{"name": "token", "kind": "function"}],
            "specSource": "/** @fixtures clock */\nexport function token(): string;\n",
            "apiSource": "export declare function token(): string;",
        }
    }

    with pytest.raises(JauntConfigError, match="no canonical fixtures"):
        _test_request(
            tmp_path,
            config,
            {"path": spec_path, "targets": ["token"]},
            modules,
        )


def test_auto_class_tests_create_stable_virtual_intents_without_duplicates(
    tmp_path: Path,
) -> None:
    _config(tmp_path)
    config_path = tmp_path / "jaunt.toml"
    config_path.write_text(
        config_path.read_text().replace(
            'test_projects = ["tsconfig.test.json"]\n',
            'test_projects = ["tsconfig.test.json"]\nauto_class_tests = true\n',
        )
    )
    config = load_config(root=tmp_path)
    modules = {
        "ts:src/store": {
            "moduleId": "ts:src/store",
            "symbols": [
                {"name": "Store", "kind": "class", "options": {}},
                {"name": "helper", "kind": "function", "options": {}},
            ],
        }
    }

    records = _implicit_class_test_specs(tmp_path, config, modules)
    assert records == (
        {
            "path": "tests/auto.src-store-Store.jaunt-test.ts",
            "targets": ["ts:src/store#Store"],
            "syntheticSource": (
                "// Jaunt implicit class-test intent for ts:src/store#Store.\n"
                "// Derive public examples only from the class TSDoc and API mirror.\n"
            ),
        },
    )
    assert (
        _implicit_class_test_specs(
            tmp_path,
            config,
            modules,
            explicit_specs=({"targets": ["ts:src/store#Store"]},),
        )
        == ()
    )


@pytest.mark.asyncio
async def test_auto_class_test_runs_through_generation_and_atomic_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path)
    config_path = tmp_path / "jaunt.toml"
    config_path.write_text(
        config_path.read_text().replace(
            'test_projects = ["tsconfig.test.json"]\n',
            'test_projects = ["tsconfig.test.json"]\nauto_class_tests = true\n',
        )
    )
    config = load_config(root=tmp_path)
    worker = FakeWorker(tmp_path)
    worker.module["symbols"] = [
        *worker.module["symbols"],
        {"name": "Store", "kind": "class", "options": {}},
    ]

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "batches": {"tsconfig.test.json": {"ok": True}}}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    generated = {
        "tests/__generated__/auto.src-math-Store.example.test.ts",
        "tests/__generated__/auto.src-math-Store.derived.test.ts",
    }
    assert report.exit_code == 0
    assert report.generated == frozenset(generated)
    for path in generated:
        assert (tmp_path / path).read_text().startswith("// ⚙️ jaunt:generated")


@pytest.mark.asyncio
async def test_test_command_forwards_build_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    captured: dict[str, Any] = {}
    progress = object()

    async def failed_build(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        captured.update(kwargs)
        return TargetBuildReport(language="ts", exit_code=3)

    monkeypatch.setattr("jaunt.typescript.tester.run_build", failed_build)
    report = await run_test(
        tmp_path,
        config,
        build_instructions=("Keep it small.",),
        semantic_gate_enabled=False,
        force=True,
        progress=progress,
    )

    assert report.exit_code == 3
    assert captured["build_instructions"] == ("Keep it small.",)
    assert captured["semantic_gate_enabled"] is False
    assert captured["force"] is True
    assert captured["progress"] is progress
    assert captured["finish_progress"] is False


@pytest.mark.asyncio
async def test_test_instruction_reaches_both_tiers_without_becoming_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CapturingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.prompts: dict[str, str] = {}

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.prompts[str(request.cache_payload["tier"])] = request.prompt
            assert request.cache_payload["buildInstructions"] == (
                "Use all eight required MemoryEntityItem fields.",
            )
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    generator = CapturingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        build_instructions=("Use all eight required MemoryEntityItem fields.",),
        generator=generator,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert set(generator.prompts) == {"example", "derived"}
    assert all(
        "Use all eight required MemoryEntityItem fields." in prompt
        for prompt in generator.prompts.values()
    )
    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert not [item for item in status.diagnostics if "TEST_BATTERY" in item.code]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_mode", ["none", "retirement", "input-drift"])
async def test_failed_vitest_run_repairs_once_with_protected_feedback_and_reruns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    implementation = tmp_path / "src/__generated__/math.ts"
    example_battery = tmp_path / "tests/__generated__/math.example.test.ts"
    derived_battery = tmp_path / "tests/__generated__/math.derived.test.ts"
    implementation.parent.mkdir(parents=True)
    original_implementation = b"old implementation bytes\n"
    implementation.write_bytes(original_implementation)
    journal = tmp_path / "JAUNT_LOG"
    journal.write_text("prior journal\n", encoding="utf-8")
    build_calls: list[dict[str, Any]] = []

    async def fake_build(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        build_calls.append(dict(kwargs))
        if len(build_calls) == 1:
            return TargetBuildReport(
                language="ts",
                skipped=frozenset({"ts:src/math"}),
                metadata={
                    "phase": "initial",
                    "cost": _cost(prompt=5, completion=2, cached=1, estimated=0.1),
                },
            )
        repair_root = Path(_args[0])
        assert repair_root != tmp_path
        assert not (repair_root / "tests/__generated__/math.derived.test.ts").exists()
        assert (repair_root / "tests/__generated__/math.example.test.ts").is_file()
        (repair_root / "src/__generated__/math.ts").write_text("repaired implementation bytes\n")
        return TargetBuildReport(
            language="ts",
            generated=frozenset({"ts:src/math"}),
            metadata={
                "phase": "repair",
                "cost": _cost(prompt=7, completion=3, estimated=0.2),
            },
        )

    run_calls: list[tuple[str, ...]] = []

    async def batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        run_calls.append(files)
        if len(run_calls) == 1:
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        "file": "tests/__generated__/math.derived.test.ts",
                        "tier": "derived",
                        "status": "failed",
                        "caseId": "1111111111111111",
                        "category": "assertion",
                        "durationMs": 1,
                        "message": "DERIVED_SECRET",
                        "stack": "DERIVED_SECRET_STACK",
                    },
                    {
                        "file": "tests/__generated__/math.example.test.ts",
                        "tier": "example",
                        "status": "failed",
                        "caseId": "authored-example",
                        "category": "assertion",
                        "durationMs": 2,
                        "message": "AUTHORED EXAMPLE DETAIL",
                        "stack": "EXAMPLE_STACK_NOT_NEEDED",
                    },
                ],
                "diagnostics": [
                    {
                        "code": "TS9999",
                        "severity": "error",
                        "path": "tests/__generated__/math.derived.test.ts",
                        "message": "DIAGNOSTIC_SECRET",
                    }
                ],
                "captured": {"stdout": "CAPTURED_SECRET", "stderr": "CAPTURED_SECRET"},
            }
        return {"ok": True, "mode": "run", "tests": [], "captured": {}}

    journal_calls = 0

    def append_after_commit(root: Path, events: Sequence[JournalEvent]) -> bool:
        nonlocal journal_calls
        journal_calls += 1
        assert implementation.read_text(encoding="utf-8") == "repaired implementation bytes\n"
        assert not tuple((root / ".jaunt/transactions").glob("test-repair-*.json"))
        # An ordinary Jaunt append can interleave here without participating in
        # repair rollback or causing a false validation claim.
        append_journal_events(
            root,
            (JournalEvent("build", "ts:other", "ordinary concurrent append"),),
        )
        return append_journal_events(root, events)

    cleanup_calls: list[str] = []
    original_cleanup = ts_tester._clear_rejected_test_candidate

    def track_cleanup(root: Path, target_path: str, **kwargs: Any) -> bool:
        cleanup_calls.append(target_path)
        return original_cleanup(root, target_path, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester.run_build", fake_build)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", batches)
    monkeypatch.setattr("jaunt.typescript.tester.append_events", append_after_commit)
    monkeypatch.setattr(ts_tester, "_clear_rejected_test_candidate", track_cleanup)
    default_backend = FakeGenerator()
    monkeypatch.setattr(ts_tester, "_default_backend", lambda _config: default_backend)
    requested_generator = None if failure_mode == "none" else FakeGenerator()
    mutated_expected_input = False
    if failure_mode == "input-drift":
        original_verify = ts_tester._verify_test_runtime_identity

        def mutate_expected_input_during_guard(
            operation_root: Path,
            client: Any,
            initialized: object,
            expected_runner: str,
        ) -> None:
            nonlocal mutated_expected_input
            original_verify(operation_root, client, initialized, expected_runner)
            markers = tuple((operation_root / ".jaunt/transactions").glob("test-repair-*.json"))
            if mutated_expected_input or not markers:
                return
            (operation_root / "src/math.jaunt.ts").write_text(
                worker.spec_source + "// concurrent expected-input edit\n",
                encoding="utf-8",
            )
            mutated_expected_input = True

        monkeypatch.setattr(
            ts_tester,
            "_verify_test_runtime_identity",
            mutate_expected_input_during_guard,
        )
    if failure_mode == "retirement":
        original_retire = ts_tester._retire_transaction_manifest

        def fail_outer_retirement(
            manifest: Path,
            payload: Mapping[str, Any],
            *,
            pinned_directory: Any = None,
        ) -> bool:
            if manifest.name.startswith("test-repair-"):
                return False
            return original_retire(
                manifest,
                payload,
                pinned_directory=pinned_directory,
            )

        monkeypatch.setattr(
            ts_tester,
            "_retire_transaction_manifest",
            fail_outer_retirement,
        )
    progress = object()
    if failure_mode == "retirement":
        with pytest.raises(JauntConfigError, match="durably retire.*test-repair marker"):
            await run_test(
                tmp_path,
                config,
                generator=requested_generator,
                worker_factory=lambda *_: worker,
                progress=progress,
            )
    elif failure_mode == "input-drift":
        with pytest.raises(
            JauntGenerationError,
            match=r"inputs changed after analysis.*src/math\.jaunt\.ts",
        ):
            await run_test(
                tmp_path,
                config,
                generator=requested_generator,
                worker_factory=lambda *_: worker,
                progress=progress,
            )

    if failure_mode != "none":
        assert implementation.read_bytes() == original_implementation
        assert not example_battery.exists()
        assert not derived_battery.exists()
        assert cleanup_calls == []
        assert journal_calls == 0
        assert journal.read_text(encoding="utf-8") == "prior journal\n"
        markers = tuple((tmp_path / ".jaunt/transactions").glob("test-repair-*.json"))
        if failure_mode == "retirement":
            assert markers
        else:
            assert mutated_expected_input is True
            assert not markers
        return

    report = await run_test(
        tmp_path,
        config,
        generator=requested_generator,
        worker_factory=lambda *_: worker,
        progress=progress,
    )

    assert report.exit_code == 0
    assert len(build_calls) == 2
    assert build_calls[0]["progress"] is progress
    assert build_calls[1]["progress"] is progress
    assert build_calls[0]["finish_progress"] is False
    assert build_calls[1]["finish_progress"] is False
    assert build_calls[0]["generator"] is None
    assert callable(build_calls[0]["generator_factory"])
    assert build_calls[0]["generator_factory"]() is default_backend
    assert build_calls[1]["generator"] is default_backend
    assert build_calls[1]["force"] is True
    assert build_calls[1]["max_attempts"] == 1
    assert build_calls[1]["target_ids"] == ("ts:src/math",)
    feedback = str(build_calls[1]["ephemeral_prompt"])
    assert "1111111111111111" in feedback
    assert '"category": "assertion"' in feedback
    assert "AUTHORED EXAMPLE DETAIL" in feedback
    for secret in (
        "DERIVED_SECRET",
        "DERIVED_SECRET_STACK",
        "DIAGNOSTIC_SECRET",
        "CAPTURED_SECRET",
        "EXAMPLE_STACK_NOT_NEEDED",
        "tests/__generated__/math.derived.test.ts",
    ):
        assert secret not in feedback
    assert implementation.read_text() == "repaired implementation bytes\n"
    assert (tmp_path / "tests/__generated__/math.derived.test.ts").is_file()
    assert len(run_calls) == 2
    assert run_calls[0] == run_calls[1]
    assert report.runner["build"]["phase"] == "initial"
    assert report.runner["repair"]["build"]["phase"] == "repair"
    assert report.runner["repair"]["reran"] is True
    assert "DERIVED_SECRET" not in json.dumps(report.runner["repair"]["initial_runner"])
    assert cleanup_calls
    assert journal_calls == 1
    journal_text = journal.read_text(encoding="utf-8")
    assert journal_text.index("ordinary concurrent append") < journal_text.index(
        "TypeScript test repair validated"
    )
    assert report.runner["cost"] == {
        "api_calls": 4,
        "cache_hits": 0,
        "prompt_tokens": 52,
        "cached_prompt_tokens": 1,
        "completion_tokens": 25,
        "total_tokens": 77,
        "estimated_cost_usd": 0.3,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [False, True])
async def test_failed_test_and_repair_preserve_outputs_and_stage_valid_batteries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    existing: bool,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    outputs = (
        tmp_path / "tests/__generated__/math.example.test.ts",
        tmp_path / "tests/__generated__/math.derived.test.ts",
    )
    before: dict[Path, bytes | None] = {}
    for index, output in enumerate(outputs):
        if existing:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(f"prior passing battery {index}\n".encode())
        before[output] = output.read_bytes() if output.exists() else None

    build_calls = 0

    async def failed_repair(*_args: Any, **_kwargs: Any) -> TargetBuildReport:
        nonlocal build_calls
        build_calls += 1
        if build_calls == 1:
            return TargetBuildReport(language="ts", skipped=frozenset({"ts:src/math"}))
        repair_root = Path(_args[0])
        repair_example = repair_root / "tests/__generated__/math.example.test.ts"
        repair_derived = repair_root / "tests/__generated__/math.derived.test.ts"
        assert repair_example.is_file()
        assert repair_example.read_bytes() != before[outputs[0]]
        assert not repair_derived.exists()
        return TargetBuildReport(
            language="ts",
            failed={
                "ts:src/math": (
                    TargetDiagnostic(code="JAUNT_TS_REPAIR_FAILED", message="no repair"),
                )
            },
            exit_code=3,
        )

    async def failing_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": "tests/__generated__/math.example.test.ts",
                    "tier": "example",
                    "status": "failed",
                    "caseId": "candidate-failed",
                    "category": "assertion",
                }
            ],
        }

    cache = ResponseCache(tmp_path / ".test-response-cache")
    monkeypatch.setattr("jaunt.typescript.tester.run_build", failed_repair)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", failing_batches)
    report = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert build_calls == 2
    for output, original in before.items():
        assert (output.read_bytes() if output.exists() else None) == original
    assert cache.info()["entries"] == 2
    assert {item["state"] for item in report.runner["batteries"]} == {"staged"}


@pytest.mark.asyncio
async def test_passing_test_candidate_stages_cache_before_atomic_disk_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    outputs = (
        tmp_path / "tests/__generated__/math.example.test.ts",
        tmp_path / "tests/__generated__/math.derived.test.ts",
    )
    cache = ResponseCache(tmp_path / ".test-response-cache")
    saw_run = False

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal saw_run
        if not kwargs.get("typecheck_only"):
            saw_run = True
            assert all(not output.exists() for output in outputs)
            assert cache.info()["entries"] == 2
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert saw_run is True
    assert all(output.is_file() for output in outputs)
    assert cache.info()["entries"] == 2
    assert report.runner["jobs"] == config.test.jobs


@pytest.mark.asyncio
async def test_full_battery_commit_rolls_back_runtime_change_after_worker_exit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    outputs = (
        tmp_path / "tests/__generated__/math.example.test.ts",
        tmp_path / "tests/__generated__/math.derived.test.ts",
    )

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    class ArmRuntimeMutationGenerator(FakeGenerator):
        armed = False

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            generated = await super().generate_request(request, **kwargs)
            if not self.armed:
                worker.arm_runtime_removal_after_next_verification()
                self.armed = True
            return generated

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            no_run=True,
            jobs=1,
            generator=ArmRuntimeMutationGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert worker.session_exited is True
    assert worker.verification_session_states[-2:] == [True, True]
    assert not any(output.exists() for output in outputs)
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.asyncio
async def test_test_generation_honors_jobs_and_reports_each_staged_tier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
        }

    class ConcurrentGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.active = 0
            self.max_active = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            try:
                await asyncio.sleep(0.05)
                return await super().generate_request(request, **kwargs)
            finally:
                self.active -= 1

    generator = ConcurrentGenerator()
    progress_totals: list[int] = []
    progress_phases: list[tuple[str, str, str]] = []

    class RecordingProgress:
        def set_total(self, total: int) -> None:
            progress_totals.append(total)

        def phase(self, item: str, stage: str, detail: str = "") -> None:
            progress_phases.append((item, stage, detail))

        def advance(self, _item: str, *, ok: bool) -> None:
            assert ok is True

        def finish(self) -> None:
            return None

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        generator=generator,
        progress=RecordingProgress(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.max_active == 2
    assert progress_totals == [2]
    assert {(item, detail) for item, stage, detail in progress_phases if stage == "generating"} == {
        ("tests/__generated__/math.derived.test.ts", "derived"),
        ("tests/__generated__/math.example.test.ts", "example"),
    }
    assert report.runner["jobs"] == 2
    assert report.runner["batteries"] == [
        {
            "path": "tests/__generated__/math.derived.test.ts",
            "tier": "derived",
            "state": "staged",
            "attempts": 1,
            "retry_count": 0,
            "retry_reasons": (),
        },
        {
            "path": "tests/__generated__/math.example.test.ts",
            "tier": "example",
            "state": "staged",
            "attempts": 1,
            "retry_count": 0,
            "retry_reasons": (),
        },
    ]


@pytest.mark.asyncio
async def test_test_generation_retries_live_candidate_overlay_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def candidate_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        overlays = kwargs.get("overlays", {})
        if kwargs.get("typecheck_only") and any(
            "__DYNAMIC_LOADER__" in source for source in overlays.values()
        ):
            return _redact_runner_result(
                {
                    "ok": False,
                    "mode": "typecheck",
                    "diagnostics": [
                        {
                            "code": "JAUNT_TS_TEST_DYNAMIC_LOADER",
                            "message": "generated tests must use static imports",
                            "severity": "error",
                            "path": next(iter(overlays)),
                        }
                    ],
                    "tests": [],
                    "captured": {"stdout": "", "stderr": ""},
                },
                enabled=True,
            )
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    class RepairingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}
            self.feedback: dict[str, list[list[str] | None]] = {}

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            tier = str(request.cache_payload["tier"])
            self.calls[tier] = self.calls.get(tier, 0) + 1
            raw_feedback = kwargs.get("extra_error_context")
            self.feedback.setdefault(tier, []).append(
                raw_feedback if isinstance(raw_feedback, list) else None
            )
            if self.calls[tier] == 1:
                return (
                    "const __DYNAMIC_LOADER__ = true;\n",
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    generator = RepairingGenerator()
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", candidate_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        max_attempts=2,
        generator=generator,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.calls == {"derived": 2, "example": 2}
    reason_by_tier = {item["tier"]: item["retry_reasons"] for item in report.runner["batteries"]}
    for tier in ("derived", "example"):
        path = f"tests/__generated__/math.{tier}.test.ts"
        reason = f"JAUNT_TS_TEST_DYNAMIC_LOADER: generated tests must use static imports ({path})"
        assert generator.feedback[tier][1] == [f"previous output errors: {reason}"]
        assert reason_by_tier[tier] == (reason,)
    assert {item["attempts"] for item in report.runner["batteries"]} == {2}
    assert {item["retry_count"] for item in report.runner["batteries"]} == {1}


@pytest.mark.asyncio
async def test_test_generation_runner_infrastructure_stops_retries_and_caches_live_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".live-runner-infrastructure-cache")
    example_path = "tests/__generated__/math.example.test.ts"
    runner_available = False

    async def candidate_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only") and not runner_available and files == (example_path,):
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "failures": [{"category": "runner-protocol"}],
            }
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            tier = str(request.cache_payload["tier"])
            self.calls[tier] = self.calls.get(tier, 0) + 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", candidate_batches)
    generator = CountingGenerator()
    first = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        max_attempts=3,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert first.exit_code == 3
    assert generator.calls == {"example": 1, "derived": 1}
    assert first.runner["test_cost"]["api_calls"] == 2
    assert first.runner["test_cost"]["prompt_tokens"] == 40
    assert first.runner["test_cost"]["completion_tokens"] == 20
    assert cache.info()["entries"] == 2
    outcomes = {item["path"]: item for item in first.runner["batteries"]}
    example = outcomes[example_path]
    assert example["state"] == "infrastructure-failed"
    assert example["attempts"] == 1
    assert example["retry_count"] == 0
    assert example["infrastructure_retries"] == 0
    assert "candidate" not in example
    assert {
        diagnostic.code for diagnostics in first.failed.values() for diagnostic in diagnostics
    } == {"JAUNT_TS_TEST_INFRASTRUCTURE"}
    assert not (tmp_path / _rejected_test_paths(example_path)[1]).exists()

    runner_available = True
    recovered = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        max_attempts=3,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert recovered.exit_code == 0
    assert generator.calls == {"example": 1, "derived": 1}
    assert recovered.runner["test_cost"]["api_calls"] == 0
    assert recovered.runner["test_cost"]["cache_hits"] == 1
    recovered_outcomes = {item["path"]: item for item in recovered.runner["batteries"]}
    assert recovered_outcomes[example_path]["attempts"] == 0
    assert (tmp_path / example_path).is_file()
    assert cache.info()["entries"] == 2


@pytest.mark.asyncio
async def test_cached_test_candidate_runner_infrastructure_is_not_evicted_or_charged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".cached-runner-infrastructure-cache")
    example_path = "tests/__generated__/math.example.test.ts"
    runner_available = True

    async def candidate_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only") and not runner_available and files == (example_path,):
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "failures": [{"category": "timeout"}],
                "timedOut": True,
            }
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", candidate_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    assert cache.info()["entries"] == 2
    (tmp_path / example_path).unlink()

    runner_available = False
    blocked = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=3,
        generator=ExplodingGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert blocked.exit_code == 3
    assert blocked.runner["test_cost"]["api_calls"] == 0
    assert blocked.runner["test_cost"]["cache_hits"] == 0
    assert blocked.runner["cache"]["hits"] == 1
    assert cache.info()["entries"] == 2
    outcomes = {item["path"]: item for item in blocked.runner["batteries"]}
    example = outcomes[example_path]
    assert example["state"] == "infrastructure-failed"
    assert example["attempts"] == 0
    assert example["retry_count"] == 0
    assert "cache_evicted" not in example
    assert "candidate" not in example

    runner_available = True
    recovered = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert recovered.exit_code == 0
    assert recovered.runner["test_cost"]["api_calls"] == 0
    assert recovered.runner["test_cost"]["cache_hits"] == 1
    assert recovered.runner["cache"]["hits"] == 2
    assert cache.info()["entries"] == 2
    assert (tmp_path / example_path).is_file()


@pytest.mark.asyncio
async def test_capacity_exhaustion_reports_failed_battery_and_preserves_completed_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".test-response-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    async def no_sleep(_delay: float) -> None:
        return None

    class CapacityGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def generate_request(
            self,
            request: GenerationRequest,
            **kwargs: Any,
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            tier = str(request.cache_payload["tier"])
            self.calls[tier] = self.calls.get(tier, 0) + 1
            if tier == "derived":
                raise JauntTransientGenerationError(
                    "codex exec failed: error event: Selected model is at capacity. "
                    "Please try a different model."
                )
            return await super().generate_request(request, **kwargs)

    generator = CapacityGenerator()
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert generator.calls == {"derived": 3, "example": 1}
    assert report.failed["tests/math.jaunt-test.ts#derived"][0].code == (
        "JAUNT_TS_TEST_INFRASTRUCTURE"
    )
    outcomes = {item["tier"]: item for item in report.runner["batteries"]}
    assert outcomes["derived"]["state"] == "infrastructure-failed"
    assert outcomes["derived"]["attempts"] == 0
    assert outcomes["derived"]["infrastructure_retries"] == 2
    assert len(outcomes["derived"]["infrastructure_errors"]) == 3
    assert outcomes["example"]["state"] == "committed"
    assert (tmp_path / "tests/__generated__/math.example.test.ts").is_file()
    assert cache.info()["entries"] == 1


@pytest.mark.asyncio
async def test_generation_infrastructure_failure_preserves_completed_peer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".ordinary-infrastructure-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    class InfrastructureGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self,
            request: GenerationRequest,
            **kwargs: Any,
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            tier = str(request.cache_payload["tier"])
            self.calls.append(tier)
            if tier == "example":
                raise JauntGenerationError("simulated provider protocol failure")
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    generator = InfrastructureGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert generator.calls == ["example", "derived"]
    failure = report.failed["tests/math.jaunt-test.ts#example"][0]
    assert failure.code == "JAUNT_TS_TEST_INFRASTRUCTURE"
    assert "simulated provider protocol failure" in failure.message
    outcomes = {item["tier"]: item for item in report.runner["batteries"]}
    assert outcomes["example"]["state"] == "infrastructure-failed"
    assert outcomes["example"]["attempts"] == 0
    assert outcomes["example"]["infrastructure_errors"] == ("simulated provider protocol failure",)
    assert outcomes["derived"]["state"] == "committed"
    assert (tmp_path / "tests/__generated__/math.derived.test.ts").is_file()
    assert cache.info()["entries"] == 1

    payload = typescript_test_payload(report)
    assert payload["failed"]["tests/math.jaunt-test.ts#example"][0]["code"] == (
        "JAUNT_TS_TEST_INFRASTRUCTURE"
    )
    payload_outcomes = {item["tier"]: item for item in payload["vitest"]["batteries"]}
    assert payload_outcomes["example"]["state"] == "infrastructure-failed"
    summary = "\n".join(human_lines(payload))
    assert "tests/math.jaunt-test.ts#example" in summary
    assert "JAUNT_TS_TEST_INFRASTRUCTURE: simulated provider protocol failure" in summary


@pytest.mark.asyncio
async def test_terminal_quota_exhaustion_aborts_test_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".quota-exhaustion-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    class QuotaGenerator(FakeGenerator):
        async def generate_request(
            self,
            request: GenerationRequest,
            **kwargs: Any,
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            raise JauntQuotaGenerationError(
                f"terminal {request.cache_payload['tier']} test-generation quota"
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(JauntQuotaGenerationError, match="terminal .* test-generation quota"):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            jobs=1,
            generator=QuotaGenerator(),
            response_cache=cache,
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()
    assert cache.info()["entries"] == 0


@pytest.mark.asyncio
async def test_budget_abort_drains_already_failed_sibling_tasks(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    ready = asyncio.Event()
    started = 0
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    unhandled: list[dict[str, object]] = []

    class ConcurrentFailureGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **_kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            nonlocal started
            started += 1
            if started == 2:
                ready.set()
            await ready.wait()
            raise JauntBudgetExceededError(
                f"simulated {request.cache_payload['tier']} generation failure"
            )

    def capture_unhandled(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        unhandled.append(dict(context))

    loop.set_exception_handler(capture_unhandled)
    try:
        with pytest.raises(JauntBudgetExceededError, match="simulated .* generation failure"):
            await run_test(
                tmp_path,
                config,
                no_build=True,
                jobs=2,
                generator=ConcurrentFailureGenerator(),
                response_cache=ResponseCache(tmp_path / ".test-response-cache"),
                worker_factory=lambda *_: worker,
            )
        # Force task finalizers while this loop's handler is still installed.
        # Before the all-task drain, the already-done sibling reports here.
        for _ in range(2):
            gc.collect()
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(previous_handler)

    assert started == 2
    assert not [
        context
        for context in unhandled
        if context.get("message") == "Task exception was never retrieved"
    ]


@pytest.mark.asyncio
async def test_late_generation_failure_resumes_from_staged_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".test-response-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
        }

    class LateFailureGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.cache_payload.get("tier") == "derived":
                return (
                    'import "../../src/__generated__/math.js";\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    failed = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        max_attempts=1,
        generator=LateFailureGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert failed.exit_code == 3
    assert cache.info()["entries"] == 1
    example_path = "tests/__generated__/math.example.test.ts"
    assert (tmp_path / example_path).is_file()
    assert failed.runner["partial_landing"]["accepted"] == (example_path,)
    assert failed.runner["partial_landing"]["committed"] is True

    resumed_generator = CountingGenerator()
    resumed = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        generator=resumed_generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert resumed.exit_code == 0
    assert resumed_generator.calls == 1
    assert example_path in resumed.skipped
    assert resumed.runner["cache"]["hits"] == 0
    assert cache.info()["entries"] == 2


@pytest.mark.asyncio
async def test_partial_battery_commit_rolls_back_runtime_change_inside_worker_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    example_path = tmp_path / "tests/__generated__/math.example.test.ts"

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    class LateFailureWithRuntimeMutationGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.cache_payload.get("tier") == "derived":
                return (
                    'import "../../src/__generated__/math.js";\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            generated = await super().generate_request(request, **kwargs)
            worker.arm_runtime_removal_after_next_verification()
            return generated

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            jobs=1,
            max_attempts=1,
            generator=LateFailureWithRuntimeMutationGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert worker.session_exited is True
    assert worker.verification_session_states[-2:] == [False, False]
    assert not example_path.exists()
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.asyncio
async def test_sibling_generation_failure_persists_runtime_rejected_pending_candidate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".pending-rejection-cache")
    example_path = "tests/__generated__/math.example.test.ts"

    async def reject_example_runtime(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": example_path,
                    "tier": "example",
                    "status": "failed",
                    "category": "assertion",
                    "message": "candidate behavior failed",
                }
            ],
            "failures": [{"category": "assertion"}],
        }

    class SiblingFailureGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.cache_payload.get("tier") == "derived":
                return (
                    'import "../../src/__generated__/math.js";\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        reject_example_runtime,
    )
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=SiblingFailureGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert not (tmp_path / example_path).exists()
    assert cache.info()["entries"] == 0
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    example = outcomes[example_path]
    assert example["state"] == "rejected"
    candidate = tmp_path / example["candidate"]
    metadata_path = tmp_path / example["candidate_metadata"]
    candidate_source = candidate.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert "const __jaunt_impl_double" in candidate_source
    assert metadata["terminal"] is False
    assert metadata["attempts_this_run"] == 1
    assert metadata["candidate_digest"] == _digest(candidate_source)
    assert metadata["errors"] == list(example["rejection_reasons"])


@pytest.mark.asyncio
async def test_runtime_rejected_no_property_candidate_keeps_exact_generator_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    example_path = "tests/__generated__/math.example.test.ts"

    async def reject_example(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": example_path,
                    "tier": "example",
                    "status": "failed",
                    "category": "assertion",
                }
            ],
            "failures": [{"category": "assertion"}],
        }

    class ExactBytesGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.example_source = ""

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            source, usage, advisories = await super().generate_request(request, **kwargs)
            if request.target_path == example_path:
                assert request.cache_payload.get("propertyBlock") == ""
                source = source.rstrip("\n") + "  \t"
                self.example_source = source
            return source, usage, advisories

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", reject_example)
    generator = ExactBytesGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".exact-runtime-rejection-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    rejected = outcomes[example_path]
    assert rejected["state"] == "rejected"
    candidate = tmp_path / rejected["candidate"]
    metadata = json.loads((tmp_path / rejected["candidate_metadata"]).read_text())
    assert candidate.read_bytes() == generator.example_source.encode("utf-8")
    assert metadata["candidate_digest"] == _digest(generator.example_source)


@pytest.mark.asyncio
async def test_bad_committed_baseline_retains_unrelated_candidate_for_cache_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    candidate_cache = ResponseCache(tmp_path / ".baseline-retained-candidate-cache")
    example_path = "tests/__generated__/math.example.test.ts"
    derived_path = "tests/__generated__/math.derived.test.ts"
    baseline_bad = False

    async def baseline_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        overlays = kwargs.get("overlays", {})
        if (
            kwargs.get("typecheck_only")
            and baseline_bad
            and derived_path in files
            and derived_path not in overlays
        ):
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "diagnostics": [
                    {
                        "code": "TS2322",
                        "message": "committed baseline is type-invalid",
                        "severity": "error",
                        "path": derived_path,
                    }
                ],
            }
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            assert request.target_path == example_path
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", baseline_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    (tmp_path / example_path).unlink()

    baseline_bad = True
    generator = CountingGenerator()
    blocked = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        generator=generator,
        response_cache=candidate_cache,
        worker_factory=lambda *_: worker,
    )

    assert blocked.exit_code == 3
    assert generator.calls == 1
    assert candidate_cache.info()["entries"] == 1
    assert blocked.failed["typecheck"][0].code == "JAUNT_TS_TEST_TYPECHECK"
    partial = blocked.runner["partial_landing"]
    assert partial["accepted"] == ()
    assert partial["rejected"] == ()
    assert partial["retained"] == (example_path,)
    assert partial["committed"] is False
    assert partial["isolation"]["baseline_failure"] is True
    payload_partial = typescript_test_payload(blocked)["targets"]["ts"]["vitest"]["partial_landing"]
    assert payload_partial["accepted"] == ()
    assert payload_partial["retained"] == (example_path,)
    outcomes = {item["path"]: item for item in blocked.runner["batteries"]}
    example = outcomes[example_path]
    assert example["state"] == "staged"
    assert "candidate" not in example
    assert "cache_evicted" not in example
    assert not (tmp_path / _rejected_test_paths(example_path)[1]).exists()
    assert not (tmp_path / example_path).exists()

    baseline_bad = False
    recovered = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        generator=generator,
        response_cache=candidate_cache,
        worker_factory=lambda *_: worker,
    )

    assert recovered.exit_code == 0
    assert generator.calls == 1
    assert recovered.runner["test_cost"]["api_calls"] == 0
    assert recovered.runner["test_cost"]["cache_hits"] == 1
    assert candidate_cache.info()["entries"] == 1
    assert (tmp_path / example_path).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "runtime_category",
    [None, "runner-protocol", "preflight-runner", "baseline-runner"],
)
async def test_partial_landing_runs_surviving_baseline_and_retains_infrastructure_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_category: str | None,
) -> None:
    config = _config(tmp_path)

    class TwoSpecWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.other_spec_path = "tests/other.jaunt-test.ts"
            (root / self.other_spec_path).write_text("// Verify another public view.\n")

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "analyzeWorkspace":
                result["testSpecs"] = [
                    {
                        "path": self.test_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                    {
                        "path": self.other_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                ]
            return result

    worker = TwoSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    math_example = "tests/__generated__/math.example.test.ts"
    math_derived = "tests/__generated__/math.derived.test.ts"
    (tmp_path / math_example).unlink()
    (tmp_path / math_derived).unlink()

    class SiblingFailureGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.target_path == math_derived:
                return (
                    'import "../../src/__generated__/math.js";\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    runtime_files: list[tuple[str, ...]] = []

    async def observe_stage(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            files = tuple(kwargs.get("files", ()))
            if runtime_category == "preflight-runner" and len(files) > 1:
                return {
                    "ok": False,
                    "mode": "typecheck",
                    "tests": [],
                    "failures": [{"category": "runner-protocol"}],
                }
            if runtime_category == "baseline-runner" and len(files) > 1:
                if kwargs.get("overlays"):
                    return {
                        "ok": False,
                        "mode": "typecheck",
                        "tests": [],
                        "diagnostics": [
                            {
                                "code": "TS2451",
                                "message": "combined overlay conflict",
                                "severity": "error",
                            }
                        ],
                    }
                return {
                    "ok": False,
                    "mode": "typecheck",
                    "tests": [],
                    "failures": [{"category": "runner"}],
                }
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        files = tuple(kwargs.get("files", ()))
        runtime_files.append(files)
        if runtime_category is None:
            return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": runtime_category}],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", observe_stage)
    response_cache = ResponseCache(tmp_path / f".partial-stage-{runtime_category}-cache")
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=SiblingFailureGenerator(),
        response_cache=response_cache,
        worker_factory=lambda *_: worker,
    )

    assert seeded.exit_code == 0
    assert report.exit_code == 3
    if runtime_category in {"preflight-runner", "baseline-runner"}:
        assert runtime_files == []
    else:
        assert runtime_files == [
            (
                math_example,
                "tests/__generated__/other.derived.test.ts",
                "tests/__generated__/other.example.test.ts",
            )
        ]
    assert response_cache.info()["entries"] == 1
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    if runtime_category is None:
        assert report.runner["partial_landing"]["accepted"] == (math_example,)
        assert report.runner["partial_landing"]["retained"] == ()
        assert report.runner["partial_landing"]["committed"] is True
        assert (tmp_path / math_example).is_file()
    else:
        if runtime_category in {"preflight-runner", "baseline-runner"}:
            assert report.runner["partial_landing"]["accepted"] == ()
            assert report.runner["partial_landing"]["retained"] == (math_example,)
            payload_partial = typescript_test_payload(report)["vitest"]["partial_landing"]
            assert payload_partial["accepted"] == ()
            assert payload_partial["retained"] == (math_example,)
        else:
            assert report.runner["partial_landing"]["accepted"] == (math_example,)
            assert report.runner["partial_landing"]["retained"] == ()
        assert report.runner["partial_landing"]["committed"] is False
        assert not (tmp_path / math_example).exists()
        assert outcomes[math_example]["state"] == "staged"
        assert "candidate" not in outcomes[math_example]
        assert "cache_evicted" not in outcomes[math_example]
        failure_key = (
            "stage-preflight"
            if runtime_category in {"preflight-runner", "baseline-runner"}
            else "stage-runner"
        )
        assert {item.code for item in report.failed[failure_key]} == {
            "JAUNT_TS_TEST_INFRASTRUCTURE"
        }


@pytest.mark.asyncio
async def test_generation_failure_reruns_and_lands_runtime_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    class TwoSpecWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.other_spec_path = "tests/other.jaunt-test.ts"
            (root / self.other_spec_path).write_text("// Verify another public view.\n")

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "analyzeWorkspace":
                result["testSpecs"] = [
                    {
                        "path": self.test_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                    {
                        "path": self.other_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                ]
            return result

    worker = TwoSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0

    generated_paths = tuple(
        f"tests/__generated__/{stem}.{tier}.test.ts"
        for stem in ("math", "other")
        for tier in ("derived", "example")
    )
    for path in generated_paths:
        (tmp_path / path).unlink()
    generation_failed = "tests/__generated__/math.derived.test.ts"
    runtime_failed = "tests/__generated__/math.example.test.ts"
    survivors = (
        "tests/__generated__/other.derived.test.ts",
        "tests/__generated__/other.example.test.ts",
    )

    class OneGenerationFailure(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.target_path == generation_failed:
                return (
                    'import "../../src/__generated__/math.js";\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    runtime_runs: list[tuple[str, ...]] = []

    async def fail_one_then_pass(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_runs.append(files)
        if len(runtime_runs) == 1:
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        "file": runtime_failed,
                        "tier": "example",
                        "status": "failed",
                        "category": "assertion",
                        "message": "candidate-owned assertion",
                    }
                ],
                "failures": [],
            }
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", fail_one_then_pass)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=OneGenerationFailure(),
        response_cache=ResponseCache(tmp_path / ".stage-runtime-survivors-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert runtime_runs == [
        (runtime_failed, *survivors),
        survivors,
    ]
    partial = report.runner["partial_landing"]
    assert partial["accepted"] == survivors
    assert partial["rejected"] == (runtime_failed,)
    assert partial["committed"] is True
    assert all((tmp_path / path).is_file() for path in survivors)
    assert not (tmp_path / generation_failed).exists()
    assert not (tmp_path / runtime_failed).exists()
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert all(outcomes[path]["state"] == "committed" for path in survivors)
    assert outcomes[runtime_failed]["state"] == "rejected"


@pytest.mark.asyncio
async def test_preflight_isolation_reruns_and_lands_runtime_survivors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    class TwoSpecWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            self.other_spec_path = "tests/other.jaunt-test.ts"
            (root / self.other_spec_path).write_text("// Verify another public view.\n")

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "analyzeWorkspace":
                result["testSpecs"] = [
                    {
                        "path": self.test_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                    {
                        "path": self.other_spec_path,
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                ]
            return result

    worker = TwoSpecWorker(tmp_path)
    generated_paths = tuple(
        f"tests/__generated__/{stem}.{tier}.test.ts"
        for stem in ("math", "other")
        for tier in ("derived", "example")
    )
    runtime_failed = generated_paths[0]
    survivors = generated_paths[1:]
    preflight_failed = False
    runtime_runs: list[tuple[str, ...]] = []

    async def isolate_then_fail_one(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal preflight_failed
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only"):
            if len(files) == len(generated_paths) and not preflight_failed:
                preflight_failed = True
                return {
                    "ok": False,
                    "mode": "typecheck",
                    "tests": [],
                    "diagnostics": [
                        {
                            "code": "TS2451",
                            "message": "combined overlay conflict",
                            "severity": "error",
                        }
                    ],
                }
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_runs.append(files)
        if len(runtime_runs) == 1:
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        "file": runtime_failed,
                        "tier": "derived",
                        "status": "failed",
                        "category": "assertion",
                        "message": "candidate-owned assertion",
                    }
                ],
                "failures": [],
            }
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", isolate_then_fail_one)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        response_cache=ResponseCache(tmp_path / ".preflight-runtime-survivors-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert runtime_runs == [generated_paths, survivors]
    partial = report.runner["partial_landing"]
    assert partial["accepted"] == survivors
    assert partial["rejected"] == (runtime_failed,)
    assert partial["committed"] is True
    assert all((tmp_path / path).is_file() for path in survivors)
    assert not (tmp_path / runtime_failed).exists()
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert all(outcomes[path]["state"] == "committed" for path in survivors)
    assert outcomes[runtime_failed]["state"] == "rejected"


@pytest.mark.asyncio
async def test_cross_battery_preflight_lands_only_the_compatible_subset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".test-response-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            "tests": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    assert cache.info()["entries"] == 2
    derived_path = "tests/__generated__/math.derived.test.ts"
    derived_metadata = dict(
        _test_header_metadata((tmp_path / derived_path).read_text(encoding="utf-8")) or {}
    )
    rejected_request = GenerationRequest(
        language="ts",
        kind="test",
        target_path=derived_path,
        context_files={},
        prompt="generate",
        cache_payload={"path": worker.test_spec_path, "tier": "derived"},
        validator=lambda _source: [],
        project_root=tmp_path,
    )
    accepted_marker = _write_rejected_test_candidate(
        tmp_path,
        rejected_request,
        source_path=worker.test_spec_path,
        tier="derived",
        fingerprint=derived_metadata["battery_fingerprint"],
        candidate_source="export const previouslyRejected = true;\n",
        attempts=2,
        errors=("previous rejection",),
        expected_provenance=derived_metadata,
    )
    assert accepted_marker is not None
    for tier in ("example", "derived"):
        (tmp_path / f"tests/__generated__/math.{tier}.test.ts").unlink()

    async def cross_battery_failure(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if len(files) <= 1:
            return {
                "ok": True,
                "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
                "diagnostics": [],
                "tests": [],
            }
        return {
            "ok": False,
            "mode": "typecheck",
            "diagnostics": [
                {
                    "code": "TS2451",
                    "message": "cross-battery declaration conflict",
                    "severity": "error",
                }
            ],
            "tests": [],
        }

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        cross_battery_failure,
    )
    fresh_cache = ResponseCache(tmp_path / ".fresh-preflight-response-cache")
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        generator=FakeGenerator(),
        response_cache=fresh_cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert fresh_cache.info()["entries"] == 1
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert outcomes[derived_path]["state"] == "committed"
    assert outcomes["tests/__generated__/math.example.test.ts"]["state"] == "rejected"
    assert "cache_evicted" not in outcomes["tests/__generated__/math.example.test.ts"]
    assert "candidate" in outcomes["tests/__generated__/math.example.test.ts"]
    assert outcomes["tests/__generated__/math.example.test.ts"]["rejection_reasons"] == (
        "TS2451: cross-battery declaration conflict",
    )
    assert report.runner["partial_landing"]["accepted"] == (
        "tests/__generated__/math.derived.test.ts",
    )
    assert report.runner["partial_landing"]["rejected"] == (
        "tests/__generated__/math.example.test.ts",
    )
    assert report.runner["partial_landing"]["retained"] == ()
    assert report.runner["partial_landing"]["committed"] is True
    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert (tmp_path / derived_path).is_file()
    assert all(not (tmp_path / path).exists() for path in accepted_marker)


@pytest.mark.asyncio
async def test_mid_isolation_infrastructure_reports_only_proven_candidates_as_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".mid-isolation-infrastructure-cache")
    generated_paths = (
        "tests/__generated__/math.derived.test.ts",
        "tests/__generated__/math.example.test.ts",
    )
    combined_checks = 0

    async def interrupted_isolation(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal combined_checks
        files = tuple(kwargs.get("files", ()))
        if not kwargs.get("typecheck_only") or len(files) <= 1:
            return {
                "ok": True,
                "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
                "tests": [],
                "diagnostics": [],
            }
        combined_checks += 1
        if combined_checks == 1:
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "diagnostics": [
                    {
                        "code": "TS2451",
                        "message": "combined overlay conflict",
                        "severity": "error",
                    }
                ],
            }
        return {
            "ok": False,
            "mode": "typecheck",
            "tests": [],
            "failures": [{"category": "runner-protocol"}],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", interrupted_isolation)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=1,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert report.failed["typecheck"][0].code == "JAUNT_TS_TEST_INFRASTRUCTURE"
    partial = report.runner["partial_landing"]
    assert partial["accepted"] == (generated_paths[0],)
    assert partial["rejected"] == ()
    assert partial["retained"] == (generated_paths[1],)
    assert partial["committed"] is False
    assert partial["isolation"]["infrastructure"] is True
    payload_partial = typescript_test_payload(report)["vitest"]["partial_landing"]
    assert payload_partial["accepted"] == (generated_paths[0],)
    assert payload_partial["retained"] == (generated_paths[1],)
    assert cache.info()["entries"] == 2
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert {path: outcomes[path]["state"] for path in generated_paths} == {
        generated_paths[0]: "staged",
        generated_paths[1]: "staged",
    }
    assert all("cache_evicted" not in outcomes[path] for path in generated_paths)
    assert all(not (tmp_path / path).exists() for path in generated_paths)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("category", "attributed", "expected_entries"),
    [("assertion", True, 1), ("assertion", False, 1), ("runner", True, 2)],
)
async def test_compatible_subset_run_updates_partition_after_behavior_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
    attributed: bool,
    expected_entries: int,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".test-response-cache")
    cache.put(
        "unrelated-key",
        CacheEntry("unrelated", 1, 1, "unrelated-model", "unrelated-provider", 0.0),
    )

    async def cross_battery_then_behavior_failure(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only"):
            if len(files) <= 1:
                return {"ok": True, "mode": "typecheck", "diagnostics": [], "tests": []}
            return {
                "ok": False,
                "mode": "typecheck",
                "diagnostics": [
                    {
                        "code": "TS2451",
                        "message": "cross-battery declaration conflict",
                        "severity": "error",
                    }
                ],
                "tests": [],
            }
        assert files == ("tests/__generated__/math.derived.test.ts",)
        return {
            "ok": False,
            "mode": "run",
            "tests": (
                [
                    {
                        "file": files[0],
                        "tier": "derived",
                        "status": "failed",
                        "category": category,
                        "message": f"{category} failure",
                    }
                ]
                if attributed
                else []
            ),
            "failures": [] if attributed else [{"category": category}],
        }

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        cross_battery_then_behavior_failure,
    )
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        jobs=2,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert cache.info()["entries"] == expected_entries
    assert cache.get("unrelated-key") is not None
    derived_path = "tests/__generated__/math.derived.test.ts"
    example_path = "tests/__generated__/math.example.test.ts"
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    failed = outcomes[derived_path]
    partial = report.runner["partial_landing"]
    if category == "assertion":
        assert failed["state"] == "rejected"
        assert failed["cache_evicted"] is True
        assert failed["rejection_reasons"] == (
            "The compatible-subset Vitest run rejected this battery; "
            "its cached response was removed.",
        )
        assert partial["accepted"] == ()
        assert partial["rejected"] == (derived_path, example_path)
    else:
        assert failed["state"] == "staged"
        assert "cache_evicted" not in failed
        assert "rejection_reasons" not in failed
        assert partial["accepted"] == (derived_path,)
        assert partial["rejected"] == (example_path,)
    assert partial["retained"] == ()
    assert partial["committed"] is False
    payload_partial = typescript_test_payload(report)["vitest"]["partial_landing"]
    assert payload_partial["accepted"] == partial["accepted"]
    assert payload_partial["rejected"] == partial["rejected"]
    assert payload_partial["retained"] == partial["retained"]
    assert payload_partial["committed"] is False
    assert report.generated == frozenset()
    assert report.refrozen == frozenset()
    assert all(not (tmp_path / path).exists() for path in (derived_path, example_path))


@pytest.mark.asyncio
async def test_candidate_owned_collection_failure_is_evicted_and_regenerated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".collection-retry-cache")

    class CollectionThenValidGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            count = self.calls.get(request.target_path, 0) + 1
            self.calls[request.target_path] = count
            if request.target_path.endswith(".derived.test.ts") and count == 1:
                return (
                    'import { test } from "vitest";\ntest.todo("missing generated body");\n',
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    async def reject_todo(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        overlays = kwargs.get("overlays", {})
        derived_path = "tests/__generated__/math.derived.test.ts"
        if isinstance(overlays, Mapping) and "test.todo" in str(overlays.get(derived_path, "")):
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        # The protected runner discloses no file/status for a
                        # derived collection error. Its opaque case ID is the
                        # reporter's path digest and can be mapped only against
                        # the candidates Jaunt just requested.
                        "caseId": _digest(derived_path)[7:23],
                        "category": "collection",
                    }
                ],
                "diagnostics": [],
            }
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    generator = CollectionThenValidGenerator()
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", reject_todo)
    first = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    derived_path = "tests/__generated__/math.derived.test.ts"
    outcomes = {item["path"]: item for item in first.runner["batteries"]}
    assert first.exit_code == 4
    assert outcomes[derived_path]["state"] == "rejected"
    assert outcomes[derived_path]["cache_evicted"] is True
    assert cache.info()["entries"] == 1
    assert not (tmp_path / derived_path).exists()

    second = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert second.exit_code == 0
    assert generator.calls[derived_path] == 2
    assert generator.calls["tests/__generated__/math.example.test.ts"] == 1
    assert (tmp_path / derived_path).is_file()
    assert (tmp_path / "tests/__generated__/math.example.test.ts").is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["runner", "runner-protocol", "timeout"])
async def test_runner_nonbehavioral_failures_skip_repair_and_keep_staged_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    cache = ResponseCache(tmp_path / ".test-response-cache")
    build_calls: list[dict[str, Any]] = []

    async def fake_build(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        build_calls.append(dict(kwargs))
        return TargetBuildReport(language="ts", skipped=frozenset({"ts:src/math"}))

    async def infrastructure_failure(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        if category in {"collection", "runner"}:
            return {
                "ok": False,
                "mode": "run",
                "tests": [
                    {
                        "file": "tests/__generated__/math.example.test.ts",
                        "tier": "example",
                        "status": "failed",
                        "caseId": "infrastructure",
                        "category": category,
                    }
                ],
            }
        return {
            "ok": False,
            "mode": "run",
            "tests": [],
            "failures": [{"category": category}],
            **({"timedOut": True} if category == "timeout" else {}),
        }

    monkeypatch.setattr("jaunt.typescript.tester.run_build", fake_build)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", infrastructure_failure)
    report = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert len(build_calls) == 1
    assert "repair" not in report.runner
    assert cache.info()["entries"] == 2
    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()


@pytest.mark.asyncio
async def test_failed_post_repair_rerun_rolls_back_implementation_and_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    implementation = tmp_path / "src/__generated__/math.ts"
    sidecar = tmp_path / "src/__generated__/math.jaunt.json"
    journal = tmp_path / "JAUNT_LOG"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("prior passing implementation\n", encoding="utf-8")
    sidecar.write_text('{"state":"built-before"}\n', encoding="utf-8")
    journal.write_text("prior journal\n", encoding="utf-8")
    before = {
        implementation: implementation.read_bytes(),
        sidecar: sidecar.read_bytes(),
        journal: journal.read_bytes(),
    }
    build_calls = 0

    async def superficially_green_repair(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        nonlocal build_calls
        build_calls += 1
        if build_calls == 1:
            return TargetBuildReport(language="ts", skipped=frozenset({"ts:src/math"}))
        assert not str(kwargs["response_cache"]._cache_dir).startswith(str(tmp_path))
        repair_root = Path(_args[0])
        (repair_root / "src/__generated__/math.ts").write_text(
            "still behaviorally wrong\n", encoding="utf-8"
        )
        (repair_root / "src/__generated__/math.jaunt.json").write_text(
            '{"state":"repaired"}\n', encoding="utf-8"
        )
        (repair_root / "JAUNT_LOG").write_text("prior journal\nrepair journal\n", encoding="utf-8")
        return TargetBuildReport(language="ts", generated=frozenset({"ts:src/math"}))

    async def always_failing_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": "tests/__generated__/math.example.test.ts",
                    "tier": "example",
                    "status": "failed",
                    "caseId": "still-failing",
                    "category": "assertion",
                }
            ],
        }

    cache = ResponseCache(tmp_path / ".test-response-cache")
    monkeypatch.setattr("jaunt.typescript.tester.run_build", superficially_green_repair)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", always_failing_batches)
    report = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        response_cache=cache,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert report.runner["repair"]["reran"] is True
    for path, content in before.items():
        assert path.read_bytes() == content
    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()
    assert cache.info()["entries"] == 2


@pytest.mark.asyncio
async def test_failed_repair_preserves_implementation_and_does_not_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)

    class RejectingRepairWorker(_TestSpecWorker):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            candidates = params.get("candidates")
            if (
                method == "validateOverlay"
                and isinstance(candidates, dict)
                and any("invalid_repair" in str(source) for source in candidates.values())
            ):
                self.requests.append((method, params))
                return {
                    **self._stamp(),
                    "valid": False,
                    "artifacts": [],
                    "diagnostics": [
                        {
                            "code": "TS2322",
                            "message": "repair candidate failed overlay validation",
                            "severity": "error",
                            "path": "src/__generated__/math.ts",
                        }
                    ],
                    "affectedProjects": ["tsconfig.json"],
                }
            return await super().request(method, params)

    class InvalidRepairGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if request.kind == "build":
                return (
                    "const invalid_repair: string = 42;\n",
                    TokenUsage(20, 10, "fake-ts", "fake"),
                    (),
                )
            return await super().generate_request(request, **kwargs)

    worker = RejectingRepairWorker(tmp_path)
    seeded = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    implementation = tmp_path / "src/__generated__/math.ts"
    known_good = implementation.read_bytes()

    run_count = 0

    async def failing_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal run_count
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        run_count += 1
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": "tests/__generated__/math.derived.test.ts",
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "2222222222222222",
                    "category": "runtime",
                    "durationMs": 1,
                }
            ],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", failing_batches)
    report = await run_test(
        tmp_path,
        config,
        generator=InvalidRepairGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert run_count == 1
    assert implementation.read_bytes() == known_good
    assert report.runner["repair"]["attempted"] is True
    assert report.runner["repair"]["ok"] is False
    assert report.runner["repair"]["reran"] is False
    assert report.runner["repair"]["build"]["cost"]["api_calls"] == 1
    assert report.runner["cost"]["api_calls"] == 3
    assert "ts:src/math" in report.failed
    assert "vitest" in report.failed


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("no_build", "no_run", "expected_build_calls", "expected_exit"),
    [(True, False, 0, 4), (False, True, 1, 0)],
)
async def test_test_modes_never_enter_implementation_repair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    no_build: bool,
    no_run: bool,
    expected_build_calls: int,
    expected_exit: int,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    build_calls: list[dict[str, Any]] = []
    runtime_calls = 0

    async def fake_build(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        build_calls.append(dict(kwargs))
        return TargetBuildReport(language="ts", metadata={"cost": _cost(prompt=1, completion=1)})

    async def batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
        runtime_calls += 1
        return {
            "ok": False,
            "mode": "run",
            "tests": [
                {
                    "file": "tests/__generated__/math.derived.test.ts",
                    "tier": "derived",
                    "status": "failed",
                    "caseId": "3333333333333333",
                    "category": "assertion",
                }
            ],
        }

    monkeypatch.setattr("jaunt.typescript.tester.run_build", fake_build)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=no_build,
        no_run=no_run,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == expected_exit
    assert len(build_calls) == expected_build_calls
    assert all("ephemeral_prompt" not in call for call in build_calls)
    assert "repair" not in report.runner
    if no_run:
        assert runtime_calls == 0
        assert build_calls[0]["validate_committed_batteries"] is False
    if no_build:
        outcomes = {item["path"]: item for item in report.runner["batteries"]}
        rejected = outcomes["tests/__generated__/math.derived.test.ts"]
        assert rejected["state"] == "rejected"
        assert rejected["cache_evicted"] is True
        assert rejected["rejection_reasons"] == (
            "The final protected Vitest run rejected this battery; "
            "its cached response was removed.",
        )
        assert outcomes["tests/__generated__/math.example.test.ts"]["state"] == "staged"
        assert ResponseCache(tmp_path / ".jaunt" / "cache").info()["entries"] == 1


@pytest.mark.asyncio
async def test_external_cost_tracker_keeps_build_and_test_phase_summaries_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    aggregate = CostTracker()
    report = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        cost_tracker=aggregate,
        worker_factory=lambda *_: worker,
        repo_map_enabled=False,
        auto_skills_enabled=False,
    )

    assert report.exit_code == 0
    assert report.runner["build"]["cost"]["api_calls"] == 1
    assert report.runner["test_cost"]["api_calls"] == 2
    assert report.runner["cost"]["api_calls"] == 3
    assert aggregate.api_calls == 3


@pytest.mark.asyncio
async def test_test_default_cost_budget_is_command_wide_across_build_and_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    config = replace(config, llm=replace(config.llm, max_cost_per_build=0.00025))
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class PricedGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            source, _usage, advisories = await super().generate_request(request, **kwargs)
            return source, TokenUsage(20, 10, "gpt-5.6-sol", "codex"), advisories

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)

    with pytest.raises(JauntBudgetExceededError, match="exceeds budget"):
        await run_test(
            tmp_path,
            config,
            generator=PricedGenerator(),
            worker_factory=lambda *_: worker,
            jobs=1,
            repo_map_enabled=False,
            auto_skills_enabled=False,
        )


@pytest.mark.asyncio
async def test_test_without_specs_or_batteries_is_green_and_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)

    async def unexpected_runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("empty test selection must not start Vitest")

    def unexpected_generator() -> GeneratorBackend:
        raise AssertionError("empty test selection must not construct a generator")

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", unexpected_runner)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator_factory=unexpected_generator,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.runner["skipped"] is True


@pytest.mark.asyncio
async def test_tests_batch_by_owner_with_custom_generated_dir_and_atomic_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _config(tmp_path)
    for package in ("a", "b"):
        (tmp_path / f"packages/{package}/src").mkdir(parents=True)
        (tmp_path / f"packages/{package}/tests").mkdir(parents=True)
        (tmp_path / f"packages/{package}/tsconfig.test.json").write_text("{}\n")
    (tmp_path / "jaunt.toml").write_text(
        """version = 2
[target.ts]
source_roots = ["src", "packages/*/src"]
test_roots = ["packages/*/tests"]
projects = ["tsconfig.json"]
test_projects = ["packages/*/tsconfig.test.json"]
generated_dir = "machine"
[codex]
model = "gpt-5.6-sol"
"""
    )
    config = load_config(root=tmp_path)
    a_spec = "packages/a/tests/a.jaunt-test.ts"
    b_spec = "packages/b/tests/b.jaunt-test.ts"
    (tmp_path / a_spec).write_text("// authored test A\n")
    (tmp_path / b_spec).write_text("// authored test B\n")
    contract_source = tmp_path / "packages/a/src/util.ts"
    contract_source.write_text("export function value(): number { return 1; }\n")
    contract_battery = _battery_path(tmp_path, config, contract_source, "value")
    contract_battery.parent.mkdir(parents=True)
    contract_battery.write_text('import { test } from "vitest";\ntest("value", () => {});\n')

    class MultiProjectWorker(FakeWorker):
        async def initialize(self, _params: InitializeParams) -> InitializeResult:
            initialized = await super().initialize(_params)
            return replace(
                initialized,
                capabilities=(
                    *initialized.capabilities,
                    "scoped-diagnostics",
                    "scoped-analysis",
                ),
            )

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "analyzeWorkspace":
                self.requests.append((method, params))
                return {
                    **self._stamp(),
                    "projects": [
                        {
                            "id": "packages/a/tsconfig.test.json",
                            "role": "test",
                            "rootFiles": [contract_battery.relative_to(tmp_path).as_posix()],
                        },
                        {
                            "id": "packages/b/tsconfig.test.json",
                            "role": "test",
                            "rootFiles": [],
                        },
                    ],
                    "routes": [],
                    "specs": [],
                    "testSpecs": [
                        {
                            "path": a_spec,
                            "project": "packages/a/tsconfig.test.json",
                            "targets": ["ts:src/math#double"],
                        },
                        {
                            "path": b_spec,
                            "project": "packages/b/tsconfig.test.json",
                            "targets": ["ts:packages/b/src/triple#triple"],
                        },
                    ],
                    "contracts": [
                        {
                            "path": "packages/a/src/util.ts",
                            "project": "tsconfig.json",
                            "symbols": ["value"],
                        }
                    ],
                    "diagnostics": [],
                }
            if method == "analyzeContracts":
                self.requests.append((method, params))
                triple = {
                    **self.module,
                    "moduleId": "ts:packages/b/src/triple",
                    "specPath": "packages/b/src/triple.jaunt.ts",
                    "facadePath": "packages/b/src/triple.ts",
                    "apiMirrorPath": "packages/b/src/machine/triple.api.ts",
                    "implementationPath": "packages/b/src/machine/triple.ts",
                    "sidecarPath": "packages/b/src/machine/triple.jaunt.json",
                    "symbols": [{"name": "triple", "kind": "function"}],
                }
                modules = [self.module, triple]
                selected = {
                    str(item).split("#", 1)[0]
                    for item in params.get("moduleIds", [])
                    if isinstance(item, str)
                }
                return {
                    **self._stamp(),
                    "modules": [
                        module
                        for module in modules
                        if not selected or str(module["moduleId"]) in selected
                    ],
                }
            return await super().request(method, params)

    worker = MultiProjectWorker(tmp_path)
    a_output = tmp_path / "packages/a/tests/machine/a.example.test.ts"
    b_output = tmp_path / "packages/b/tests/machine/b.example.test.ts"
    calls: list[tuple[bool, str | None, tuple[str, ...], bool, tuple[str, ...], str | None]] = []

    async def green_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            (
                bool(kwargs.get("typecheck_only")),
                kwargs.get("tsconfig_path"),
                tuple(kwargs.get("files", ())),
                a_output.exists() or b_output.exists(),
                tuple(kwargs.get("project_config_paths", ())),
                kwargs.get("tier"),
            )
        )
        return {"ok": True, "tests": [], "diagnostics": [], "captured": {}}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", green_runner)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert a_output.is_file() and b_output.is_file()
    candidate_calls = calls[:4]
    assert all(call[0] for call in candidate_calls)
    candidate_owners = [call[1] for call in candidate_calls]
    assert candidate_owners.count("packages/a/tsconfig.test.json") == 2
    assert candidate_owners.count("packages/b/tsconfig.test.json") == 2
    assert all(len(call[2]) == 1 for call in candidate_calls)
    preflight_calls = calls[4:6]
    assert [call[1] for call in preflight_calls] == [
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    ]
    assert all(call[0] for call in preflight_calls)
    runner_calls = calls[6:]
    assert {call[1] for call in runner_calls} == {
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    }
    assert {call[5] for call in runner_calls} == {"example", "derived"}
    assert all(
        call[4]
        == (
            "packages/a/tsconfig.test.json",
            "packages/b/tsconfig.test.json",
        )
        for call in calls
    )
    assert all(not call[3] for call in calls)
    assert contract_battery.relative_to(tmp_path).as_posix() in preflight_calls[0][2]
    assert set(report.runner["batches"]) == {
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    }

    calls.clear()
    worker.requests.clear()
    targeted = await run_test(
        tmp_path,
        config,
        target_ids=("ts:src/math",),
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert targeted.exit_code == 0
    assert calls
    assert {call[1] for call in calls} == {"packages/a/tsconfig.test.json"}
    assert all(b_output.relative_to(tmp_path).as_posix() not in call[2] for call in calls)
    assert all(contract_battery.relative_to(tmp_path).as_posix() not in call[2] for call in calls)
    assert ("analyzeWorkspace", {"moduleIds": ["ts:src/math"]}) in worker.requests
    assert ("analyzeContracts", {"moduleIds": ["ts:src/math"]}) in worker.requests


@pytest.mark.asyncio
async def test_deleted_test_and_contract_sources_leave_blocking_cleanable_orphans(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    generated = tmp_path / "tests/__generated__/removed.example.test.ts"
    generated.parent.mkdir(parents=True)
    generated.write_text(
        _with_test_header(
            'import { test } from "vitest";\ntest("old", () => {});\n',
            tier="example",
            source_path="tests/removed.jaunt-test.ts",
        )
    )
    contract = tmp_path / "tests/contract/src/removed.gone.contract.test.ts"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        _with_header(
            'import { test } from "vitest";\ntest("old", () => {});\n',
            "src/removed.ts",
            "sha256:gone",
        )
    )
    legacy_generated = tmp_path / "tests/__generated__/legacy.example.test.ts"
    legacy_generated.write_text(
        "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.\n"
        "// jaunt:tier=example\nexport {};\n"
    )
    legacy_contract = tmp_path / "tests/contract/legacy.contract.test.ts"
    legacy_contract.parent.mkdir(parents=True, exist_ok=True)
    legacy_contract.write_text(
        "// ⚙️ jaunt:contract-battery — historical preview provenance.\nexport {};\n"
    )
    handwritten = tmp_path / "tests/__generated__/handwritten.test.ts"
    handwritten.write_text("// jaunt:source=looks-owned.ts\nexport {};\n")

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert {(item.path.relative_to(tmp_path).as_posix(), item.kind) for item in status.orphans} == {
        ("tests/__generated__/legacy.example.test.ts", "generated-test"),
        ("tests/__generated__/removed.example.test.ts", "generated-test"),
        ("tests/contract/legacy.contract.test.ts", "contract-battery"),
        ("tests/contract/src/removed.gone.contract.test.ts", "contract-battery"),
    }
    assert status_payload(status)["orphans"] == [
        "tests/__generated__/legacy.example.test.ts",
        "tests/__generated__/removed.example.test.ts",
        "tests/contract/legacy.contract.test.ts",
        "tests/contract/src/removed.gone.contract.test.ts",
    ]

    magic = await run_check(
        tmp_path,
        config,
        magic_only=True,
        worker_factory=lambda *_: worker,
    )
    contracts = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )
    assert magic.exit_code == 4
    assert [item.kind for item in magic.orphans] == ["generated-test", "generated-test"]
    assert contracts.exit_code == 4
    assert [item.kind for item in contracts.orphans] == [
        "contract-battery",
        "contract-battery",
    ]

    preview = await run_clean(
        tmp_path,
        config,
        orphans_only=True,
        dry_run=True,
        worker_factory=lambda *_: worker,
    )
    assert preview.would_remove == (
        "tests/__generated__/legacy.example.test.ts",
        "tests/__generated__/removed.example.test.ts",
        "tests/contract/legacy.contract.test.ts",
        "tests/contract/src/removed.gone.contract.test.ts",
    )
    cleaned = await run_clean(
        tmp_path,
        config,
        orphans_only=True,
        worker_factory=lambda *_: worker,
    )
    assert cleaned.removed == preview.would_remove
    assert not generated.exists()
    assert not contract.exists()
    assert not legacy_generated.exists()
    assert not legacy_contract.exists()
    assert handwritten.exists()


@pytest.mark.asyncio
async def test_plain_clean_removes_current_magic_batteries_but_preserves_contracts_and_sources(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    built = await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert built.exit_code == 0
    batteries = []
    for tier in ("example", "derived"):
        battery = tmp_path / f"tests/__generated__/math.{tier}.test.ts"
        battery.parent.mkdir(parents=True, exist_ok=True)
        battery.write_text(
            _with_test_header(
                'import { test } from "vitest";\ntest("owned", () => {});\n',
                tier=tier,
                source_path=worker.test_spec_path,
            ),
            encoding="utf-8",
        )
        batteries.append(battery)
    contract = tmp_path / "tests/contract/keep.contract.test.ts"
    contract.parent.mkdir(parents=True)
    contract.write_text(
        _with_header(
            'import { test } from "vitest";\ntest("contract", () => {});\n',
            "src/keep.ts",
            "sha256:keep",
        ),
        encoding="utf-8",
    )
    facade = tmp_path / "src/math.ts"
    spec = tmp_path / "src/math.jaunt.ts"
    test_spec = tmp_path / worker.test_spec_path

    cleaned = await run_clean(tmp_path, config, worker_factory=lambda *_: worker)

    assert cleaned.removed == (
        "src/__generated__/math.api.ts",
        "src/__generated__/math.jaunt.json",
        "src/__generated__/math.ts",
        "tests/__generated__/math.derived.test.ts",
        "tests/__generated__/math.example.test.ts",
    )
    assert all(not battery.exists() for battery in batteries)
    assert contract.is_file()
    assert facade.is_file()
    assert spec.is_file()
    assert test_spec.is_file()


@pytest.mark.asyncio
async def test_targeted_clean_scopes_artifacts_and_magic_batteries_to_requested_module(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)

    class TwoModuleCleanWorker(FakeWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            triple_source = (
                'import * as jaunt from "@usejaunt/ts/spec";\n'
                "jaunt.magicModule();\n"
                "/** Triple a number. */\n"
                "export function triple(value: number): number { return jaunt.magic(); }\n"
            )
            (root / "src/triple.jaunt.ts").write_text(triple_source, encoding="utf-8")
            self.triple = {
                **self.module,
                "moduleId": "ts:src/triple",
                "specPath": "src/triple.jaunt.ts",
                "facadePath": "src/triple.ts",
                "apiMirrorPath": "src/__generated__/triple.api.ts",
                "implementationPath": "src/__generated__/triple.ts",
                "sidecarPath": "src/__generated__/triple.jaunt.json",
                "symbols": [{"name": "triple", "kind": "function"}],
                "specSource": triple_source,
            }

        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "analyzeWorkspace":
                result["testSpecs"] = [
                    {
                        "path": "tests/math.jaunt-test.ts",
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/math#double"],
                    },
                    {
                        "path": "tests/triple.jaunt-test.ts",
                        "project": "tsconfig.test.json",
                        "targets": ["ts:src/triple#triple"],
                    },
                ]
            elif method == "analyzeContracts":
                result["modules"] = [self.module, self.triple]
            return result

    worker = TwoModuleCleanWorker(tmp_path)
    generated = tmp_path / "src/__generated__"
    generated.mkdir()
    for stem in ("math", "triple"):
        (tmp_path / f"src/{stem}.ts").write_text(
            f'export * from "./__generated__/{stem}.js";\n', encoding="utf-8"
        )
        (generated / f"{stem}.api.ts").write_text("export {};\n", encoding="utf-8")
        (generated / f"{stem}.ts").write_text(
            f"// jaunt:module=ts:src/{stem}\nexport {{}};\n", encoding="utf-8"
        )
        (generated / f"{stem}.jaunt.json").write_text("{}\n", encoding="utf-8")
        (tmp_path / f"tests/{stem}.jaunt-test.ts").write_text("// authored\n", encoding="utf-8")
        for tier in ("example", "derived"):
            battery = tmp_path / f"tests/__generated__/{stem}.{tier}.test.ts"
            battery.parent.mkdir(parents=True, exist_ok=True)
            battery.write_text(
                _with_test_header(
                    "export {};\n",
                    tier=tier,
                    source_path=f"tests/{stem}.jaunt-test.ts",
                ),
                encoding="utf-8",
            )

    cleaned = await run_clean(
        tmp_path,
        config,
        target_ids=("ts:src/math",),
        worker_factory=lambda *_: worker,
    )

    assert cleaned.removed == (
        "src/__generated__/math.api.ts",
        "src/__generated__/math.jaunt.json",
        "src/__generated__/math.ts",
        "tests/__generated__/math.derived.test.ts",
        "tests/__generated__/math.example.test.ts",
    )
    assert (tmp_path / "src/math.ts").is_file()
    assert (tmp_path / "src/math.jaunt.ts").is_file()
    for suffix in ("api.ts", "jaunt.json", "ts"):
        assert (generated / f"triple.{suffix}").is_file()
    for tier in ("example", "derived"):
        assert (tmp_path / f"tests/__generated__/triple.{tier}.test.ts").is_file()


@pytest.mark.asyncio
async def test_imported_type_context_drift_regenerates_batteries_without_api_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")
    original_api_digest = worker.module["apiDigest"]
    original_api_source = worker.module["apiSource"]

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    battery_paths = frozenset(
        {
            "tests/__generated__/math.example.test.ts",
            "tests/__generated__/math.derived.test.ts",
        }
    )
    assert seeded.exit_code == 0
    assert seeded.generated == battery_paths
    before = {
        path: dict(_test_header_metadata((tmp_path / path).read_text(encoding="utf-8")) or {})
        for path in battery_paths
    }
    assert all(metadata.get("imported_type_context_fingerprint") for metadata in before.values())

    stable = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert stable.generated == frozenset()
    assert stable.refrozen == frozenset()
    assert stable.skipped == battery_paths

    worker.module["contextSource"] = _test_imported_type_context_source(
        "id: string; required_label: string;"
    )
    assert worker.module["apiDigest"] == original_api_digest
    assert worker.module["apiSource"] == original_api_source
    stale = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    stale_batteries = [
        diagnostic for diagnostic in stale.diagnostics if diagnostic.path in battery_paths
    ]
    assert len(stale_batteries) == 2
    assert all(diagnostic.code == "JAUNT_TS_TEST_BATTERY_STALE" for diagnostic in stale_batteries)
    assert all(
        set(diagnostic.data.get("mismatches", ()))
        == {"battery_fingerprint", "imported_type_context_fingerprint"}
        for diagnostic in stale_batteries
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.targets: set[str] = set()

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.targets.add(request.target_path)
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    regenerated = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".legacy-context-cache"),
        worker_factory=lambda *_: worker,
    )
    assert regenerated.exit_code == 0
    assert generator.targets == battery_paths
    assert regenerated.generated == battery_paths
    assert regenerated.refrozen == frozenset()
    after = {
        path: dict(_test_header_metadata((tmp_path / path).read_text(encoding="utf-8")) or {})
        for path in battery_paths
    }
    for path in battery_paths:
        assert after[path]["target_api_digest"] == before[path]["target_api_digest"]
        assert (
            after[path]["imported_type_context_fingerprint"]
            != before[path]["imported_type_context_fingerprint"]
        )
        assert after[path]["battery_fingerprint"] != before[path]["battery_fingerprint"]

    fresh = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert not [diagnostic for diagnostic in fresh.diagnostics if diagnostic.path in battery_paths]


@pytest.mark.asyncio
async def test_legacy_battery_missing_imported_context_fingerprint_without_run_regenerates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    battery_paths = frozenset(seeded.generated)
    for relative in battery_paths:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        metadata = dict(_test_header_metadata(source) or {})
        tier = metadata["tier"]
        source_path = metadata["source"]
        legacy_values = {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "tier",
                "source",
                "body_digest",
                "battery_fingerprint",
                "imported_type_context_fingerprint",
            }
        }
        legacy_values["battery_fingerprint"] = _canonical_digest({"tier": tier, **legacy_values})
        path.write_text(
            _with_test_header(
                _strip_test_header(source),
                tier=tier,
                source_path=source_path,
                provenance=legacy_values,
            ),
            encoding="utf-8",
        )

    stale = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    diagnostics = [item for item in stale.diagnostics if item.path in battery_paths]
    assert len(diagnostics) == 2
    assert all(
        set(item.data.get("mismatches", ()))
        == {"battery_fingerprint", "imported_type_context_fingerprint"}
        for item in diagnostics
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.targets: set[str] = set()

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.targets.add(request.target_path)
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    regenerated = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".legacy-missing-context-cache"),
        worker_factory=lambda *_: worker,
    )
    assert regenerated.exit_code == 0
    assert regenerated.generated == battery_paths
    assert not regenerated.refrozen
    assert generator.targets == battery_paths


@pytest.mark.asyncio
async def test_legacy_battery_missing_imported_context_verifies_codrift_and_refreezes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    battery_paths = frozenset(seeded.generated)
    bodies: dict[str, str] = {}
    for relative in battery_paths:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        bodies[relative] = _strip_test_header(source)
        metadata = dict(_test_header_metadata(source) or {})
        legacy_values = {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "tier",
                "source",
                "body_digest",
                "battery_fingerprint",
                "fixture_fingerprint",
                "imported_type_context_fingerprint",
            }
        }
        legacy_values["battery_fingerprint"] = _canonical_digest(
            {"tier": metadata["tier"], **legacy_values}
        )
        path.write_text(
            _with_test_header(
                bodies[relative],
                tier=metadata["tier"],
                source_path=metadata["source"],
                provenance=legacy_values,
            ),
            encoding="utf-8",
        )

    worker.module["apiDigest"] = "sha256:legacy-context-api-v2"
    worker.module["apiSource"] = (
        "export declare function double(value: number, label?: string): number;\n"
    )
    worker.api = str(worker.module["apiSource"])
    expected_sidecar = json.loads(str(worker.module["sidecar"]))
    expected_sidecar["apiDigest"] = worker.module["apiDigest"]
    worker.module["sidecar"] = json.dumps(expected_sidecar, sort_keys=True) + "\n"
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert ts_tester._current_target_artifact_ids(tmp_path, (worker.module,)) == frozenset(
        {"ts:src/math"}
    )
    (tmp_path / "node_modules/vitest/package.json").write_text(
        '{"name":"vitest","version":"4.2.0"}\n', encoding="utf-8"
    )
    runner = worker.installation.package_root / "dist/test/runner.js"
    runner.write_text("export const legacyContextRuntime = 2;\n", encoding="utf-8")
    from jaunt.typescript import tester as tester_module

    original_prompt_text = tester_module._prompt_text

    def upgraded_prompt(path: str, default_name: str) -> str:
        return original_prompt_text(path, default_name) + "\nLegacy context prompt revision.\n"

    monkeypatch.setattr(tester_module, "_prompt_text", upgraded_prompt)
    verification_calls: list[tuple[bool, frozenset[str]]] = []

    async def verified_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        verification_calls.append(
            (
                bool(kwargs.get("typecheck_only")),
                frozenset(str(path) for path in kwargs.get("files", ())),
            )
        )
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", verified_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == battery_paths
    assert not report.generated
    assert verification_calls == [
        (True, battery_paths),
        (False, battery_paths),
        (True, battery_paths),
        (False, battery_paths),
    ]
    for relative in battery_paths:
        source = (tmp_path / relative).read_text(encoding="utf-8")
        metadata = dict(_test_header_metadata(source) or {})
        assert _strip_test_header(source) == bodies[relative]
        assert metadata.get("imported_type_context_fingerprint")
        assert metadata["fixture_fingerprint"] == _canonical_digest(None)

    verification_calls.clear()
    fresh = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert fresh.skipped == battery_paths
    assert not fresh.refrozen
    assert not fresh.generated
    assert verification_calls == [(True, battery_paths), (False, battery_paths)]


@pytest.mark.asyncio
async def test_legacy_imported_context_upgrade_requires_current_target_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    for relative in seeded.generated:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        metadata = dict(_test_header_metadata(source) or {})
        legacy_values = {
            key: value
            for key, value in metadata.items()
            if key
            not in {
                "tier",
                "source",
                "body_digest",
                "battery_fingerprint",
                "fixture_fingerprint",
                "imported_type_context_fingerprint",
            }
        }
        legacy_values["battery_fingerprint"] = _canonical_digest(
            {"tier": metadata["tier"], **legacy_values}
        )
        path.write_text(
            _with_test_header(
                _strip_test_header(source),
                tier=metadata["tier"],
                source_path=metadata["source"],
                provenance=legacy_values,
            ),
            encoding="utf-8",
        )

    # Analysis now advertises a newer API, but --no-build still exposes the
    # previous implementation and API mirror on disk.
    worker.module["apiDigest"] = "sha256:unbuilt-api-v2"
    worker.module["apiSource"] = (
        "export declare function double(value: number, label?: string): number;\n"
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.targets: set[str] = set()

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.targets.add(request.target_path)
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".stale-target-context-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.generated == seeded.generated
    assert not report.refrozen
    assert generator.targets == seeded.generated


@pytest.mark.asyncio
async def test_current_target_artifacts_require_hashes_and_no_pending_transaction(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    expected = frozenset({"ts:src/math"})
    assert ts_tester._current_target_artifact_ids(tmp_path, (worker.module,)) == expected

    implementation = tmp_path / str(worker.module["implementationPath"])
    original = implementation.read_bytes()
    implementation.write_bytes(original + b"// replaced outside Jaunt\n")
    assert not ts_tester._current_target_artifact_ids(tmp_path, (worker.module,))
    implementation.write_bytes(original)
    assert ts_tester._current_target_artifact_ids(tmp_path, (worker.module,)) == expected

    transaction_directory = tmp_path / ".jaunt/transactions"
    transaction_directory.mkdir(parents=True, exist_ok=True)
    marker = transaction_directory / "ts-pending.json"
    marker.write_text("{}\n", encoding="utf-8")
    assert not ts_tester._current_target_artifact_ids(tmp_path, (worker.module,))
    with pytest.raises(JauntGenerationError, match="unresolved TypeScript artifact transaction"):
        atomic_write_manifest(
            tmp_path,
            (
                _Write(
                    path="src/__generated__/probe.ts",
                    content="export {};\n",
                    kind="test",
                    module_id="ts-test:probe",
                ),
            ),
        )
    marker.unlink()
    assert ts_tester._current_target_artifact_ids(tmp_path, (worker.module,)) == expected


@pytest.mark.asyncio
async def test_removed_imported_context_regenerates_despite_tooling_codrift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    worker.module["contextSource"] = 'export const authored = "preserved";\n'
    (tmp_path / "node_modules/vitest/package.json").write_text(
        '{"name":"vitest","version":"4.2.0"}\n', encoding="utf-8"
    )
    (worker.installation.package_root / "dist/test/runner.js").write_text(
        "export const removedContextRuntime = 2;\n", encoding="utf-8"
    )

    stale = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    diagnostics = [item for item in stale.diagnostics if item.path in seeded.generated]
    assert len(diagnostics) == 2
    assert all(
        "imported_type_context_fingerprint" in item.data.get("mismatches", ())
        for item in diagnostics
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.targets: set[str] = set()

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.targets.add(request.target_path)
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".removed-context-cache"),
        worker_factory=lambda *_: worker,
    )
    assert report.generated == seeded.generated
    assert not report.refrozen
    assert generator.targets == seeded.generated


@pytest.mark.asyncio
async def test_immutable_toolchain_context_and_api_codrift_regenerates_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An adopter-shaped toolchain swap stays paid when declaration context changed."""

    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    battery_paths = frozenset(seeded.generated)

    worker.module["contextSource"] = _test_imported_type_context_source(
        "id: string; required_label: string;"
    )
    worker.module["apiDigest"] = "sha256:immutable-toolchain-api-v2"
    worker.module["apiSource"] = (
        "export declare function double(value: number, label?: string): number;\n"
    )
    runner = worker.installation.package_root / "dist/test/runner.js"
    runner.write_text("export const immutableRuntime = 2;\n", encoding="utf-8")
    from jaunt.typescript import tester as tester_module

    original_prompt_text = tester_module._prompt_text

    def upgraded_prompt(path: str, default_name: str) -> str:
        rendered = original_prompt_text(path, default_name)
        return rendered + "\nImmutable toolchain prompt revision.\n"

    monkeypatch.setattr(tester_module, "_prompt_text", upgraded_prompt)

    stale = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    battery_diagnostics = [
        diagnostic for diagnostic in stale.diagnostics if diagnostic.path in battery_paths
    ]
    assert len(battery_diagnostics) == 2
    mismatches = [set(diagnostic.data.get("mismatches", ())) for diagnostic in battery_diagnostics]
    assert all(
        fields
        == {
            "battery_fingerprint",
            "imported_type_context_fingerprint",
            "prompt_fingerprint",
            "runner_fingerprint",
            "target_api_digest",
        }
        for fields in mismatches
    ), mismatches

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.targets: set[str] = set()

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.targets.add(request.target_path)
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".immutable-toolchain-context-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.targets == battery_paths
    assert report.generated == battery_paths
    assert not report.refrozen


@pytest.mark.asyncio
async def test_test_battery_provenance_detects_semantic_and_body_drift_without_orphaning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    generated = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert generated.exit_code == 0
    example = tmp_path / "tests/__generated__/math.example.test.ts"
    derived = tmp_path / "tests/__generated__/math.derived.test.ts"
    for battery in (example, derived):
        text = battery.read_text()
        metadata = dict(_test_header_metadata(text) or {})
        assert "imported_type_context_fingerprint" not in metadata
        for field in (
            "test_spec_digest",
            "target_api_digest",
            "fixture_fingerprint",
            "vitest_fingerprint",
            "fast_check_fingerprint",
            "runner_fingerprint",
            "prompt_fingerprint",
            "policy_fingerprint",
            "battery_fingerprint",
            "body_digest",
        ):
            assert f"// jaunt:{field}=sha256:" in text

    fresh = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert not [item for item in fresh.diagnostics if "TEST_BATTERY" in item.code]

    example_source = example.read_text(encoding="utf-8")
    example.write_text(
        re.sub(r"(?m)^// jaunt:policy_fingerprint=.*\n", "", example_source),
        encoding="utf-8",
    )
    old_policy = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert any(
        item.code == "JAUNT_TS_TEST_BATTERY_STALE"
        and "policy_fingerprint" in item.data.get("mismatches", ())
        for item in old_policy.diagnostics
    )
    example.write_text(example_source, encoding="utf-8")

    spec = tmp_path / worker.test_spec_path
    spec.write_text("//   Verify   the public double function.   \n")
    formatting_only = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert not [item for item in formatting_only.diagnostics if "TEST_BATTERY" in item.code]

    spec.write_text("// Verify doubling rejects non-finite values.\n")
    stale = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    stale_diagnostics = [item for item in stale.diagnostics if "TEST_BATTERY" in item.code]
    assert [item.code for item in stale_diagnostics] == [
        "JAUNT_TS_TEST_BATTERY_STALE",
        "JAUNT_TS_TEST_BATTERY_STALE",
    ]
    assert not stale.orphans
    preview = await run_clean(
        tmp_path,
        config,
        orphans_only=True,
        dry_run=True,
        worker_factory=lambda *_: worker,
    )
    assert preview.would_remove == ()
    checked = await run_check(
        tmp_path,
        config,
        magic_only=True,
        worker_factory=lambda *_: worker,
    )
    assert checked.exit_code == 4
    assert [item.code for item in checked.diagnostics if "TEST_BATTERY" in item.code]
    assert check_payload(checked)["diagnostics"]

    derived.unlink()
    missing = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert any(item.code == "JAUNT_TS_TEST_BATTERY_MISSING" for item in missing.diagnostics)
    assert not missing.orphans


@pytest.mark.asyncio
async def test_magic_check_blocks_policy_aware_battery_typecheck_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green)
    generated = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert generated.exit_code == 0

    async def private_import(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        assert kwargs.get("typecheck_only") is True
        return {
            "ok": False,
            "mode": "typecheck",
            "diagnostics": [{"code": "JAUNT_TS_TEST_PRIVATE_IMPORT"}],
            "tests": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", private_import)
    checked = await run_check(
        tmp_path,
        config,
        magic_only=True,
        worker_factory=lambda *_: worker,
    )

    assert checked.exit_code == 4
    assert any(item.code == "JAUNT_TS_TEST_TYPECHECK" for item in checked.diagnostics)


@pytest.mark.asyncio
async def test_test_incrementality_skips_fresh_batteries_but_force_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    calls: list[tuple[bool, tuple[str, ...]]] = []

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(
            (
                bool(kwargs.get("typecheck_only")),
                tuple(sorted(kwargs.get("files", ()))),
            )
        )
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    first_generator = CountingGenerator()
    first = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=first_generator,
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(
        {
            "tests/__generated__/math.example.test.ts",
            "tests/__generated__/math.derived.test.ts",
        }
    )
    assert first.exit_code == 0
    assert first_generator.calls == 2
    assert first.generated == paths
    assert not first.skipped
    assert not first.refrozen

    calls.clear()
    fresh = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert fresh.exit_code == 0
    assert fresh.skipped == paths
    assert not fresh.generated
    assert not fresh.refrozen
    assert calls == [(True, tuple(sorted(paths))), (False, tuple(sorted(paths)))]

    forced_generator = CountingGenerator()
    forced = await run_test(
        tmp_path,
        config,
        no_build=True,
        force=True,
        generator=forced_generator,
        worker_factory=lambda *_: worker,
    )
    assert forced.exit_code == 0
    assert forced_generator.calls == 2
    assert forced.generated == paths
    assert not forced.skipped
    assert not forced.refrozen


@pytest.mark.asyncio
async def test_test_incrementality_refreezes_tooling_only_drift_before_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    before = {
        path: (
            _strip_test_header((tmp_path / path).read_text()),
            dict(_test_header_metadata((tmp_path / path).read_text()) or {}),
        )
        for path in paths
    }

    (tmp_path / "node_modules/vitest/package.json").write_text(
        '{"name":"vitest","version":"4.2.0"}\n'
    )
    runner = worker.installation.package_root / "dist/test/runner.js"
    runner.write_text("export const changedRunner = true;\n")
    phases: list[tuple[bool, bool, bool]] = []

    async def observe_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        example = "tests/__generated__/math.example.test.ts"
        overlays = kwargs.get("overlays", {})
        current = dict(_test_header_metadata((tmp_path / example).read_text()) or {})
        phases.append(
            (
                bool(kwargs.get("typecheck_only")),
                example in overlays,
                current.get("runner_fingerprint") != before[example][1].get("runner_fingerprint"),
            )
        )
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", observe_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == paths
    assert not report.generated
    assert not report.skipped
    assert phases == [(True, True, False), (False, True, False)]
    for path in paths:
        source = (tmp_path / path).read_text()
        metadata = dict(_test_header_metadata(source) or {})
        assert _strip_test_header(source) == before[path][0]
        assert metadata["body_digest"] == before[path][1]["body_digest"]
        assert metadata["runner_fingerprint"] != before[path][1]["runner_fingerprint"]
        assert metadata["vitest_fingerprint"] != before[path][1]["vitest_fingerprint"]
        assert metadata["battery_fingerprint"] != before[path][1]["battery_fingerprint"]


@pytest.mark.asyncio
async def test_same_version_vitest_runtime_change_refreezes_battery_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    before = {
        path: dict(_test_header_metadata((tmp_path / path).read_text()) or {})
        for path in seeded.generated
    }

    (tmp_path / "node_modules/vitest/dist/index.js").write_text(
        "export const packageVersion = 'same-version-rebuilt';\n",
        encoding="utf-8",
    )
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == seeded.generated
    for path in report.refrozen:
        after = dict(_test_header_metadata((tmp_path / path).read_text()) or {})
        assert after["vitest_fingerprint"] != before[path]["vitest_fingerprint"]


@pytest.mark.asyncio
async def test_vitest_symlink_aba_aborts_battery_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)
    owner_vitest = tmp_path / "node_modules/vitest"
    lexical = worker.installation.package_root / "node_modules/vitest"
    stores = tmp_path / ".package-store"
    first = stores / "vitest-a"
    second = stores / "vitest-b"
    first.parent.mkdir()
    shutil.copytree(owner_vitest, first, copy_function=shutil.copy2)
    (first / "dist/index.js").write_text(
        "export const packageVersion = 'runner-specific-same-version';\n",
        encoding="utf-8",
    )
    shutil.copytree(first, second, copy_function=shutil.copy2)
    lexical.parent.mkdir()
    lexical.symlink_to(first, target_is_directory=True)
    assert (owner_vitest / "dist/index.js").read_bytes() != (first / "dist/index.js").read_bytes()
    runner_vitest = _module_resolved_test_dependency(
        worker.installation.package_root / "dist/test/runner.js",
        "vitest",
    )
    assert runner_vitest is not None
    assert runner_vitest.resolve() == first.resolve()

    class RetargetingGenerator(FakeGenerator):
        changed = False

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if not self.changed:
                lexical.unlink()
                lexical.symlink_to(second, target_is_directory=True)
                self.changed = True
            return await super().generate_request(request, **kwargs)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=RetargetingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()


@pytest.mark.asyncio
async def test_vite_dependency_change_rolls_back_battery_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    vitest_manifest = tmp_path / "node_modules/vitest/package.json"
    vitest_payload = json.loads(vitest_manifest.read_text(encoding="utf-8"))
    vitest_payload["dependencies"] = {"vite": "^7.0.0"}
    vitest_manifest.write_text(json.dumps(vitest_payload), encoding="utf-8")
    vite_runtime = tmp_path / "node_modules/vite/dist/index.js"
    vite_runtime.parent.mkdir(parents=True)
    vite_runtime.write_text("export const viteRuntime = 1;\n", encoding="utf-8")
    (vite_runtime.parent.parent / "package.json").write_text(
        json.dumps(
            {
                "name": "vite",
                "version": "7.0.0",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )

    class ViteMutationWorker(_RuntimeMutationTestWorker):
        mutated = False

        def seal_runtime_identity(self) -> str:
            if not self.mutated:
                vite_runtime.write_text("export const viteRuntime = 2;\n", encoding="utf-8")
                self.mutated = True
            return super().seal_runtime_identity()

    worker = ViteMutationWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=FakeGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert worker.mutated
    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.asyncio
async def test_fixture_source_change_regenerates_batteries_instead_of_reheadering(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    fixture = tmp_path / "tests/fixtures.ts"
    fixture.write_text('export const clock = () => "v1";\n', encoding="utf-8")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    before = {
        path: dict(_test_header_metadata((tmp_path / path).read_text()) or {}) for path in paths
    }
    fixture.write_text('export const clock = () => "v2";\n', encoding="utf-8")

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls.append(str(request.cache_payload["tier"]))
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".fixture-change-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert sorted(generator.calls) == ["derived", "example"]
    assert report.generated == paths
    assert not report.refrozen
    for path in paths:
        metadata = dict(_test_header_metadata((tmp_path / path).read_text()) or {})
        assert metadata["fixture_fingerprint"] != before[path]["fixture_fingerprint"]


@pytest.mark.asyncio
async def test_asi_sensitive_fixture_whitespace_regenerates_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    fixture = tmp_path / "tests/fixtures.ts"
    fixture.write_text(
        "export function value() { return { value: 1 } }\n",
        encoding="utf-8",
    )

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    before = {
        path: dict(_test_header_metadata((tmp_path / path).read_text()) or {})
        for path in seeded.generated
    }
    fixture.write_text(
        "export function value() { return\n{ value: 1 } }\n",
        encoding="utf-8",
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".asi-fixture-cache"),
        worker_factory=lambda *_: worker,
    )

    assert generator.calls == 2
    assert report.generated == seeded.generated
    assert not report.refrozen
    assert all(
        dict(_test_header_metadata((tmp_path / path).read_text()) or {})["fixture_fingerprint"]
        != before[path]["fixture_fingerprint"]
        for path in seeded.generated
    )


@pytest.mark.asyncio
async def test_legacy_battery_without_an_empty_fixture_fingerprint_refreezes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    bodies: dict[str, str] = {}
    for relative in paths:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        bodies[relative] = _strip_test_header(source)
        source = re.sub(r"(?m)^// jaunt:fixture_fingerprint=.*\n", "", source)
        source = re.sub(
            r"(?m)^// jaunt:battery_fingerprint=.*$",
            "// jaunt:battery_fingerprint=sha256:" + ("0" * 64),
            source,
        )
        path.write_text(source, encoding="utf-8")

    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == paths
    assert not report.generated
    for relative in paths:
        source = (tmp_path / relative).read_text(encoding="utf-8")
        metadata = dict(_test_header_metadata(source) or {})
        assert _strip_test_header(source) == bodies[relative]
        assert metadata["fixture_fingerprint"] == _canonical_digest(None)
        assert metadata["battery_fingerprint"] != "sha256:" + ("0" * 64)


@pytest.mark.asyncio
async def test_legacy_empty_fixture_can_verify_with_api_and_tooling_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    old_api_digest = "sha256:" + ("1" * 64)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    bodies: dict[str, str] = {}
    for relative in paths:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        bodies[relative] = _strip_test_header(source)
        source = re.sub(r"(?m)^// jaunt:fixture_fingerprint=.*\n", "", source)
        for field, value in {
            "target_api_digest": old_api_digest,
            "prompt_fingerprint": "sha256:" + ("2" * 64),
            "runner_fingerprint": "sha256:" + ("3" * 64),
            "vitest_fingerprint": "sha256:" + ("4" * 64),
            "battery_fingerprint": "sha256:" + ("5" * 64),
        }.items():
            source = re.sub(
                rf"(?m)^// jaunt:{field}=.*$",
                f"// jaunt:{field}={value}",
                source,
            )
        path.write_text(source, encoding="utf-8")

    monkeypatch.setattr(
        "jaunt.typescript.tester.proven_previous_target_api_digests",
        lambda *_args, **_kwargs: frozenset({old_api_digest}),
    )
    original_action = ts_tester._existing_test_battery_action

    def require_verification(*args: Any, **kwargs: Any) -> tuple[str, str | None]:
        action = original_action(*args, **kwargs)
        assert action[0] == "verify", action[0]
        return action

    monkeypatch.setattr(ts_tester, "_existing_test_battery_action", require_verification)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        response_cache=ResponseCache(tmp_path / ".legacy-empty-api-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.refrozen == paths
    assert not report.generated
    for relative in paths:
        source = (tmp_path / relative).read_text(encoding="utf-8")
        metadata = dict(_test_header_metadata(source) or {})
        assert _strip_test_header(source) == bodies[relative]
        assert metadata["fixture_fingerprint"] == _canonical_digest(None)
        assert metadata["target_api_digest"] != old_api_digest


@pytest.mark.asyncio
async def test_legacy_battery_without_a_real_fixture_fingerprint_regenerates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    (tmp_path / "tests/fixtures.ts").write_text(
        'export const clock = () => "v1";\n', encoding="utf-8"
    )

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    for relative in seeded.generated:
        path = tmp_path / relative
        source = path.read_text(encoding="utf-8")
        source = re.sub(r"(?m)^// jaunt:fixture_fingerprint=.*\n", "", source)
        source = re.sub(
            r"(?m)^// jaunt:battery_fingerprint=.*$",
            "// jaunt:battery_fingerprint=sha256:" + ("0" * 64),
            source,
        )
        path.write_text(source, encoding="utf-8")

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".legacy-real-fixture-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.calls == 2
    assert report.generated == seeded.generated
    assert not report.refrozen


@pytest.mark.asyncio
async def test_fixture_source_is_an_exact_battery_commit_precondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    fixture = tmp_path / "tests/fixtures.ts"
    fixture.write_text('export const clock = () => "v1";\n', encoding="utf-8")

    class FixtureMutatingGenerator(FakeGenerator):
        changed = False

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if not self.changed:
                fixture.write_text('export const clock = () => "v2";\n', encoding="utf-8")
                self.changed = True
            return await super().generate_request(request, **kwargs)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(JauntGenerationError, match=r"inputs changed.*tests/fixtures\.ts"):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=FixtureMutatingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "tests/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/__generated__/math.derived.test.ts").exists()


@pytest.mark.asyncio
async def test_crlf_fixture_bytes_are_the_exact_commit_precondition(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    fixture = tmp_path / "tests/fixtures.ts"
    fixture.write_bytes(b'export const clock = () => "crlf";\r\n')
    observed_fixture_sources: list[str] = []

    class ObservingGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            observed_fixture_sources.append(request.context_files["_context/fixtures.ts"])
            return await super().generate_request(request, **kwargs)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ObservingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset(
        {
            "tests/__generated__/math.example.test.ts",
            "tests/__generated__/math.derived.test.ts",
        }
    )
    assert observed_fixture_sources == [
        'export const clock = () => "crlf";\r\n',
        'export const clock = () => "crlf";\r\n',
    ]


@pytest.mark.asyncio
async def test_vitest_config_closure_is_pinned_through_battery_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)
    assert config.typescript_target is not None
    config = replace(
        config,
        typescript_target=replace(
            config.typescript_target,
            vitest_config="config/vitest.config.ts",
        ),
    )
    vitest_config = tmp_path / "config/vitest.config.ts"
    setup = tmp_path / "tests/setup.ts"
    vitest_config.parent.mkdir()
    vitest_config.write_bytes(
        b'import "config/base.config";\r\n'
        b'export default { test: { setupFiles: ["tests/setup.ts"] } }\r\n'
    )
    base_config = tmp_path / "config/base.config.ts"
    base_config.write_bytes(b'import "./helpers";\r\nexport default {};\r\n')
    helper = tmp_path / "config/helpers/index.mts"
    helper.parent.mkdir()
    helper.write_bytes(b'export const helperVersion = "v1";\r\n')
    setup.write_bytes(b'export const setupVersion = "v1"\r\n')
    shadow = tmp_path / "config/tests/setup.ts"
    shadow.parent.mkdir()
    shadow.write_bytes(b'export const setupVersion = "shadow"\r\n')
    closure = ts_tester._local_config_closure(tmp_path, "config/vitest.config.ts")
    assert "tests/setup.ts" in closure
    assert "config/tests/setup.ts" not in closure
    assert "config/base.config.ts" in closure
    assert "config/helpers/index.mts" in closure
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    before = {relative: (tmp_path / relative).read_bytes() for relative in seeded.generated}
    (worker.installation.package_root / "dist/test/runner.js").write_text(
        "export const configMutationRuntime = 2;\n",
        encoding="utf-8",
    )
    mutated = False

    async def mutate_config(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal mutated
        if not mutated:
            setup.write_bytes(b'export const setupVersion = "v2"\r\n')
            mutated = True
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", mutate_config)
    with pytest.raises(
        JauntGenerationError,
        match=r"(?:inputs changed.*setup\.ts|Vitest configuration changed)",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert mutated
    assert {relative: (tmp_path / relative).read_bytes() for relative in seeded.generated} == before


def test_vitest_config_closure_rejects_escaping_local_dependency(tmp_path: Path) -> None:
    config = tmp_path / "vitest.config.ts"
    outside = tmp_path.parent / f"{tmp_path.name}-outside.ts"
    outside.write_text("export const outside = true;\n", encoding="utf-8")
    config.write_text(f'import "../{outside.name}";\nexport default {{}};\n', encoding="utf-8")

    with pytest.raises(JauntConfigError, match="outside the workspace"):
        ts_tester._local_config_closure(tmp_path, "vitest.config.ts")


def test_vitest_config_snapshot_preserves_absent_candidates_and_exact_bytes(
    tmp_path: Path,
) -> None:
    config_bytes = (
        b'import "./helper";\r\nexport default { test: { include: ["tests/**/*.test.ts"] } };\r\n'
    )
    (tmp_path / "vitest.config.ts").write_bytes(config_bytes)

    closure, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert closure["helper.ts"] == MISSING_INPUT
    assert "tests/**/*.test.ts" not in closure
    assert overlays["vitest.config.ts"].encode("utf-8") == config_bytes

    # This file appears after the snapshot. The disposable runner view must
    # still execute the captured absent-candidate world, not the transient one.
    (tmp_path / "helper.ts").write_text("throw new Error('transient');\n", encoding="utf-8")
    deleted = tuple(path for path, digest in closure.items() if digest == MISSING_INPUT)
    with _isolated_test_workspace(
        tmp_path,
        (),
        overlays,
        tier="derived",
        deleted_files=deleted,
    ) as isolated:
        assert (isolated / "vitest.config.ts").read_bytes() == config_bytes
        assert not (isolated / "helper.ts").exists()


def test_vitest_config_snapshot_reduces_static_node_path_helpers(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'import { join as pathJoin } from "node:path";\n'
        'export default { test: { setupFiles: [pathJoin("config", "setup.ts")] } };\n',
        encoding="utf-8",
    )
    setup = tmp_path / "config/setup.ts"
    setup.parent.mkdir()
    setup.write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "config/setup.ts" in closure
    assert "setup.ts" not in closure


@pytest.mark.parametrize(
    "binding, call",
    [
        ('import path, { resolve as pathResolve } from "node:path";', "pathResolve"),
        ('import {\n  join as pathJoin,\n} from "node:path";', "pathJoin"),
        ('const { join: pathJoin } = require("path");', "pathJoin"),
        ('const path = require("node:path");', "path.join"),
        ('const pathJoin = require("node:path").join;', "pathJoin"),
        ('import path = require("node:path");', "path.join"),
        ('import path from "node:path";', "path.posix.join"),
        ('import { posix as platformPath } from "node:path";', "platformPath.join"),
        ('const platformPath = require("node:path").win32;', "platformPath.join"),
    ],
)
def test_vitest_config_snapshot_reduces_common_node_path_import_forms(
    tmp_path: Path,
    binding: str,
    call: str,
) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        f"{binding}\n"
        f'export default {{ test: {{ setupFiles: [{call}("config", "setup.ts")] }} }};\n',
        encoding="utf-8",
    )
    setup = tmp_path / "config/setup.ts"
    setup.parent.mkdir()
    setup.write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "config/setup.ts" in closure
    assert "setup.ts" not in closure


def test_vitest_config_computed_paths_are_normalized_from_the_workspace_root(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config/vitest.config.ts"
    config.parent.mkdir()
    config.write_text(
        'import { join } from "node:path";\n'
        'export default { test: { setupFiles: [join("./configdir", "setup.ts")] } };\n',
        encoding="utf-8",
    )
    setup = tmp_path / "configdir/setup.ts"
    setup.parent.mkdir()
    setup.write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(
        tmp_path,
        "config/vitest.config.ts",
    )

    assert "configdir/setup.ts" in closure
    assert "config/configdir/setup.ts" not in closure


@pytest.mark.parametrize(
    ("expression", "config_path", "expected"),
    [
        ("resolve(__dirname, `setup.ts`)", "config/vitest.config.ts", "config/setup.ts"),
        (
            'resolve(import.meta.dirname, "setup.ts")',
            "config/vitest.config.ts",
            "config/setup.ts",
        ),
        ('resolve(process.cwd(), "setup.ts")', "config/vitest.config.ts", "setup.ts"),
        ('join("config", `setup.ts`)', "vitest.config.ts", "config/setup.ts"),
    ],
)
def test_vitest_config_snapshot_reduces_known_static_path_bases(
    tmp_path: Path,
    expression: str,
    config_path: str,
    expected: str,
) -> None:
    config = tmp_path / config_path
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        'import { join, resolve } from "node:path";\n'
        f"export default {{ test: {{ setupFiles: [{expression}] }} }};\n",
        encoding="utf-8",
    )
    setup = tmp_path / expected
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, config_path)

    assert expected in closure


def test_vitest_config_path_scanner_ignores_comments_and_string_bodies(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'import { join } from "node:path";\n'
        '// old: join("config", process.env.SETUP)\n'
        "const documentation = 'join(\"config\", process.env.SETUP)';\n"
        'export default { test: { setupFiles: [join("config", "setup.ts")] } };\n',
        encoding="utf-8",
    )
    setup = tmp_path / "config/setup.ts"
    setup.parent.mkdir()
    setup.write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "config/setup.ts" in closure


def test_vitest_config_snapshot_captures_static_template_literal_paths(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "export default { test: { setupFiles: [`./setup.ts`] } };\n",
        encoding="utf-8",
    )
    (tmp_path / "setup.ts").write_text("export const setup = true;\n", encoding="utf-8")

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "setup.ts" in closure


def test_vitest_config_snapshot_rejects_interpolated_template_paths(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "const name = 'setup';\nexport default { test: { setupFiles: [`./${name}.ts`] } };\n",
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="interpolated path"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


def test_vitest_config_snapshot_allows_non_path_interpolated_metadata(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "const mode = 'fast';\n"
        "export default { test: { name: `unit-${mode}-${process.env.MODE}` } };\n",
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert tuple(closure) == ("vitest.config.ts",)


@pytest.mark.parametrize(
    "expression",
    [
        '(await import("vite-plugin-example")).default',
        'readFileSync("./test-name.txt", "utf8")',
    ],
)
def test_vitest_config_snapshot_rejects_effectful_metadata_interpolation(
    tmp_path: Path,
    expression: str,
) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        f"export default {{ test: {{ name: `unit-${{{expression}}}` }} }};\n",
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="unresolved computed value"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


def test_vitest_config_snapshot_rejects_ambiguously_composed_interpolated_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "const setup = `${process.env.SETUP}`;\n"
        "export default { test: { setupFiles: [setup] } };\n",
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="unresolved computed value"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


def test_vitest_config_snapshot_rejects_concatenated_paths(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        "const name = 'setup';\n"
        "export default { test: { setupFiles: ['./config/' + name + '.ts'] } };\n",
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="concatenated path"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


def test_vitest_config_imported_packages_are_fingerprinted_and_pinned(tmp_path: Path) -> None:
    config = tmp_path / "vitest.config.ts"
    config.write_text(
        'import path from "node:path";\n'
        'import plugin from "vite-plugin-example";\n'
        'export * from "vite-plugin-example";\n'
        "void import(`vite-plugin-example`);\n"
        'import "./setup.ts";\n'
        "export default { plugins: [plugin()], root: path.resolve('.') };\n",
        encoding="utf-8",
    )
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    package = tmp_path / "node_modules/vite-plugin-example"
    runtime = package / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("export default () => ({});\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
                "dependencies": {"vite-plugin-dependency": "1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    dependency = tmp_path / "node_modules/vite-plugin-dependency"
    dependency.mkdir()
    dependency_runtime = dependency / "index.js"
    dependency_runtime.write_text("export const value = 1;\n", encoding="utf-8")
    (dependency / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-dependency",
                "version": "1.0.0",
                "type": "module",
                "main": "./index.js",
            }
        ),
        encoding="utf-8",
    )
    closure, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    before = ts_tester._config_package_runtime_identities(tmp_path, overlays)
    dependency_runtime.write_text("export const value = 2;\n", encoding="utf-8")
    after = ts_tester._config_package_runtime_identities(tmp_path, overlays)

    assert "setup.ts" in closure
    assert tuple(before) == (
        "vitest.config.ts:vite-plugin-example",
        "vitest.config.ts:vite-plugin-example>vite-plugin-dependency",
    )
    assert before != after

    calls: list[tuple[tuple[object, ...], Mapping[str, object]]] = []

    class Client:
        def pin_package_resolution_closure(self, *args: object, **kwargs: object) -> None:
            calls.append((args, kwargs))

    ts_tester._pin_vitest_config_dependency_runtimes(Client(), tmp_path, overlays)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[:3] == (
        "Vitest config dependency vite-plugin-example from vitest.config.ts",
        config,
        "vite-plugin-example",
    )
    assert kwargs == {
        "boundary": tmp_path,
        "module_path": True,
    }


def test_vitest_config_snapshot_captures_package_import_local_branches_and_manifest(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    for name in ("exact", "node", "default", "first", "fallback", "self"):
        (tmp_path / f"config/{name}.ts").write_text(
            f"export const value = {name!r};\n",
            encoding="utf-8",
        )
    manifest = {
        "name": "workspace",
        "exports": {"./self/*": "./config/*.ts"},
        "imports": {
            "#exact/*": "./config/*.ts",
            "#conditional": {
                "node": "./config/node.ts",
                "default": "./config/default.ts",
            },
            "#array": ["./config/first.ts", "./config/fallback.ts"],
            "#self/*": "workspace/self/*",
        },
    }
    manifest_path = tmp_path / "package.json"
    manifest_path.write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (tmp_path / "vitest.config.ts").write_text(
        'import "#exact/exact";\nimport "#conditional";\nimport "#array";\n'
        'import "#self/self";\nexport default {};\n',
        encoding="utf-8",
    )

    before, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")
    (tmp_path / "config/exact.ts").write_text(
        "export const value = 'changed';\n",
        encoding="utf-8",
    )
    after, _ = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")
    manifest["imports"]["#exact/*"] = "./config/fallback.ts"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    after_manifest, _ = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert set(overlays) == {
        "config/default.ts",
        "config/exact.ts",
        "config/fallback.ts",
        "config/first.ts",
        "config/node.ts",
        "config/self.ts",
        "vitest.config.ts",
    }
    assert "package.json" in before
    assert before != after
    assert after != after_manifest


def test_vitest_config_package_import_external_branches_are_fingerprinted_and_sealed(
    tmp_path: Path,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "name": "workspace",
                "imports": {
                    "#helper/*": {
                        "node": ["helper-a/*", "helper-b"],
                        "default": "helper-default",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "vitest.config.ts").write_text(
        'import "#helper/feature";\nexport default {};\n',
        encoding="utf-8",
    )
    helper_runtimes: dict[str, Path] = {}
    for package in ("helper-a", "helper-b", "helper-default"):
        package_root = tmp_path / "node_modules" / package
        package_root.mkdir(parents=True)
        runtime = package_root / "index.js"
        runtime.write_text("export const value = 1;\n", encoding="utf-8")
        (package_root / "package.json").write_text(
            json.dumps({"name": package, "version": "1.0.0", "main": "./index.js"}),
            encoding="utf-8",
        )
        helper_runtimes[package] = runtime
    _closure, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    before = ts_tester._config_package_runtime_identities(tmp_path, overlays)
    helper_runtimes["helper-a"].write_text("export const value = 2;\n", encoding="utf-8")
    after = ts_tester._config_package_runtime_identities(tmp_path, overlays)

    assert set(before) == {
        "vitest.config.ts:helper-a",
        "vitest.config.ts:helper-b",
        "vitest.config.ts:helper-default",
    }
    assert before != after

    (tmp_path / "src").mkdir()
    worker = _RuntimeMutationWorker(tmp_path)
    ts_tester._pin_vitest_config_dependency_runtimes(worker, tmp_path, overlays)
    helper_runtimes["helper-b"].write_text("export const value = 3;\n", encoding="utf-8")
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        worker.seal_runtime_identity()


@pytest.mark.parametrize(
    ("imports", "message"),
    [
        ({}, "unresolved alias"),
        ({"#helper": "./../outside.ts"}, "unsafe package-relative target"),
    ],
)
def test_vitest_config_snapshot_rejects_invalid_package_import_alias(
    tmp_path: Path,
    imports: Mapping[str, object],
    message: str,
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "workspace", "imports": imports}),
        encoding="utf-8",
    )
    (tmp_path / "vitest.config.ts").write_text(
        'import "#helper";\nexport default {};\n',
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match=message):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


@pytest.mark.parametrize(
    ("plugin_relative", "plugin_source"),
    [
        (
            "dist/index.cjs",
            'module.exports = require("hoisted-runtime-helper");\n',
        ),
        (
            "dist/index.mjs",
            'import { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            'export default load("hoisted-runtime-helper");\n',
        ),
    ],
)
def test_vitest_config_runtime_identity_tracks_undeclared_static_helper_bytes(
    tmp_path: Path,
    plugin_relative: str,
    plugin_source: str,
) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'import plugin from "vite-plugin-example";\nexport default { plugins: [plugin()] };\n',
        encoding="utf-8",
    )
    plugin = tmp_path / "node_modules/vite-plugin-example"
    plugin_runtime = plugin / plugin_relative
    plugin_runtime.parent.mkdir(parents=True)
    plugin_runtime.write_text(plugin_source, encoding="utf-8")
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "main": f"./{plugin_relative}",
            }
        ),
        encoding="utf-8",
    )
    helper = tmp_path / "node_modules/hoisted-runtime-helper"
    helper.mkdir()
    helper_runtime = helper / "index.js"
    helper_runtime.write_text("export const value = 1;\n", encoding="utf-8")
    (helper / "package.json").write_text(
        json.dumps(
            {
                "name": "hoisted-runtime-helper",
                "version": "1.0.0",
                "main": "./index.js",
            }
        ),
        encoding="utf-8",
    )
    _closure, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    before = ts_tester._config_package_runtime_identities(tmp_path, overlays)
    helper_runtime.write_text("export const value = 2;\n", encoding="utf-8")
    after = ts_tester._config_package_runtime_identities(tmp_path, overlays)

    assert "vitest.config.ts:vite-plugin-example>hoisted-runtime-helper" in before
    assert before != after


def test_vitest_config_runtime_identity_records_absent_undeclared_static_helper(
    tmp_path: Path,
) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'import "vite-plugin-example";\nexport default {};\n',
        encoding="utf-8",
    )
    plugin = tmp_path / "node_modules/vite-plugin-example"
    plugin_runtime = plugin / "dist/index.js"
    plugin_runtime.parent.mkdir(parents=True)
    plugin_runtime.write_text(
        'export const load = () => import("optional-hoisted-helper");\n',
        encoding="utf-8",
    )
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    _closure, overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    identities = ts_tester._config_package_runtime_identities(tmp_path, overlays)

    assert (
        identities["vitest.config.ts:vite-plugin-example>optional-hoisted-helper"] == MISSING_INPUT
    )


def test_vitest_config_package_scanner_excludes_type_only_imports() -> None:
    source = """
import type DefaultType from "type-default";
export type * from "type-export";
import { type First, type Second as Alias } from "type-named";
export { type Third } from "type-reexport";
import { type Fourth, runtimeValue } from "runtime-mixed";
export { type Fifth, runtimeValue as exposed } from "runtime-export";
import { type as runtimeNamedType } from "runtime-named-type";
"""

    assert ts_tester._config_package_imports(source) == (
        ("runtime-export", "runtime-export"),
        ("runtime-mixed", "runtime-mixed"),
        ("runtime-named-type", "runtime-named-type"),
    )


@pytest.mark.parametrize(
    "source",
    [
        'const packageName = "vite-plugin-example"; void import(packageName);',
        'const suffix = "example"; void import("vite-plugin-" + suffix);',
        'const packageName = "vite-plugin-example"; require(packageName);',
        'const suffix = "example"; require("vite-plugin-" + suffix);',
        'const packageName = "vite-plugin-example"; require.resolve(packageName);',
        'const packageName = "vite-plugin-example"; module.require(packageName);',
    ],
)
def test_vitest_config_package_scanner_rejects_computed_loads(source: str) -> None:
    with pytest.raises(JauntConfigError, match="computed import/require specifier"):
        ts_tester._config_package_imports(source)


def test_vitest_config_package_scanner_accepts_commented_literal_dynamic_import() -> None:
    assert ts_tester._config_package_imports(
        'void import(/* @vite-ignore */ "vite-plugin-example");'
    ) == (("vite-plugin-example", "vite-plugin-example"),)


@pytest.mark.parametrize(
    ("source", "specifier"),
    [
        ('const pattern = /"/; import plugin from "vite-plugin-example";', "vite-plugin-example"),
        ('import/* legal trivia */ plugin from "vite-plugin-example";', "vite-plugin-example"),
        ('const plugin = `${await import("vite-plugin-example")}`;', "vite-plugin-example"),
        ('const plugin = `${require("vite-plugin-example")}`;', "vite-plugin-example"),
        (
            'module.exports={test:{setupFiles:[require.resolve("vite-plugin-example/setup")]}};',
            "vite-plugin-example/setup",
        ),
        (
            'module/* trivia */.require/* trivia */("vite-plugin-example");',
            "vite-plugin-example",
        ),
    ],
)
def test_vitest_config_package_scanner_covers_executable_lexical_forms(
    source: str, specifier: str
) -> None:
    assert ts_tester._config_package_imports(source) == ((specifier, "vite-plugin-example"),)


def test_vitest_config_package_scanner_ignores_regex_and_template_text() -> None:
    source = r"""
const quoted = /import fake from "fake-package"/;
const called = /require\("other-package"\)/;
const inert = `import("template-package")`;
"""

    assert ts_tester._config_package_imports(source) == ()


@pytest.mark.parametrize(
    "prefix",
    [
        'const value = new /ignored import("missing-import") '
        'require("missing-require")/.constructor("actual");',
        'export default /ignored import("missing-import") require("missing-require")/;',
        'class Runner extends /ignored import("missing-import") '
        'require("missing-require")/.constructor {}',
        'const rendered = `${new /ignored import("missing-import") '
        'require("missing-require")/.constructor("actual")}`;',
    ],
)
def test_vitest_config_package_scanner_ignores_regex_after_expression_prefix_keyword(
    prefix: str,
) -> None:
    source = f'{prefix}\nimport("real-plugin");\n'

    assert ts_tester._config_package_imports(source) == (("real-plugin", "real-plugin"),)


@pytest.mark.parametrize("member", ["target.new", "target?.default", "target.extends"])
def test_vitest_config_package_scanner_keeps_division_after_keyword_named_member(
    member: str,
) -> None:
    source = f'const ratio = {member} / require("real-divisor") / divisor;\n'

    assert ts_tester._config_package_imports(source) == (("real-divisor", "real-divisor"),)


@pytest.mark.parametrize(
    "prefix",
    [
        'const value = new /ignored "ghost.ts"/.constructor;',
        'export default /ignored "ghost.ts"/;',
        'class Runner extends /ignored "ghost.ts"/.constructor {}',
    ],
)
def test_typescript_lexical_regions_ignore_strings_in_expression_prefix_regex(
    prefix: str,
) -> None:
    source = f'{prefix}\nconst actual = "actual";\n'

    _comments, strings = ts_tester._typescript_lexical_regions(source)

    assert tuple(source[start:end] for start, end, _quote in strings) == ('"actual"',)


@pytest.mark.parametrize(
    "control_head",
    [
        "if (ok)",
        "if ((ok && check()))",
        "while (ok)",
        "for (; ok; step())",
        "with (scope)",
    ],
)
def test_vitest_config_package_scanner_ignores_regex_after_control_flow_head(
    control_head: str,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        f'{control_head} /import("missing-import") require("missing-require") '
        'createRequire(import.meta.url)("missing-create-require")/.test(value);\n'
        'import("real-plugin");\n'
    )

    assert ts_tester._config_package_imports(source) == (("real-plugin", "real-plugin"),)


@pytest.mark.parametrize(
    "statement",
    [
        "{ run(); }",
        "label: { run(); }",
        "if (ok) { run(); }",
        "if (ok) { run(); } else { recover(); }",
        "switch (value) { default: run(); }",
        "try { run(); } catch { recover(); }",
        "try { run(); } catch (error) { recover(error); } finally { finish(); }",
        "do { run(); } while (again);",
        "function run() {}",
        "function run({ value } = {}) {}",
        "class Runner {}",
        "class Runner extends mixin({}) {}",
        "namespace Runtime {}",
        "enum Mode { Active }",
        "interface Options {}",
        "abstract class AbstractRunner {}",
        "declare class AmbientRunner {}",
        "class GenericRunner<T extends {}> {}",
        "function genericRun<T extends {}>() {}",
    ],
)
def test_vitest_config_package_scanner_ignores_regex_after_statement_brace(
    statement: str,
) -> None:
    source = (
        f'{statement} /import("missing-import") require("missing-require")/.test(value);\n'
        'import("real-plugin");\n'
    )

    assert ts_tester._config_package_imports(source) == (("real-plugin", "real-plugin"),)


def test_vitest_config_package_scanner_tracks_statement_brace_regex_inside_template() -> None:
    source = (
        'const rendered = `${(() => { if (ok) {} /} import("missing-template")/.test(value); '
        'return import("real-template"); })()}`;\n'
    )

    assert ts_tester._config_package_imports(source) == (("real-template", "real-template"),)


@pytest.mark.parametrize(
    "source",
    [
        (
            'function outer() { {} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        (
            'switch (value) { case 1: {} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        (
            'function stopped() { return\n{} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        'const value = 1\n{} /import("missing-import") require("missing-require")/.test(value);',
        'type Runtime = {}\n/import("missing-import") require("missing-require")/.test(value);',
    ],
)
def test_vitest_config_package_scanner_ignores_regex_after_nested_or_asi_block(
    source: str,
) -> None:
    source += '\nimport("real-plugin");\n'

    assert ts_tester._config_package_imports(source) == (("real-plugin", "real-plugin"),)


def test_vitest_config_package_scanner_tracks_nested_block_regex_inside_template() -> None:
    source = (
        'const rendered = `${(() => { {} /} import("missing-template")/.test(value); '
        'return import("real-template"); })()}`;\n'
    )

    assert ts_tester._config_package_imports(source) == (("real-template", "real-template"),)


@pytest.mark.parametrize("expression", ["{}", "function () {}", "class {}", "(() => {})"])
def test_vitest_config_package_scanner_keeps_division_after_braced_expression(
    expression: str,
) -> None:
    source = f'const ratio = {expression} / require("real-divisor");\n'

    assert ts_tester._config_package_imports(source) == (("real-divisor", "real-divisor"),)


@pytest.mark.parametrize(
    "source",
    [
        'const value = { nested: {} / require("real-divisor") };',
        'const value = class extends (class {}) {} / require("real-divisor");',
        '({ value } = {}) / require("real-divisor");',
        'function ratio() { return {} / require("real-divisor"); }',
    ],
)
def test_vitest_config_package_scanner_preserves_nested_braced_expression_division(
    source: str,
) -> None:
    assert ts_tester._config_package_imports(source) == (("real-divisor", "real-divisor"),)


def test_vitest_config_package_scanner_keeps_division_adjacent_loads_executable() -> None:
    source = (
        "const ratio = (total) / divisor;\n"
        'import("real-import");\n'
        'const adjusted = ratio / require("real-require");\n'
    )

    assert ts_tester._config_package_imports(source) == (
        ("real-import", "real-import"),
        ("real-require", "real-require"),
    )


def test_vitest_config_package_scanner_distinguishes_nested_control_head_division() -> None:
    source = 'if ((total) / require("real-divisor")) /import("missing-plugin")/.test(value);\n'

    assert ts_tester._config_package_imports(source) == (("real-divisor", "real-divisor"),)


def test_vitest_config_snapshot_ignores_regex_paths_after_control_flow_head(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        'if (ok) /require\\(".\\/setup.ts"\\)/.test(value);\nexport default {};\n',
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert tuple(closure) == ("vitest.config.ts",)


@pytest.mark.parametrize(
    "statement",
    [
        "{ run(); }",
        "label: { run(); }",
        "if (ok) { run(); }",
        "try { run(); } catch { recover(); }",
        "function run() {}",
        "class Runner {}",
    ],
)
def test_vitest_config_snapshot_ignores_regex_paths_after_statement_brace(
    tmp_path: Path,
    statement: str,
) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        f'{statement} /require\\(".\\/setup.ts"\\)/.test(value);\nexport default {{}};\n',
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert tuple(closure) == ("vitest.config.ts",)


def test_vitest_config_snapshot_ignores_path_shaped_metadata_values(tmp_path: Path) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        'import { join } from "node:path";\n'
        "export default {\n"
        '  define: { API_BASE: join("/", "api"), '
        'ASSET_PATHS: ["assets/v1", "assets/v2"], '
        'MANIFEST: "config.json", VERSIONED: `/api/${process.env.VERSION}` },\n'
        '  env: { SERVICE_PATH: join("/", "service/v1") },\n'
        '  test: { name: join("unit", "api"), setupFiles: ["./setup.ts"] },\n'
        "};\n",
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert tuple(closure) == ("setup.ts", "vitest.config.ts")


def test_vitest_config_metadata_does_not_hide_executable_local_loads(tmp_path: Path) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        'export default { define: { SETUP: require("./setup") } };\n',
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "setup.ts" in closure


def test_vitest_config_absolute_setup_path_still_fails_confinement(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'export default { test: { setupFiles: ["/outside.ts"] } };\n',
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="outside the workspace"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


@pytest.mark.parametrize(
    ("source", "specifier", "package"),
    [
        (
            'import { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            'load("vite-plugin-example");',
            "vite-plugin-example",
            "vite-plugin-example",
        ),
        (
            'import { createRequire as makeRequire } from "module";\n'
            "const load = makeRequire(import.meta.url);\n"
            'load.resolve("@scope/plugin/config");',
            "@scope/plugin/config",
            "@scope/plugin",
        ),
        (
            'import * as moduleApi from "node:module";\n'
            "const load = moduleApi.createRequire(import.meta.url);\n"
            'load("vite-plugin-namespace");',
            "vite-plugin-namespace",
            "vite-plugin-namespace",
        ),
        (
            'const { createRequire: makeRequire } = require("node:module");\n'
            "const load = makeRequire(import.meta.url);\n"
            'load("vite-plugin-cjs");',
            "vite-plugin-cjs",
            "vite-plugin-cjs",
        ),
        (
            'import { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            "const again = load;\n"
            'again("vite-plugin-alias");',
            "vite-plugin-alias",
            "vite-plugin-alias",
        ),
        (
            'import { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            'const rendered = `${load("vite-plugin-template")}`;',
            "vite-plugin-template",
            "vite-plugin-template",
        ),
        (
            'import { createRequire } from "node:module";\n'
            "const load: ReturnType<typeof createRequire> = createRequire(import.meta.url);\n"
            'load("vite-plugin-typed");',
            "vite-plugin-typed",
            "vite-plugin-typed",
        ),
        (
            'import { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            "const again: NodeRequire = load;\n"
            'again("vite-plugin-typed-alias");',
            "vite-plugin-typed-alias",
            "vite-plugin-typed-alias",
        ),
        (
            'import { createRequire } from "node:module";\n'
            "const make: typeof createRequire = createRequire;\n"
            "const load = make(import.meta.url);\n"
            'load("vite-plugin-typed-factory");',
            "vite-plugin-typed-factory",
            "vite-plugin-typed-factory",
        ),
        (
            'const { createRequire }: typeof import("node:module") = '
            'await import("node:module");\n'
            "const load = createRequire(import.meta.url);\n"
            'load("vite-plugin-typed-destructuring");',
            "vite-plugin-typed-destructuring",
            "vite-plugin-typed-destructuring",
        ),
    ],
)
def test_vitest_config_package_scanner_tracks_create_require_loaders(
    source: str,
    specifier: str,
    package: str,
) -> None:
    assert ts_tester._config_package_imports(source) == ((specifier, package),)


def test_vitest_config_snapshot_tracks_create_require_local_loads(tmp_path: Path) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        'import { createRequire as makeRequire } from "node:module";\n'
        "const load = makeRequire(import.meta.url);\n"
        'load("./setup");\n'
        "export default {};\n",
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "setup.ts" in closure


@pytest.mark.parametrize(
    "call",
    [
        "load(packageName)",
        "load.resolve(packageName)",
        'load("vite-plugin-" + packageName)',
        'load.resolve("vite-plugin-" + packageName)',
    ],
)
def test_vitest_config_create_require_loader_rejects_computed_specifiers(call: str) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        'const packageName = "vite-plugin-example";\n'
        f"{call};\n"
    )

    with pytest.raises(JauntConfigError, match="computed import/require specifier"):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    ("suffix", "message"),
    [
        ('load = customLoad; load("vite-plugin-example");', "reassigns"),
        ('load.call(null, "vite-plugin-example");', "unsupported member"),
        ('load["resolve"]("vite-plugin-example");', "unsupported member"),
        ("consume(load);", "ambiguously uses loader"),
    ],
)
def test_vitest_config_create_require_loader_rejects_unsupported_uses(
    suffix: str,
    message: str,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "let load = createRequire(import.meta.url);\n"
        f"{suffix}\n"
    )

    with pytest.raises(JauntConfigError, match=message):
        ts_tester._config_package_imports(source)


def test_vitest_config_create_require_loader_rejects_conditional_capture() -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const load = enabled ? createRequire(import.meta.url) : customLoad;\n"
        'load("vite-plugin-example");\n'
    )

    with pytest.raises(JauntConfigError, match="conditionally stores a createRequire result"):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        'import { createRequire } from "node:module"; consume(createRequire);',
        'import { createRequire } from "node:module"; createRequire.bind(null);',
        'import { createRequire } from "node:module"; '
        "const make = createRequire.bind(null); make(import.meta.url);",
        'import * as moduleApi from "node:module"; '
        'moduleApi["createRequire"](import.meta.url)("vite-plugin-example");',
    ],
)
def test_vitest_config_create_require_factory_rejects_unsupported_uses(source: str) -> None:
    with pytest.raises(JauntConfigError, match="createRequire|computed access"):
        ts_tester._config_package_imports(source)


def test_vitest_config_package_scanner_rejects_unproven_typed_destructuring() -> None:
    source = (
        'const { createRequire }: typeof import("node:module") = moduleLike;\n'
        "const load = createRequire(import.meta.url);\n"
        'load("vite-plugin-example");\n'
    )

    with pytest.raises(JauntConfigError, match="typed createRequire destructuring"):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        'import { createRequire } from "node:module";\n'
        "export const load = createRequire(import.meta.url);",
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        "export { load };",
        'import { createRequire } from "node:module";\nexport default createRequire;',
        'import * as moduleApi from "node:module";\nexport default { moduleApi };',
        'export const moduleApi = await import("node:module");',
    ],
)
def test_vitest_config_package_scanner_rejects_exported_loader_capabilities(
    source: str,
) -> None:
    with pytest.raises(JauntConfigError, match="exports a tracked module-loading capability"):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    "source",
    [
        'export { createRequire as make } from "node:module";',
        'export * from "node:module";',
        'export * as moduleApi from "node:module";',
        'export { default as moduleApi } from "module";',
    ],
)
def test_vitest_config_package_scanner_rejects_node_module_runtime_reexports(
    source: str,
) -> None:
    with pytest.raises(JauntConfigError, match="re-exports the Node module runtime"):
        ts_tester._config_package_imports(source)


def test_vitest_config_export_scan_does_not_claim_later_asi_statements() -> None:
    source = (
        "export const ordinary = 1\n"
        'import { createRequire } from "node:module"\n'
        "const load = createRequire(import.meta.url)\n"
        'load("vite-plugin-example")\n'
    )

    assert ts_tester._config_package_imports(source) == (
        ("vite-plugin-example", "vite-plugin-example"),
    )


@pytest.mark.parametrize(
    "source",
    [
        '(require)("vite-plugin-example");',
        'const load = (require);\nload("vite-plugin-example");',
        "consume(require);",
        "const holder = { require };",
        'module["require"]("vite-plugin-example");',
        'import { createRequire } from "node:module";\n'
        "holder.load = createRequire(import.meta.url);\n"
        'holder.load("vite-plugin-example");',
        'import { createRequire } from "node:module";\n'
        "const holder = { load: createRequire(import.meta.url) };",
        'import * as moduleApi from "node:module";\nconst holder = { moduleApi };',
    ],
)
def test_vitest_config_package_scanner_rejects_obscured_loader_capabilities(
    source: str,
) -> None:
    with pytest.raises(JauntConfigError):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    "expression",
    [
        "createRequire(import.meta.url) || customLoad",
        "load || customLoad",
    ],
)
def test_vitest_config_create_require_rejects_composed_capabilities(expression: str) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        f"const composed = {expression};\n"
        'composed("vite-plugin-example");\n'
    )

    with pytest.raises(JauntConfigError, match="ambiguously composes"):
        ts_tester._config_package_imports(source)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            "const createRequire = () => (value: string) => value;\n"
            "const load = createRequire();\n"
            'load("not-a-package-load");',
            (),
        ),
        (
            'import { createRequire as makeRequire } from "not-node-module";\n'
            "const load = makeRequire(import.meta.url);\n"
            'load("not-a-package-load");',
            (("not-node-module", "not-node-module"),),
        ),
        (
            "const moduleApi = { createRequire: () => (value: string) => value };\n"
            "const load = moduleApi.createRequire();\n"
            'load("not-a-package-load");',
            (),
        ),
        (
            'import type { createRequire } from "node:module";\n'
            "const load = createRequire(import.meta.url);\n"
            'load("not-a-package-load");',
            (),
        ),
    ],
)
def test_vitest_config_package_scanner_does_not_trust_create_require_by_name(
    source: str,
    expected: tuple[tuple[str, str], ...],
) -> None:
    assert ts_tester._config_package_imports(source) == expected


def test_vitest_config_snapshot_does_not_let_regex_quotes_hide_local_imports(
    tmp_path: Path,
) -> None:
    (tmp_path / "setup.ts").write_text("export {};\n", encoding="utf-8")
    (tmp_path / "vitest.config.ts").write_text(
        'const pattern = /"/;\nimport "./setup.ts";\nexport default { pattern };\n',
        encoding="utf-8",
    )

    closure, _overlays = ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")

    assert "setup.ts" in closure


def test_vitest_config_snapshot_rejects_dynamic_node_path_helpers(tmp_path: Path) -> None:
    (tmp_path / "vitest.config.ts").write_text(
        'import { join } from "node:path";\n'
        'export default { test: { setupFiles: [join("config", process.env.SETUP)] } };\n',
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="computed path that cannot be captured safely"):
        ts_tester._local_config_snapshot(tmp_path, "vitest.config.ts")


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink regression")
def test_vitest_config_closure_rejects_external_symlink_dependency(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.ts"
    outside.write_text("export const outside = true;\n", encoding="utf-8")
    (tmp_path / "setup.ts").symlink_to(outside)
    (tmp_path / "vitest.config.ts").write_text(
        'import "./setup.ts";\nexport default {};\n',
        encoding="utf-8",
    )

    with pytest.raises(JauntConfigError, match="escapes the workspace"):
        ts_tester._local_config_closure(tmp_path, "vitest.config.ts")


@pytest.mark.asyncio
async def test_nearer_fixture_creation_invalidates_prepared_battery_selection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    class NestedTestWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            (root / self.test_spec_path).unlink()
            self.test_spec_path = "tests/nested/math.jaunt-test.ts"
            nested_spec = root / self.test_spec_path
            nested_spec.parent.mkdir()
            nested_spec.write_text("// Verify the public double function.\n", encoding="utf-8")

    worker = NestedTestWorker(tmp_path)
    (tmp_path / "tests/fixtures.ts").write_text(
        'export const owner = "parent";\n',
        encoding="utf-8",
    )
    nearer = tmp_path / "tests/nested/fixtures.ts"

    class SelectionMutatingGenerator(FakeGenerator):
        changed = False

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            if not self.changed:
                nearer.write_text('export const owner = "nearer";\n', encoding="utf-8")
                self.changed = True
            return await super().generate_request(request, **kwargs)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    with pytest.raises(
        JauntGenerationError,
        match=r"inputs changed.*tests/nested/fixtures\.ts",
    ):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=SelectionMutatingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert not (tmp_path / "tests/nested/__generated__/math.example.test.ts").exists()
    assert not (tmp_path / "tests/nested/__generated__/math.derived.test.ts").exists()


@pytest.mark.asyncio
async def test_fixture_freshness_is_scoped_to_the_nearest_test_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path)

    class PackageTestWorker(_TestSpecWorker):
        def __init__(self, root: Path) -> None:
            super().__init__(root)
            (root / self.test_spec_path).unlink()
            self.test_spec_path = "tests/package-a/math.jaunt-test.ts"
            spec = root / self.test_spec_path
            spec.parent.mkdir(parents=True)
            spec.write_text("// Verify package A's public double function.\n", encoding="utf-8")

    worker = PackageTestWorker(tmp_path)
    fixture_a = tmp_path / "tests/package-a/fixtures.ts"
    fixture_b = tmp_path / "tests/package-b/fixtures.ts"
    fixture_a.write_text('export const owner = "a-v1";\n', encoding="utf-8")
    fixture_b.parent.mkdir(parents=True)
    fixture_b.write_text('export const owner = "b-v1";\n', encoding="utf-8")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    assert paths == frozenset(
        {
            "tests/package-a/__generated__/math.example.test.ts",
            "tests/package-a/__generated__/math.derived.test.ts",
        }
    )

    fixture_b.write_text('export const owner = "b-v2";\n', encoding="utf-8")
    sibling_only = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert sibling_only.exit_code == 0
    assert sibling_only.skipped == paths
    assert not sibling_only.generated
    assert not sibling_only.refrozen

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    fixture_a.write_text('export const owner = "a-v2";\n', encoding="utf-8")
    generator = CountingGenerator()
    owner_change = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        worker_factory=lambda *_: worker,
    )
    assert owner_change.exit_code == 0
    assert owner_change.generated == paths
    assert generator.calls == 2


@pytest.mark.asyncio
async def test_refreeze_keeps_rejected_marker_until_atomic_commit_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _RuntimeMutationTestWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    target = "tests/__generated__/math.example.test.ts"
    metadata = dict(_test_header_metadata((tmp_path / target).read_text()) or {})
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path=target,
        context_files={},
        prompt="generate",
        cache_payload={"path": worker.test_spec_path, "tier": "example"},
        validator=lambda _source: [],
        project_root=tmp_path,
    )
    marker_paths = _write_rejected_test_candidate(
        tmp_path,
        request,
        source_path=worker.test_spec_path,
        tier="example",
        fingerprint=metadata["battery_fingerprint"],
        candidate_source="export const rejected = true;\n",
        attempts=1,
        errors=("rejected",),
        expected_provenance=metadata,
    )
    assert marker_paths is not None
    runner = worker.installation.package_root / "dist/test/runner.js"
    runner.write_text("export const changedRunner = true;\n", encoding="utf-8")

    def fail_commit(*_args: Any, **_kwargs: Any) -> None:
        raise RuntimeError("simulated atomic commit failure")

    monkeypatch.setattr("jaunt.typescript.tester.atomic_write_manifest", fail_commit)
    with pytest.raises(RuntimeError, match="simulated atomic commit failure"):
        await run_test(
            tmp_path,
            config,
            no_build=True,
            generator=ExplodingGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert all((tmp_path / path).is_file() for path in marker_paths)


@pytest.mark.asyncio
async def test_toolchain_recompose_revalidates_existing_batteries_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    bodies = {path: _strip_test_header((tmp_path / path).read_text()) for path in paths}
    expected = json.loads(worker.sidecar)
    expected["structuralDigest"] = "sha256:toolchain-structure"
    expected["apiDigest"] = "sha256:toolchain-api"
    worker.module["structuralDigest"] = expected["structuralDigest"]
    worker.module["apiDigest"] = expected["apiDigest"]
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"
    monkeypatch.setattr("jaunt.typescript.reuse._store", lambda *_args: None)

    report = await run_test(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset()
    assert report.refrozen == paths
    assert report.runner["build"]["recomposed"] == ("ts:src/math",)
    for path in paths:
        assert _strip_test_header((tmp_path / path).read_text()) == bodies[path]


@pytest.mark.asyncio
async def test_separate_build_preserves_battery_reuse_proof_for_later_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    bodies = {path: _strip_test_header((tmp_path / path).read_text()) for path in paths}
    expected = json.loads(worker.sidecar)
    expected["structuralDigest"] = "sha256:toolchain-structure"
    expected["apiDigest"] = "sha256:toolchain-api"
    worker.module["structuralDigest"] = expected["structuralDigest"]
    worker.module["apiDigest"] = expected["apiDigest"]
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    build = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert build.refrozen == frozenset({"ts:src/math"})

    # A second metadata-only restamp must not erase the earlier API transition
    # before the separately invoked test command has consumed it.
    restamped = json.loads(str(worker.module["sidecar"]))
    restamped["fingerprint"] = "draft.2"
    worker.module["sidecar"] = json.dumps(restamped, sort_keys=True) + "\n"
    second_build = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert second_build.refrozen == frozenset({"ts:src/math"})

    report = await run_test(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset()
    assert report.refrozen == paths
    assert report.runner["build"]["refrozen"] == []
    for path in paths:
        assert _strip_test_header((tmp_path / path).read_text()) == bodies[path]

    # Consuming A -> B after the successful reheader means the next cycle
    # records B -> C rather than collapsing the lineage to A -> C.
    next_expected = json.loads(str(worker.module["sidecar"]))
    next_expected["structuralDigest"] = "sha256:next-toolchain-structure"
    next_expected["apiDigest"] = "sha256:next-toolchain-api"
    worker.module["structuralDigest"] = next_expected["structuralDigest"]
    worker.module["apiDigest"] = next_expected["apiDigest"]
    worker.module["sidecar"] = json.dumps(next_expected, sort_keys=True) + "\n"
    third_build = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert third_build.refrozen == frozenset({"ts:src/math"})
    next_report = await run_test(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert next_report.generated == frozenset()
    assert next_report.refrozen == paths


@pytest.mark.asyncio
async def test_migration_preserves_battery_reuse_proof_for_no_build_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    paths = frozenset(seeded.generated)
    bodies = {path: _strip_test_header((tmp_path / path).read_text()) for path in paths}
    expected = json.loads(str(worker.module["sidecar"]))
    expected["semanticEnvironmentDigest"] = "sha256:environment-v2"
    expected["structuralDigest"] = "sha256:environment-structure-v2"
    expected["apiDigest"] = "sha256:environment-api-v2"
    worker.module.update(expected)
    worker.module["sidecar"] = json.dumps(expected, sort_keys=True) + "\n"

    plan = await plan_typescript_migration(
        tmp_path,
        config,
        worker_factory=lambda *_: worker,
    )
    assert not plan.requires_rebuild
    apply_typescript_migration(plan)

    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset()
    assert report.refrozen == paths
    for path in paths:
        assert _strip_test_header((tmp_path / path).read_text()) == bodies[path]


@pytest.mark.asyncio
async def test_api_only_reheader_lands_when_sibling_generation_exhausts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    seeded_cache = ResponseCache(tmp_path / ".seed-cache")

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        response_cache=seeded_cache,
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    example_relative = "tests/__generated__/math.example.test.ts"
    derived_relative = "tests/__generated__/math.derived.test.ts"
    example = tmp_path / example_relative
    original = example.read_text(encoding="utf-8")
    original_metadata = dict(_test_header_metadata(original) or {})
    drifted_metadata = {
        **original_metadata,
        "target_api_digest": "sha256:" + "f" * 64,
        "battery_fingerprint": "sha256:" + "e" * 64,
    }
    example.write_text(
        _with_test_header(
            _strip_test_header(original),
            tier="example",
            source_path="tests/math.jaunt-test.ts",
            provenance=drifted_metadata,
        ),
        encoding="utf-8",
    )
    (tmp_path / derived_relative).unlink()

    class FailingDerivedGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            tier = str(request.cache_payload["tier"])
            self.calls.append(tier)
            assert tier == "derived"
            return (
                'import "../../src/__generated__/math.js";\n',
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    generator = FailingDerivedGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".fresh-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert generator.calls == ["derived"]
    assert report.refrozen == frozenset({example_relative})
    assert _strip_test_header(example.read_text(encoding="utf-8")) == _strip_test_header(original)
    refreshed_metadata = _test_header_metadata(example.read_text(encoding="utf-8"))
    assert refreshed_metadata is not None
    assert refreshed_metadata["target_api_digest"] == original_metadata["target_api_digest"]
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert outcomes[example_relative]["state"] == "verified"
    assert report.runner["partial_landing"]["accepted"] == (example_relative,)
    assert report.runner["partial_landing"]["committed"] is True
    assert not (tmp_path / derived_relative).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "co_drift_field",
    ["prompt_fingerprint", "runner_fingerprint"],
    ids=["prompt-and-api", "runner-and-api"],
)
async def test_api_transition_with_safe_drift_verifies_before_run_without_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    co_drift_field: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    original = example.read_text(encoding="utf-8")
    original_metadata = dict(_test_header_metadata(original) or {})
    drifted_metadata = {
        **original_metadata,
        "target_api_digest": "sha256:" + "f" * 64,
        co_drift_field: "sha256:" + "d" * 64,
        "battery_fingerprint": "sha256:" + "e" * 64,
    }
    example.write_text(
        _with_test_header(
            _strip_test_header(original),
            tier="example",
            source_path=worker.test_spec_path,
            provenance=drifted_metadata,
        ),
        encoding="utf-8",
    )
    phases: list[bool] = []
    safety_checked = False

    async def ordered_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal safety_checked
        typecheck_only = bool(kwargs.get("typecheck_only"))
        phases.append(typecheck_only)
        if typecheck_only:
            safety_checked = True
        else:
            assert safety_checked, "runtime verification must follow safety-aware typechecking"
            safety_checked = False
        return {
            "ok": True,
            "mode": "typecheck" if typecheck_only else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", ordered_batches)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert report.generated == frozenset()
    assert report.refrozen == frozenset({example_relative})
    assert phases == [True, False, True, False]
    refreshed = example.read_text(encoding="utf-8")
    refreshed_metadata = dict(_test_header_metadata(refreshed) or {})
    assert _strip_test_header(refreshed) == _strip_test_header(original)
    assert refreshed_metadata["target_api_digest"] == original_metadata["target_api_digest"]
    assert refreshed_metadata[co_drift_field] == original_metadata[co_drift_field]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_drift_field",
    ["policy_fingerprint", "skills_fingerprint", "fast_check_fingerprint"],
)
async def test_api_transition_with_content_policy_drift_still_regenerates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content_drift_field: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    original = example.read_text(encoding="utf-8")
    metadata = {
        **dict(_test_header_metadata(original) or {}),
        "target_api_digest": "sha256:" + "f" * 64,
        content_drift_field: "sha256:" + "d" * 64,
        "battery_fingerprint": "sha256:" + "e" * 64,
    }
    example.write_text(
        _with_test_header(
            _strip_test_header(original),
            tier="example",
            source_path=worker.test_spec_path,
            provenance=metadata,
        ),
        encoding="utf-8",
    )

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls.append(str(request.cache_payload["tier"]))
            return await super().generate_request(request, **kwargs)

    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / f".{content_drift_field}-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.calls == ["example"]
    assert report.generated == frozenset({example_relative})
    assert example_relative not in report.refrozen


@pytest.mark.asyncio
async def test_api_and_runner_drift_never_executes_an_unsafe_existing_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    original = example.read_text(encoding="utf-8")
    metadata = {
        **dict(_test_header_metadata(original) or {}),
        "target_api_digest": "sha256:" + "f" * 64,
        "runner_fingerprint": "sha256:" + "d" * 64,
        "battery_fingerprint": "sha256:" + "e" * 64,
    }
    unsafe = (
        'declare const require: (specifier: string) => unknown;\nvoid require("node:module");\n'
    )
    example.write_text(
        _with_test_header(
            unsafe,
            tier="example",
            source_path=worker.test_spec_path,
            provenance=metadata,
        ),
        encoding="utf-8",
    )
    phases: list[tuple[bool, bool]] = []

    async def reject_unsafe_typecheck(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        typecheck_only = bool(kwargs.get("typecheck_only"))
        overlays = kwargs.get("overlays", {})
        has_unsafe_body = isinstance(overlays, Mapping) and any(
            'require("node:module")' in str(source) for source in overlays.values()
        )
        phases.append((typecheck_only, has_unsafe_body))
        if not typecheck_only:
            assert not has_unsafe_body, "unsafe existing bytes reached runtime verification"
        if typecheck_only and has_unsafe_body:
            return {
                "ok": False,
                "mode": "typecheck",
                "tests": [],
                "diagnostics": [
                    {
                        "code": "JAUNT_TS_TEST_DYNAMIC_LOADER",
                        "message": "dynamic loading is forbidden",
                        "severity": "error",
                        "path": example_relative,
                    }
                ],
            }
        return {
            "ok": True,
            "mode": "typecheck" if typecheck_only else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls.append(str(request.cache_payload["tier"]))
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        reject_unsafe_typecheck,
    )
    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".unsafe-verification-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert generator.calls == ["example"]
    assert report.generated == frozenset({example_relative})
    assert any(typecheck and unsafe_body for typecheck, unsafe_body in phases)
    assert not any(not typecheck and unsafe_body for typecheck, unsafe_body in phases)
    assert 'require("node:module")' not in example.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_api_only_reheader_failure_regenerates_the_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    source = example.read_text(encoding="utf-8")
    metadata = dict(_test_header_metadata(source) or {})
    metadata.update(
        {
            "target_api_digest": "sha256:" + "f" * 64,
            "prompt_fingerprint": "sha256:" + "d" * 64,
            "battery_fingerprint": "sha256:" + "e" * 64,
        }
    )
    example.write_text(
        _with_test_header(
            _strip_test_header(source),
            tier="example",
            source_path="tests/math.jaunt-test.ts",
            provenance=metadata,
        ),
        encoding="utf-8",
    )
    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    api_drift = next(
        diagnostic
        for diagnostic in status.diagnostics
        if diagnostic.path == example_relative and diagnostic.code == "JAUNT_TS_TEST_BATTERY_STALE"
    )
    assert set(api_drift.data["mismatches"]) == {
        "battery_fingerprint",
        "prompt_fingerprint",
        "target_api_digest",
    }
    assert "jaunt test --language ts --no-build" in api_drift.message
    assert "without `--no-run`" in api_drift.message
    runtime_calls = 0

    async def reject_verification(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_calls += 1
        if runtime_calls <= 2:
            return {
                "ok": False,
                "mode": "run",
                "tests": [{"path": example_relative, "ok": False}],
                "failures": [{"category": "assertion", "path": example_relative}],
            }
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls.append(str(request.cache_payload["tier"]))
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", reject_verification)
    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".verification-fallback-cache"),
        worker_factory=lambda *_: worker,
    )

    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert generator.calls == ["example"]
    assert report.generated == frozenset({example_relative})
    assert example_relative not in report.refrozen


@pytest.mark.asyncio
@pytest.mark.parametrize("category", ["runner", "runner-protocol", "timeout", "unknown"])
async def test_api_only_verification_infrastructure_never_queues_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    category: str,
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    original = example.read_text(encoding="utf-8")
    metadata = dict(_test_header_metadata(original) or {})
    metadata.update(
        {
            "target_api_digest": "sha256:" + "f" * 64,
            "battery_fingerprint": "sha256:" + "e" * 64,
        }
    )
    example.write_text(
        _with_test_header(
            _strip_test_header(original),
            tier="example",
            source_path="tests/math.jaunt-test.ts",
            provenance=metadata,
        ),
        encoding="utf-8",
    )
    drifted_source = example.read_text(encoding="utf-8")

    runtime_calls = 0

    async def unavailable_verifier(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}
        runtime_calls += 1
        result: dict[str, Any] = {
            "ok": False,
            "mode": "run",
            "tests": [],
        }
        if category != "unknown":
            result["failures"] = [{"category": category}]
        if category == "timeout":
            result["timedOut"] = True
        return result

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unavailable_verifier)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=ExplodingGenerator(),
        response_cache=ResponseCache(tmp_path / ".verification-infrastructure-cache"),
        worker_factory=lambda *_: worker,
    )

    assert seeded.exit_code == 0
    assert report.exit_code == 3
    assert runtime_calls == 1
    assert example.read_text(encoding="utf-8") == drifted_source
    assert {
        diagnostic.code for diagnostics in report.failed.values() for diagnostic in diagnostics
    } == {"JAUNT_TS_TEST_VERIFICATION_INFRASTRUCTURE"}
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    assert outcomes[example_relative]["state"] == "verification-infrastructure"


@pytest.mark.asyncio
async def test_no_run_regenerates_api_drift_without_executing_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    example_relative = "tests/__generated__/math.example.test.ts"
    example = tmp_path / example_relative
    source = example.read_text(encoding="utf-8")
    metadata = dict(_test_header_metadata(source) or {})
    metadata.update(
        {
            "target_api_digest": "sha256:" + "f" * 64,
            "battery_fingerprint": "sha256:" + "e" * 64,
        }
    )
    example.write_text(
        _with_test_header(
            _strip_test_header(source),
            tier="example",
            source_path=worker.test_spec_path,
            provenance=metadata,
        ),
        encoding="utf-8",
    )
    runtime_calls = 0

    async def typecheck_only(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal runtime_calls
        if not kwargs.get("typecheck_only"):
            runtime_calls += 1
            raise AssertionError("--no-run must not execute API-transition batteries")
        return {"ok": True, "mode": "typecheck", "tests": [], "diagnostics": []}

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls.append(str(request.cache_payload["tier"]))
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr(
        "jaunt.typescript.tester.proven_previous_target_api_digests",
        lambda *_args, **_kwargs: frozenset({metadata["target_api_digest"]}),
    )
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", typecheck_only)
    generator = CountingGenerator()
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        no_run=True,
        generator=generator,
        response_cache=ResponseCache(tmp_path / ".no-run-api-drift-cache"),
        worker_factory=lambda *_: worker,
    )

    assert seeded.exit_code == 0
    assert report.exit_code == 0
    assert generator.calls == ["example"]
    assert runtime_calls == 0
    assert example_relative in report.generated


def test_rejected_candidate_paths_bound_long_components_and_keep_full_identity(
    tmp_path: Path,
) -> None:
    long_stem = "intent-" + "a" * 500
    target_path = f"tests/__generated__/{long_stem}.example.test.tsx"
    other_target = f"tests/__generated__/{long_stem}-other.example.test.tsx"
    candidate_source = "export const exact = true;\n"
    candidate_digest = _digest(candidate_source)
    candidate_relative, metadata_relative = _rejected_test_paths(
        target_path,
        candidate_digest=candidate_digest,
    )
    other_metadata = _rejected_test_paths(other_target)[1]
    lock_relative = metadata_relative.with_suffix(".lock")

    assert metadata_relative != other_metadata
    assert candidate_digest.removeprefix("sha256:") in candidate_relative.name
    for relative in (candidate_relative, metadata_relative, lock_relative):
        assert max(len(component.encode("utf-8")) for component in relative.parts) < 200

    fingerprint = "sha256:" + "a" * 64
    provenance = {
        "test_spec_digest": "sha256:" + "b" * 64,
        "target_api_digest": "sha256:" + "c" * 64,
        "battery_fingerprint": fingerprint,
    }
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path=target_path,
        context_files={},
        prompt="generate",
        cache_payload={"path": "tests/long.jaunt-test.tsx", "tier": "example"},
        validator=lambda _source: [],
        project_root=tmp_path,
    )
    written = _write_rejected_test_candidate(
        tmp_path,
        request,
        source_path="tests/long.jaunt-test.tsx",
        tier="example",
        fingerprint=fingerprint,
        candidate_source=candidate_source,
        attempts=1,
        errors=("rejected",),
        expected_provenance=provenance,
    )

    assert written == (candidate_relative.as_posix(), metadata_relative.as_posix())
    assert (tmp_path / candidate_relative).is_file()
    assert (tmp_path / metadata_relative).is_file()
    assert (tmp_path / lock_relative).is_file()
    token = _rejected_test_token(
        tmp_path,
        target_path,
        expected_fingerprint=fingerprint,
        expected_provenance=provenance,
    )
    assert _clear_rejected_test_candidate(
        tmp_path,
        target_path,
        expected_token=token,
    )


def test_rejected_candidate_preserves_exact_bytes_and_cleanup_uses_snapshot_cas(
    tmp_path: Path,
) -> None:
    target_path = "tests/__generated__/exact.example.test.ts"
    fingerprint = "sha256:" + "a" * 64
    provenance = {
        "test_spec_digest": "sha256:" + "b" * 64,
        "target_api_digest": "sha256:" + "c" * 64,
        "prompt_fingerprint": "sha256:" + "d" * 64,
        "battery_fingerprint": fingerprint,
    }
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path=target_path,
        context_files={},
        prompt="generate",
        cache_payload={"path": "tests/exact.jaunt-test.ts", "tier": "example"},
        validator=lambda _source: [],
        project_root=tmp_path,
    )
    first_source = "export const exact = 1;  \t"
    first_paths = _write_rejected_test_candidate(
        tmp_path,
        request,
        source_path="tests/exact.jaunt-test.ts",
        tier="example",
        fingerprint=fingerprint,
        candidate_source=first_source,
        attempts=1,
        errors=("rejected",),
        terminal=True,
        expected_provenance=provenance,
    )
    assert first_paths is not None
    first_candidate = tmp_path / first_paths[0]
    assert first_candidate.read_bytes() == first_source.encode("utf-8")
    first_metadata = json.loads((tmp_path / first_paths[1]).read_text(encoding="utf-8"))
    assert first_metadata["schema"] == 2
    assert first_metadata["candidate_digest"] == _digest(first_source)
    assert first_metadata["expected_provenance"] == provenance
    assert first_metadata["semantic_identity"].startswith("sha256:")
    first_token = _rejected_test_token(
        tmp_path,
        target_path,
        expected_fingerprint=fingerprint,
        expected_provenance=provenance,
    )
    assert first_token is not None

    second_source = "export const exact = 2;\n"
    drifted_provenance = {
        **provenance,
        "prompt_fingerprint": "sha256:" + "e" * 64,
        "battery_fingerprint": "sha256:" + "f" * 64,
    }
    second_paths = _write_rejected_test_candidate(
        tmp_path,
        request,
        source_path="tests/exact.jaunt-test.ts",
        tier="example",
        fingerprint=drifted_provenance["battery_fingerprint"],
        candidate_source=second_source,
        attempts=1,
        errors=("rejected again",),
        terminal=True,
        expected_provenance=drifted_provenance,
    )
    assert second_paths is not None
    assert not _clear_rejected_test_candidate(
        tmp_path,
        target_path,
        expected_token=first_token,
    )
    current = _rejected_test_diagnostic(
        tmp_path,
        target_path,
        expected_fingerprint=drifted_provenance["battery_fingerprint"],
        expected_provenance=drifted_provenance,
    )
    assert current is not None
    assert current["candidate"] == second_paths[0]
    assert current["consecutive_attempts"] == 2
    second_token = _rejected_test_token(
        tmp_path,
        target_path,
        expected_fingerprint=drifted_provenance["battery_fingerprint"],
        expected_provenance=drifted_provenance,
    )
    assert _clear_rejected_test_candidate(
        tmp_path,
        target_path,
        expected_token=second_token,
    )
    assert not (tmp_path / second_paths[0]).exists()
    assert not (tmp_path / second_paths[1]).exists()


@pytest.mark.asyncio
async def test_imported_type_context_drift_resets_terminal_exhaustion_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    worker.module["contextSource"] = _test_imported_type_context_source("id: string;")
    original_api_digest = worker.module["apiDigest"]
    original_api_source = worker.module["apiSource"]

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class RejectedGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            return (
                'import "../../src/__generated__/math.js";\n',
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    first = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".imported-context-marker-v1"),
        worker_factory=lambda *_: worker,
    )
    battery_paths = {
        "tests/__generated__/math.example.test.ts",
        "tests/__generated__/math.derived.test.ts",
    }
    example_path = "tests/__generated__/math.example.test.ts"
    assert first.exit_code == 3
    first_outcomes = {item["path"]: item for item in first.runner["batteries"]}
    first_metadata = json.loads(
        (tmp_path / first_outcomes[example_path]["candidate_metadata"]).read_text(encoding="utf-8")
    )
    first_context_fingerprint = first_metadata["expected_provenance"][
        "imported_type_context_fingerprint"
    ]
    assert first_metadata["consecutive_attempts"] == 1

    stable = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert {
        str(diagnostic.path): diagnostic.code
        for diagnostic in stable.diagnostics
        if diagnostic.path in battery_paths
    } == {path: "JAUNT_TS_TEST_GENERATION_EXHAUSTED" for path in battery_paths}

    worker.module["contextSource"] = _test_imported_type_context_source(
        "id: string; required_label: string;"
    )
    assert worker.module["apiDigest"] == original_api_digest
    assert worker.module["apiSource"] == original_api_source
    drifted = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert {
        str(diagnostic.path): diagnostic.code
        for diagnostic in drifted.diagnostics
        if diagnostic.path in battery_paths
    } == {path: "JAUNT_TS_TEST_BATTERY_MISSING" for path in battery_paths}

    second = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".imported-context-marker-v2"),
        worker_factory=lambda *_: worker,
    )
    assert second.exit_code == 3
    second_outcomes = {item["path"]: item for item in second.runner["batteries"]}
    second_metadata = json.loads(
        (tmp_path / second_outcomes[example_path]["candidate_metadata"]).read_text(encoding="utf-8")
    )
    assert second_metadata["consecutive_attempts"] == 1
    assert (
        second_metadata["expected_provenance"]["imported_type_context_fingerprint"]
        != first_context_fingerprint
    )


@pytest.mark.asyncio
async def test_exhausted_marker_survives_prompt_drift_but_resets_for_semantic_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class RejectedGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            return (
                'import "../../src/__generated__/math.js";\n',
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    seeded = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert seeded.exit_code == 0

    test_spec = tmp_path / worker.test_spec_path
    exhausted_test_spec = "// A changed authored test contract that will exhaust generation.\n"
    test_spec.write_text(exhausted_test_spec, encoding="utf-8")
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".semantic-marker-cache"),
        worker_factory=lambda *_: worker,
    )
    assert report.exit_code == 3

    battery_paths = {
        "tests/__generated__/math.example.test.ts",
        "tests/__generated__/math.derived.test.ts",
    }

    def diagnostic_codes(status: TargetStatus | TargetCheckReport) -> dict[str, str]:
        return {
            str(diagnostic.path): diagnostic.code
            for diagnostic in status.diagnostics
            if diagnostic.path in battery_paths
        }

    def prompt_drifted_provenance(*args: Any, **kwargs: Any) -> Mapping[str, str]:
        provenance = dict(_test_provenance(*args, **kwargs))
        provenance["prompt_fingerprint"] = "sha256:" + "d" * 64
        provenance["battery_fingerprint"] = "sha256:" + "e" * 64
        return provenance

    async def unexpected_typecheck(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("check must exclude a semantically current exhausted battery")

    with monkeypatch.context() as prompt_drift:
        prompt_drift.setattr(
            "jaunt.typescript.tester._test_provenance",
            prompt_drifted_provenance,
        )
        drifted = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
        prompt_drift.setattr(
            "jaunt.typescript.tester._run_test_batches",
            unexpected_typecheck,
        )
        checked = await run_check(
            tmp_path,
            config,
            magic_only=True,
            worker_factory=lambda *_: worker,
        )

    assert diagnostic_codes(drifted) == {
        path: "JAUNT_TS_TEST_GENERATION_EXHAUSTED" for path in battery_paths
    }
    assert diagnostic_codes(checked) == diagnostic_codes(drifted)

    test_spec.write_text("// A different authored test contract.\n", encoding="utf-8")
    changed_spec = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert diagnostic_codes(changed_spec) == {
        path: "JAUNT_TS_TEST_BATTERY_STALE" for path in battery_paths
    }
    test_spec.write_text(exhausted_test_spec, encoding="utf-8")

    original_api_digest = worker.module["apiDigest"]
    original_api_source = worker.module["apiSource"]
    worker.module["apiDigest"] = "sha256:changed-api"
    worker.module["apiSource"] = (
        "/** Double a number. */\nexport declare function double(value: string): string;\n"
    )
    changed_api = await run_status(tmp_path, config, worker_factory=lambda *_: worker)
    assert diagnostic_codes(changed_api) == {
        path: "JAUNT_TS_TEST_BATTERY_STALE" for path in battery_paths
    }
    worker.module["apiDigest"] = original_api_digest
    worker.module["apiSource"] = original_api_source


@pytest.mark.asyncio
async def test_exhausted_battery_persists_exact_candidate_and_check_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    (tmp_path / worker.test_spec_path).write_text(
        "// Verify a changed public contract.\n", encoding="utf-8"
    )
    worker.module["specSource"] = (
        'import * as jaunt from "@usejaunt/ts/spec";\n'
        "jaunt.magicModule();\n"
        "/** Double a number.\n"
        " * @prop given value: fc.integer() :: double(value) equals value * 2\n"
        " */\n"
        "export function double(value: number): number { return jaunt.magic(); }\n"
    )

    class RejectedGenerator(FakeGenerator):
        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            return (
                'import "../../src/__generated__/math.js";\n',
                TokenUsage(20, 10, "fake-ts", "fake"),
                (),
            )

    report = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".rejected-cache"),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 3
    assert {
        diagnostic.code for diagnostics in report.failed.values() for diagnostic in diagnostics
    } == {"JAUNT_TS_TEST_GENERATION_EXHAUSTED"}
    outcomes = {item["path"]: item for item in report.runner["batteries"]}
    example = outcomes["tests/__generated__/math.example.test.ts"]
    candidate = tmp_path / example["candidate"]
    metadata_path = tmp_path / example["candidate_metadata"]
    candidate_source = candidate.read_text(encoding="utf-8")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert 'import "../../src/__generated__/math.js"' in candidate_source
    assert "const __jauntPropertyArbitrary_" in candidate_source
    assert metadata["terminal"] is True
    assert metadata["consecutive_attempts"] == 1
    assert metadata["candidate_digest"] == _digest(candidate_source)

    second = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".rejected-cache-second"),
        worker_factory=lambda *_: worker,
    )
    second_outcomes = {item["path"]: item for item in second.runner["batteries"]}
    second_metadata = json.loads(
        (
            tmp_path
            / second_outcomes["tests/__generated__/math.example.test.ts"]["candidate_metadata"]
        ).read_text(encoding="utf-8")
    )
    assert second.exit_code == 3
    assert second_metadata["consecutive_attempts"] == 2

    (tmp_path / worker.test_spec_path).write_text(
        "// Verify another changed public contract.\n",
        encoding="utf-8",
    )
    third = await run_test(
        tmp_path,
        config,
        no_build=True,
        max_attempts=1,
        generator=RejectedGenerator(),
        response_cache=ResponseCache(tmp_path / ".rejected-cache-third"),
        worker_factory=lambda *_: worker,
    )
    third_outcomes = {item["path"]: item for item in third.runner["batteries"]}
    marker_paths = {
        path: (tmp_path / outcome["candidate"], tmp_path / outcome["candidate_metadata"])
        for path, outcome in third_outcomes.items()
    }
    for _candidate_path, marker_path in marker_paths.values():
        reset_metadata = json.loads(marker_path.read_text(encoding="utf-8"))
        assert reset_metadata["consecutive_attempts"] == 1

    example_path = tmp_path / "tests/__generated__/math.example.test.ts"
    example_path.parent.mkdir(parents=True, exist_ok=True)
    example_path.write_text(
        _with_test_header(
            "const invalidType: string = 1;\n",
            tier="example",
            source_path=worker.test_spec_path,
            provenance={},
        ),
        encoding="utf-8",
    )
    checked_typecheck_files: list[tuple[str, ...]] = []

    async def reject_type_invalid_marker(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        files = tuple(kwargs.get("files", ()))
        if kwargs.get("typecheck_only"):
            checked_typecheck_files.append(files)
            if "tests/__generated__/math.example.test.ts" in files:
                return {
                    "ok": False,
                    "mode": "typecheck",
                    "tests": [],
                    "diagnostics": [
                        {
                            "code": "TS2322",
                            "message": "invalid stale exhausted battery",
                            "severity": "error",
                        }
                    ],
                }
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    monkeypatch.setattr(
        "jaunt.typescript.tester._run_test_batches",
        reject_type_invalid_marker,
    )
    checked = await run_check(
        tmp_path,
        config,
        magic_only=True,
        worker_factory=lambda *_: worker,
    )
    exhausted = [
        item for item in checked.diagnostics if item.code == "JAUNT_TS_TEST_GENERATION_EXHAUSTED"
    ]
    assert len(exhausted) == 2
    assert all(item.data["consecutive_attempts"] == 1 for item in exhausted)
    assert all(
        "tests/__generated__/math.example.test.ts" not in files for files in checked_typecheck_files
    )
    assert not any(item.code == "JAUNT_TS_TEST_TYPECHECK" for item in checked.diagnostics)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    recovered = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        response_cache=ResponseCache(tmp_path / ".rejected-cache-recovery"),
        worker_factory=lambda *_: worker,
    )
    assert recovered.exit_code == 0
    assert all(
        not candidate_path.exists() and not metadata_path.exists()
        for candidate_path, metadata_path in marker_paths.values()
    )


@pytest.mark.asyncio
async def test_test_incrementality_restores_body_cache_but_regenerates_content_drift(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)

    async def green_batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "tests": [],
            "diagnostics": [],
        }

    class CountingGenerator(FakeGenerator):
        def __init__(self) -> None:
            self.calls = 0

        async def generate_request(
            self, request: GenerationRequest, **kwargs: Any
        ) -> tuple[str, TokenUsage, tuple[str, ...]]:
            self.calls += 1
            return await super().generate_request(request, **kwargs)

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    example = tmp_path / "tests/__generated__/math.example.test.ts"
    example.write_text(example.read_text() + "// hand edit\n")
    body_generator = CountingGenerator()
    body_report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=body_generator,
        worker_factory=lambda *_: worker,
    )
    assert body_generator.calls == 0
    assert body_report.generated == frozenset({"tests/__generated__/math.example.test.ts"})
    assert body_report.skipped == frozenset({"tests/__generated__/math.derived.test.ts"})
    assert body_report.runner["cost"]["cache_hits"] == 1

    (tmp_path / worker.test_spec_path).write_text("// Verify a semantically changed contract.\n")
    content_generator = CountingGenerator()
    content_report = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=content_generator,
        worker_factory=lambda *_: worker,
    )
    assert content_generator.calls == 2
    assert content_report.generated == frozenset(
        {
            "tests/__generated__/math.example.test.ts",
            "tests/__generated__/math.derived.test.ts",
        }
    )
    assert not content_report.skipped
    assert not content_report.refrozen


@pytest.mark.asyncio
async def test_check_carries_diagnostics_but_warnings_do_not_block(tmp_path: Path) -> None:
    config = _config(tmp_path)

    class WarningWorker(FakeWorker):
        async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            result = await super().request(method, params)
            if method == "validateOverlay":
                result["diagnostics"] = [
                    {
                        "code": "JAUNT_TS_ADVISORY",
                        "severity": "warning",
                        "message": "A non-blocking advisory.",
                        "path": "tsconfig.json",
                    }
                ]
            return result

    worker = WarningWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    report = await run_check(
        tmp_path,
        config,
        magic_only=True,
        worker_factory=lambda *_: worker,
    )
    assert report.exit_code == 0
    assert [item.severity for item in report.diagnostics] == ["warning"]
    payload = check_payload(report)
    assert payload["diagnostics"][0]["message"] == "A non-blocking advisory."
    assert any("JAUNT_TS_ADVISORY" in line for line in human_lines(payload))

    failure_payload = check_payload(
        TargetCheckReport(
            language="ts",
            diagnostics=(
                TargetDiagnostic(
                    code="JAUNT_TS_CHECK_ERROR",
                    message="The deterministic check failed.",
                    path="src/math.ts",
                ),
            ),
            blocked=({"reason": "stale-battery", "target": "src/math.ts#double"},),
            exit_code=4,
        )
    )
    rendered = "\n".join(human_lines(failure_payload))
    assert "The deterministic check failed." in rendered
    assert "stale-battery: src/math.ts#double" in rendered


def test_check_payload_synthesizes_magic_blocker_diagnostics(tmp_path: Path) -> None:
    report = TargetCheckReport(
        language="ts",
        root=tmp_path,
        stale={"ts:src/stale": "structural"},
        unbuilt=frozenset({"ts:src/new"}),
        invalid={
            "ts:src/bad": (
                TargetDiagnostic(
                    code="JAUNT_TS_API_DRIFT",
                    message="The API mirror drifted.",
                    path="src/bad.ts",
                ),
            )
        },
        orphans=(
            TargetArtifact(
                path=tmp_path / "src/__generated__/ghost.ts",
                kind="implementation",
                module_id="ts:src/ghost",
            ),
            TargetArtifact(
                path=tmp_path / "tests/contract/ghost.test.ts",
                kind="contract-battery",
                module_id="ts-contract:ghost",
            ),
        ),
        diagnostics=(
            TargetDiagnostic(
                code="JAUNT_TS_ADVISORY",
                message="Review this warning.",
                severity="warning",
            ),
        ),
        exit_code=4,
    )

    payload = check_payload(report)

    assert [item["code"] for item in payload["diagnostics"]] == [
        "JAUNT_TS_ADVISORY",
        "JAUNT_MAGIC_STALE",
        "JAUNT_MAGIC_UNBUILT",
        "JAUNT_TS_API_DRIFT",
        "JAUNT_MAGIC_ORPHAN",
    ]
    blockers = payload["diagnostics"][1:]
    assert [item["severity"] for item in blockers] == ["error"] * 4
    assert [item["data"]["state"] for item in blockers] == [
        "stale",
        "unbuilt",
        "invalid",
        "orphan",
    ]
    assert [item["data"].get("target") for item in blockers] == [
        "ts:src/stale",
        "ts:src/new",
        "ts:src/bad",
        None,
    ]
    assert blockers[0]["data"]["reason"] == "structural"
    assert blockers[-1]["path"] == "src/__generated__/ghost.ts"
    assert not any(
        item.get("path") == "tests/contract/ghost.test.ts" for item in payload["diagnostics"]
    )
    assert payload["magic"]["ts"]["orphans"] == [
        "src/__generated__/ghost.ts",
        "tests/contract/ghost.test.ts",
    ]
    assert payload["targets"]["ts"]["diagnostics"] == payload["diagnostics"]
    rendered = "\n".join(human_lines(payload))
    assert "JAUNT_MAGIC_STALE" in rendered
    assert "ts:src/stale" in rendered


@pytest.mark.asyncio
async def test_magic_eject_converts_target_owned_test_intent_and_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    await run_build(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"ok": True, "mode": "run", "tests": [], "diagnostics": []}

    async def green_runner(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "mode": "typecheck" if kwargs.get("typecheck_only") else "run",
            "diagnostics": [],
            **(
                {"emittedDeclarations": ["src/math.d.ts"]} if kwargs.get("declaration_emit") else {}
            ),
        }

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", green_batches)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", green_runner)
    tested = await run_test(
        tmp_path,
        config,
        no_build=True,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert tested.exit_code == 0

    report = await run_eject(
        tmp_path,
        config,
        target="ts:src/math",
        worker_factory=lambda *_: worker,
    )
    assert report.ok
    assert not (tmp_path / worker.test_spec_path).exists()
    for tier in ("example", "derived"):
        generated = tmp_path / f"tests/__generated__/math.{tier}.test.ts"
        ordinary = tmp_path / f"tests/math.{tier}.test.ts"
        assert not generated.exists()
        assert ordinary.is_file()
        text = ordinary.read_text()
        assert "jaunt:" not in text
        assert "@usejaunt" not in text
        assert "__generated__" not in text
    assert worker.test_spec_path in report.removed
    assert "tests/math.example.test.ts" in report.changed
    assert "tests/math.derived.test.ts" in report.changed


@pytest.mark.asyncio
async def test_windows_runner_timeout_terminates_the_process_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    class Taskkill:
        async def wait(self) -> int:
            return 0

    class Process:
        pid = 321
        returncode: int | None = None
        killed = False

        def kill(self) -> None:
            self.killed = True
            self.returncode = -9

        async def wait(self) -> int:
            self.returncode = -9
            return -9

    async def create(*args: str, **_kwargs: Any) -> Taskkill:
        calls.append(tuple(args))
        return Taskkill()

    monkeypatch.setattr("jaunt.typescript.tester.asyncio.create_subprocess_exec", create)
    process = Process()
    await _terminate_runner_process(process, platform="nt")
    assert calls == [("taskkill", "/PID", "321", "/T", "/F")]


@pytest.mark.asyncio
async def test_node_permission_runner_uses_only_physical_sandbox_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    config = _config(source)
    package = source / "node_modules/@usejaunt/ts"
    source_runner = package / "dist/test/runner.js"
    source_guard = package / "dist/test/permission_guard.cjs"
    source_compiler = source / "node_modules/typescript/lib/typescript.js"
    source_package_owner = source / "packages/app"
    source_package_owner.mkdir(parents=True)
    for path in (source_runner, source_guard, source_compiler):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("export {};\n", encoding="utf-8")

    external_modules = tmp_path / "external-store/node_modules"
    external_package = external_modules / "@usejaunt/ts"
    external_compiler = external_modules / "typescript"
    for source_path, target in (
        (source_runner, external_package / "dist/test/runner.js"),
        (source_guard, external_package / "dist/test/permission_guard.cjs"),
        (source_compiler, external_compiler / "lib/typescript.js"),
    ):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source_path.read_bytes())
    physical_parent = tmp_path / "physical-view"
    physical_root = physical_parent / "workspace"
    (physical_root / "node_modules/@usejaunt").mkdir(parents=True)
    (physical_root / "node_modules/@usejaunt/ts").symlink_to(
        external_package, target_is_directory=True
    )
    (physical_root / "node_modules/typescript").symlink_to(
        external_compiler, target_is_directory=True
    )
    (physical_root / source_package_owner.relative_to(source)).mkdir(parents=True)
    alias_parent = tmp_path / "lexical-view"
    alias_parent.symlink_to(physical_parent, target_is_directory=True)
    lexical_root = alias_parent / "workspace"

    captured: dict[str, Any] = {}

    class Process:
        returncode = 0

        async def communicate(self, payload: bytes) -> tuple[bytes, bytes]:
            captured["payload"] = json.loads(payload)
            return (
                json.dumps(
                    {
                        "ok": True,
                        "mode": "run",
                        "diagnostics": [],
                        "tests": [
                            {
                                "file": "tests/safe.example.test.ts",
                                "tier": "example",
                                "status": "passed",
                                "durationMs": 0,
                            }
                        ],
                        "captured": {"stdout": "", "stderr": ""},
                    }
                ).encode(),
                b"",
            )

    async def create(*args: str, **kwargs: Any) -> Process:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return Process()

    monkeypatch.setattr("jaunt.typescript.tester._bubblewrap_executable", lambda _env: None)
    monkeypatch.setattr(
        "jaunt.typescript.tester._node_permission_flag", lambda _node: "--permission"
    )
    monkeypatch.setattr("jaunt.typescript.tester.asyncio.create_subprocess_exec", create)
    client = SimpleNamespace(
        installation=SimpleNamespace(
            node="node",
            package_root=package,
            compiler_module_path=source_compiler,
        )
    )

    result = await _run_test_runner(
        client,
        lexical_root,
        config,
        files=(),
        isolated_from=source,
        package_root=str(source_package_owner),
        redact_derived=False,
    )

    assert result["ok"] is True
    physical_root = physical_root.resolve()
    physical_runner = physical_root / source_runner.relative_to(source)
    physical_compiler = physical_root / source_compiler.relative_to(source)
    args = captured["args"]
    kwargs = captured["kwargs"]
    payload = captured["payload"]
    assert payload["root"] == str(physical_root)
    assert payload["compilerModulePath"] == str(physical_compiler)
    assert payload["packageRoot"] == str(physical_root / source_package_owner.relative_to(source))
    assert kwargs["cwd"] == str(physical_root)
    assert kwargs["env"]["PWD"] == str(physical_root)
    assert args[-1] == str(physical_runner)
    assert f"--require={physical_runner.parent / 'permission_guard.cjs'}" in args
    assert [arg for arg in args if arg.startswith("--allow-fs-write=")] == [
        f"--allow-fs-write={physical_root}"
    ]
    read_grants = {
        Path(arg.removeprefix("--allow-fs-read="))
        for arg in args
        if arg.startswith("--allow-fs-read=")
    }
    assert read_grants == {physical_root, external_modules}
    assert alias_parent not in read_grants
    assert physical_root.parent not in read_grants
    assert str(alias_parent) not in json.dumps(captured, default=str)


def test_status_freshness_digest_invalidates_prose_and_fingerprint_changes() -> None:
    from jaunt.typescript.status import _module_freshness_digest

    base = {
        "moduleId": "ts:src/token",
        "structuralDigest": "same-structure",
        "sidecar": {
            "moduleId": "ts:src/token",
            "structuralDigest": "same-structure",
            "proseDigest": "prose-a",
            "fingerprint": "compiler-a",
        },
    }
    prose = {
        **base,
        "sidecar": {**base["sidecar"], "proseDigest": "prose-b"},
    }
    fingerprint = {
        **base,
        "sidecar": {**base["sidecar"], "fingerprint": "compiler-b"},
    }

    assert _module_freshness_digest(base) != _module_freshness_digest(prose)
    assert _module_freshness_digest(base) != _module_freshness_digest(fingerprint)
