from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.cache import ResponseCache
from jaunt.config import JauntConfig, load_config
from jaunt.cost import CostTracker
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.generate.base import (
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
)
from jaunt.targets.base import TargetBuildReport, TargetCheckReport, TargetDiagnostic
from jaunt.typescript.builder import (
    _build_units,
    _build_request,
    _dependency_module_ids,
    _gate_prose_change,
    _generation_fingerprint,
    _topological_modules,
    _Write,
    TypeScriptAnalysis,
    atomic_write_manifest,
    run_build,
    run_sync,
)
from jaunt.typescript.cli_bridge import check_payload, human_lines, status_payload
from jaunt.typescript.contracts import (
    _add_contract_tag,
    _battery_request,
    _battery_path,
    _declaration_only_contract,
    _ordinary_ejected_source,
    _projection_offset,
    _remove_contract_tag,
    _with_header,
    _with_strength_metadata,
    run_adopt,
    run_eject,
)
from jaunt.typescript.design import (
    _design_output_errors,
    _design_ranges,
    _materialize_magic_stubs,
    _prepare_design_manifest,
    _validate_declaration,
    run_design,
)
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    PROTOCOL_VERSION,
    WorkspaceStamp,
)
from jaunt.typescript.status import run_check, run_clean, run_status
from jaunt.typescript.tester import (
    _assert_no_held_out_leak,
    _HeldOutLeakError,
    _implementation_repair_feedback,
    _implicit_class_test_specs,
    _isolated_test_workspace,
    _is_reviewable_example_battery,
    _redact_runner_result,
    _runner_fingerprint,
    _run_test_runner,
    _static_test_validation,
    _strip_test_header,
    _terminate_runner_process,
    _test_header_metadata,
    _test_request,
    _valid_runner_dto,
    _validate_test_owner_dependencies,
    _with_test_header,
    run_test,
)
from jaunt.typescript.worker import REQUIRED_WORKER_CAPABILITIES


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
        self.sidecar = (
            json.dumps(
                {
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
            "schema": "contract-ir/1-draft.2",
            "moduleId": "ts:src/math",
            "specPath": "src/math.jaunt.ts",
            "facadePath": "src/math.ts",
            "apiMirrorPath": "src/__generated__/math.api.ts",
            "implementationPath": "src/__generated__/math.ts",
            "sidecarPath": "src/__generated__/math.jaunt.json",
            "project": "tsconfig.json",
            "packageOwner": ".",
            "symbols": [{"name": "double", "kind": "function"}],
            "typeDeclarations": [],
            "typeImports": [],
            "dependencies": [],
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
            sync_mode = bool(params.get("syncModuleIds")) and real is None
            preserve_mode = sync_mode or restamp_mode
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
            elif implementation_kind == "implementation" and restamp_mode:
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
            if real is not None or existing is None or restamp_mode:
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
            stamp=WorkspaceStamp("schedule", 1, "snapshot", self.input_hashes),
        )

    def _stamp(self) -> dict[str, Any]:
        return {
            "sessionId": "schedule",
            "epoch": 1,
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
        "schema": "contract-ir/1-draft.2",
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

    def fail_second(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == second:
            raise OSError("simulated second replacement failure")
        original_replace(source, destination)

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
from jaunt.typescript.tester import _preserve_managed_files

root = Path(sys.argv[1])
implementation = root / "src/__generated__/math.ts"
battery = root / "tests/__generated__/math.derived.test.ts"
with _preserve_managed_files(root, ["src/__generated__/math.ts"]) as transaction:
    implementation.write_text("unaccepted repair\\n", encoding="utf-8")
    transaction.add_paths(["tests/__generated__/math.derived.test.ts"])
    battery.write_text("partial candidate battery\\n", encoding="utf-8")
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
async def test_build_keeps_failed_owner_transaction_atomic_and_commits_other_owner(
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

    assert report.exit_code == 3
    assert report.generated == frozenset({str(unrelated["moduleId"])})
    assert set(report.failed) == {str(first["moduleId"]), str(second["moduleId"])}
    assert not (tmp_path / str(first["implementationPath"])).exists()
    assert not (tmp_path / str(second["implementationPath"])).exists()
    assert (tmp_path / str(unrelated["implementationPath"])).is_file()


def test_build_units_union_reference_components_and_explicit_dependencies(tmp_path: Path) -> None:
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
        contracts={"modules": [core, app, lone, consumer]},
    )

    units = _build_units(analysis, analysis.modules)

    assert {frozenset(unit.module_ids) for unit in units} == {
        frozenset({str(core["moduleId"]), str(app["moduleId"])}),
        frozenset({str(lone["moduleId"]), str(consumer["moduleId"])}),
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
    worker.module["structuralDigest"] = "sha256:new-structure"
    worker.module["sidecar"] = json.dumps(sidecar, sort_keys=True) + "\n"

    status = await run_status(tmp_path, config, worker_factory=lambda *_: worker)

    assert status.stale == {"ts:src/math": "structural"}


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
async def test_fingerprint_drift_is_revalidated_and_restamped_without_model(
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

    report = await run_build(
        tmp_path,
        config,
        generator=ExplodingGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.generated == frozenset()
    assert report.refrozen == frozenset({"ts:src/math"})
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
        (package / "package.json").write_text('{"name":"@usejaunt/ts","version":"0.1.0-alpha.0"}\n')
        (package / "dist/test/runner.js").write_text("export const runner = 1;\n")
        (package / "dist/test/reporter.js").write_text("export const reporter = 1;\n")
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

    assert _runner_fingerprint(
        override_workspace, override_client, initialized
    ) == _runner_fingerprint(installed_workspace, installed_client, initialized)


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
    assert protected["captured"] == {"stdout": "", "stderr": ""}
    assert "CHILD-PROCESS-SENTINEL" not in json.dumps(protected)


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
                "specSource": "export function token(): string;",
                "apiSource": "export declare function token(): string;",
            }
        },
    )

    assert request.target_path == "tests/__generated__/tokens.example.test.ts"
    assert "../../src/tokens/index.js" in request.prompt
    assert "src/tokens/index.ts" not in request.prompt

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
        }
    }

    request = _test_request(
        tmp_path,
        config,
        {"path": spec_path, "targets": ["token"]},
        modules,
    )

    assert request.context_files["_context/fixtures.ts"].startswith("import { test as base }")
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
    )

    assert report.exit_code == 3
    assert captured["build_instructions"] == ("Keep it small.",)
    assert captured["semantic_gate_enabled"] is False
    assert captured["force"] is True


@pytest.mark.asyncio
async def test_failed_vitest_run_repairs_once_with_protected_feedback_and_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _TestSpecWorker(tmp_path)
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation.parent.mkdir(parents=True)
    implementation.write_text("old implementation bytes\n")
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

    monkeypatch.setattr("jaunt.typescript.tester.run_build", fake_build)
    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", batches)
    report = await run_test(
        tmp_path,
        config,
        generator=FakeGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 0
    assert len(build_calls) == 2
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
async def test_failed_test_and_repair_preserve_batteries_and_do_not_fill_cache(
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
    assert cache.info()["entries"] == 0


@pytest.mark.asyncio
async def test_passing_test_candidate_commits_disk_then_cache_after_vitest(
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
            assert cache.info()["entries"] == 0
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
    assert cache.info()["entries"] == 0


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

    async def fake_build(*_args: Any, **kwargs: Any) -> TargetBuildReport:
        build_calls.append(dict(kwargs))
        return TargetBuildReport(language="ts", metadata={"cost": _cost(prompt=1, completion=1)})

    async def batches(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        if kwargs.get("typecheck_only"):
            return {"ok": True, "mode": "typecheck", "tests": []}
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


@pytest.mark.asyncio
async def test_test_without_specs_or_batteries_is_green_and_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = FakeWorker(tmp_path)

    async def unexpected_runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("empty test selection must not start Vitest")

    monkeypatch.setattr("jaunt.typescript.tester._run_test_runner", unexpected_runner)
    report = await run_test(
        tmp_path,
        config,
        no_build=True,
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
                return {**self._stamp(), "modules": [self.module, triple]}
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
    assert [call[1] for call in calls[:2]] == [
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    ]
    assert {call[1] for call in calls[2:]} == {
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    }
    assert {call[5] for call in calls[2:]} == {"example", "derived"}
    assert all(
        call[4]
        == (
            "packages/a/tsconfig.test.json",
            "packages/b/tsconfig.test.json",
        )
        for call in calls
    )
    assert all(not call[3] for call in calls)
    assert contract_battery.relative_to(tmp_path).as_posix() in calls[0][2]
    assert set(report.runner["batches"]) == {
        "packages/a/tsconfig.test.json",
        "packages/b/tsconfig.test.json",
    }

    calls.clear()
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
        for field in (
            "test_spec_digest",
            "target_api_digest",
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
    runner = tmp_path / "dist/test/runner.js"
    runner.parent.mkdir(parents=True)
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
