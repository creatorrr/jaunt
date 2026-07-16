from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.cli import main
from jaunt.config import JauntConfig, load_config
from jaunt.errors import JauntConfigError, JauntGenerationError
from jaunt.typescript.builder import _Write
from jaunt.typescript.migrate import (
    TypeScriptMigrationAction,
    TypeScriptMigrationDiagnostic,
    TypeScriptMigrationPlan,
    _legacy_layout_plan,
    apply_typescript_migration,
    plan_typescript_migration,
)
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    PROTOCOL_VERSION,
    WorkspaceStamp,
)
from jaunt.typescript.reuse import proven_previous_target_api_digests
from jaunt.typescript.worker import REQUIRED_WORKER_CAPABILITIES


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _config(root: Path) -> JauntConfig:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
tool_owner = "."

[codex]
model = "gpt-5.6-sol"
""",
        encoding="utf-8",
    )
    return load_config(root=root)


class _MigrationWorker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.spec_source = (
            'import * as jaunt from "@usejaunt/ts/spec";\n'
            "jaunt.magicModule();\n"
            "/** Double a number. */\n"
            "export function double(value: number): number { return jaunt.magic(); }\n"
        )
        (root / "src/math.jaunt.ts").write_text(self.spec_source, encoding="utf-8")
        self.api = (
            "/** Double a number. */\nexport declare function double(value: number): number;\n"
        )
        self.placeholder = (
            "// jaunt:state=unbuilt\n"
            "export function double(): never { throw new Error('unbuilt'); }\n"
        )
        self.sidecar_value: dict[str, Any] = {
            "schema": "contract-ir/1-draft.3",
            "moduleId": "ts:src/math",
            "specPath": "src/math.jaunt.ts",
            "facadePath": "src/math.ts",
            "apiMirrorPath": "src/__generated__/math.api.ts",
            "implementationPath": "src/__generated__/math.ts",
            "project": "tsconfig.json",
            "packageOwner": ".",
            "dependencies": [],
            "options": {},
            "symbols": [{"name": "double", "kind": "function"}],
            "typeDeclarations": [],
            "typeImports": [],
            "contextDocs": [],
            "semanticEnvironmentDigest": "sha256:semantic-environment",
            "semanticEnvironmentRecords": [
                {
                    "id": "package:@fixture/contracts",
                    "digest": "sha256:declaration-v1",
                }
            ],
            "toolingProvenanceRecords": [],
            "structuralDigest": "sha256:structural",
            "proseDigest": "sha256:prose",
            "apiDigest": "sha256:api",
            "fingerprint": {
                "toolVersion": "current",
                "protocol": "jaunt-ts/1-draft.3",
                "ir": "contract-ir/1-draft.3",
            },
        }
        self.module: dict[str, Any] = {
            **self.sidecar_value,
            "sidecarPath": "src/__generated__/math.jaunt.json",
            "apiSource": self.api,
            "placeholderSource": self.placeholder,
            "sidecar": self._expected_sidecar(),
            "specSource": self.spec_source,
        }
        self.installation = SimpleNamespace(
            compiler_module_path=root / "node_modules/typescript/lib/typescript.js",
            package_root=root,
            node="node",
        )
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _expected_sidecar(self) -> str:
        return json.dumps(self.sidecar_value, sort_keys=True) + "\n"

    def refresh_expected_sidecar(self) -> None:
        self.module["sidecar"] = self._expected_sidecar()

    async def __aenter__(self) -> _MigrationWorker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialize(self, _params: InitializeParams) -> InitializeResult:
        return InitializeResult(
            worker_version="0.1.0",
            protocol=PROTOCOL_VERSION,
            typescript_version="6.0.2",
            capabilities=REQUIRED_WORKER_CAPABILITIES,
            stamp=WorkspaceStamp(
                "migration-session",
                1,
                "migration-snapshot",
                {"src/math.jaunt.ts": _digest(self.spec_source)},
            ),
        )

    def _stamp(self) -> dict[str, Any]:
        return {
            "sessionId": "migration-session",
            "epoch": 1,
            "snapshot": "migration-snapshot",
            "inputHashes": {"src/math.jaunt.ts": _digest(self.spec_source)},
        }

    @staticmethod
    def _artifact(path: str, content: str, kind: str) -> dict[str, str]:
        return {
            "path": path,
            "content": content,
            "sha256": _digest(content),
            "kind": kind,
            "moduleId": "ts:src/math",
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
                "contracts": [],
                "diagnostics": [],
            }
        if method == "analyzeContracts":
            return {**self._stamp(), "modules": [self.module]}
        if method != "validateOverlay":
            raise AssertionError(method)

        implementation_path = self.root / "src/__generated__/math.ts"
        implementation = (
            implementation_path.read_text(encoding="utf-8")
            if implementation_path.is_file()
            else self.placeholder
        )
        restamp = bool(params.get("restampModuleIds"))
        recompose = bool(params.get("recomposeModuleIds"))
        if recompose:
            implementation = (
                "// jaunt:state=built\n"
                "// jaunt:module=ts:src/math\n"
                f"// jaunt:structural={self.sidecar_value['structuralDigest']}\n"
                f"// jaunt:prose={self.sidecar_value['proseDigest']}\n"
                f"// jaunt:api={self.sidecar_value['apiDigest']}\n"
                "const __jaunt_impl_double = (value: number): number => value * 2;\n"
                'Object.defineProperty(__jaunt_impl_double, "name", '
                '{ value: "double", configurable: true });\n'
            )
        facade = 'export * from "./__generated__/math.js";\n'
        sidecar = dict(self.sidecar_value)
        sidecar.update(
            {
                "state": "built" if "jaunt:state=built" in implementation else "unbuilt",
                "artifactHashes": {
                    "src/math.ts": _digest(facade),
                    "src/__generated__/math.api.ts": _digest(self.api),
                    "src/__generated__/math.ts": _digest(implementation),
                },
            }
        )
        artifacts = [
            self._artifact("src/math.ts", facade, "facade"),
            self._artifact("src/__generated__/math.api.ts", self.api, "api-mirror"),
            self._artifact(
                "src/__generated__/math.jaunt.json",
                json.dumps(sidecar, sort_keys=True) + "\n",
                "sidecar",
            ),
        ]
        if not implementation_path.exists() or restamp or recompose:
            artifacts.append(
                self._artifact(
                    "src/__generated__/math.ts",
                    implementation,
                    "implementation" if (restamp or recompose) else "placeholder",
                )
            )
        return {
            **self._stamp(),
            "valid": True,
            "artifacts": artifacts,
            "diagnostics": [],
            "affectedProjects": ["tsconfig.json"],
        }


def _write_built_artifacts(
    root: Path,
    worker: _MigrationWorker,
    *,
    schema: str = "contract-ir/1-draft.3",
    tool_version: str = "current",
) -> None:
    generated = root / "src/__generated__"
    generated.mkdir()
    facade = 'export * from "./__generated__/math.js";\n'
    implementation = (
        "// jaunt:state=built\n"
        "// jaunt:module=ts:src/math\n"
        "// jaunt:structural=sha256:structural\n"
        "// jaunt:prose=sha256:prose\n"
        "// jaunt:api=sha256:api\n"
        "const __jaunt_impl_double = (value: number): number => value * 2;\n"
    )
    (root / "src/math.ts").write_text(facade, encoding="utf-8")
    (generated / "math.api.ts").write_text(worker.api, encoding="utf-8")
    (generated / "math.ts").write_text(implementation, encoding="utf-8")
    sidecar = {
        **worker.sidecar_value,
        "schema": schema,
        "fingerprint": {
            **worker.sidecar_value["fingerprint"],
            "toolVersion": tool_version,
        },
        "state": "built",
        "artifactHashes": {
            "src/math.ts": _digest(facade),
            "src/__generated__/math.api.ts": _digest(worker.api),
            "src/__generated__/math.ts": _digest(implementation),
        },
    }
    (generated / "math.jaunt.json").write_text(
        json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8"
    )


@pytest.mark.asyncio
async def test_typescript_migrate_repairs_artifacts_model_free_and_is_idempotent(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.blocked
    assert {action.kind for action in plan.actions} == {
        "api-mirror",
        "facade",
        "placeholder",
        "sidecar",
    }
    assert all(action.classification == "deterministic-rewrite" for action in plan.actions)
    assert not (tmp_path / "src/math.ts").exists()

    applied = apply_typescript_migration(plan)

    assert set(applied) == {action.path for action in plan.actions}
    assert "state=unbuilt" in (tmp_path / "src/__generated__/math.ts").read_text()
    again = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)
    assert again.actions == ()
    assert again.writes == ()
    assert all(method != "generate" for method, _params in worker.requests)


@pytest.mark.asyncio
async def test_typescript_migrate_recomposes_compatible_fingerprint_drift(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker, tool_version="old")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.requires_rebuild
    assert any(action.classification == "free-recompose" for action in plan.actions)
    validation = [params for method, params in worker.requests if method == "validateOverlay"]
    assert validation[-1]["recomposeModuleIds"] == ["ts:src/math"]
    apply_typescript_migration(plan)
    sidecar = json.loads((tmp_path / "src/__generated__/math.jaunt.json").read_text())
    assert sidecar["fingerprint"]["toolVersion"] == "current"


@pytest.mark.asyncio
async def test_typescript_migrate_requires_rebuild_for_proofless_legacy_sidecar(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar.pop("semanticEnvironmentDigest")
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert plan.requires_rebuild
    assert any(
        diagnostic.code == "JAUNT_TS_MIGRATE_REBUILD_REQUIRED"
        and "environment proof" in diagnostic.message
        for diagnostic in plan.diagnostics
    )
    assert all(method != "validateOverlay" for method, _params in worker.requests)


@pytest.mark.asyncio
async def test_typescript_migrate_recomposes_digest_scheme_upgrade_without_model(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    worker.sidecar_value.update(
        {
            "structuralDigest": "sha256:structural-v2",
            "apiDigest": "sha256:api-v2",
        }
    )
    worker.module.update(worker.sidecar_value)
    worker.refresh_expected_sidecar()

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.requires_rebuild
    assert any(action.classification == "free-recompose" for action in plan.actions)
    validation = [params for method, params in worker.requests if method == "validateOverlay"]
    assert validation[-1]["recomposeModuleIds"] == ["ts:src/math"]
    assert all(method != "generate" for method, _params in worker.requests)

    apply_typescript_migration(plan)
    implementation = (tmp_path / "src/__generated__/math.ts").read_text(encoding="utf-8")
    sidecar = json.loads(
        (tmp_path / "src/__generated__/math.jaunt.json").read_text(encoding="utf-8")
    )
    assert "jaunt:structural=sha256:structural-v2" in implementation
    assert sidecar["structuralDigest"] == "sha256:structural-v2"


@pytest.mark.asyncio
async def test_typescript_migrate_recomposes_known_draft_upgrade_without_model(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["schema"] = "contract-ir/1-draft.2"
    sidecar["fingerprint"]["protocol"] = "jaunt-ts/1-draft.2"
    sidecar["fingerprint"]["ir"] = "contract-ir/1-draft.2"
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.requires_rebuild
    assert any(action.classification == "free-recompose" for action in plan.actions)
    apply_typescript_migration(plan)
    migrated = json.loads(sidecar_path.read_text(encoding="utf-8"))
    assert migrated["schema"] == "contract-ir/1-draft.3"
    assert migrated["fingerprint"]["protocol"] == "jaunt-ts/1-draft.3"


@pytest.mark.asyncio
async def test_typescript_migrate_recomposes_environment_drift_and_preserves_batteries(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    worker.sidecar_value.update(
        {
            "semanticEnvironmentDigest": "sha256:semantic-environment-v2",
            "semanticEnvironmentRecords": [
                {
                    "id": "package:@fixture/contracts",
                    "digest": "sha256:declaration-v2",
                }
            ],
            "structuralDigest": "sha256:environment-structure-v2",
            "apiDigest": "sha256:environment-api-v2",
        }
    )
    worker.module.update(worker.sidecar_value)
    worker.refresh_expected_sidecar()

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.requires_rebuild
    assert any(action.classification == "free-recompose" for action in plan.actions)
    diagnostic = next(
        item for item in plan.diagnostics if item.code == "JAUNT_TS_MIGRATE_ENVIRONMENT_RECOMPOSE"
    )
    assert diagnostic.data["changed"] == ["package:@fixture/contracts"]
    validation = [params for method, params in worker.requests if method == "validateOverlay"]
    assert validation[-1]["recomposeModuleIds"] == ["ts:src/math"]
    assert all(method != "generate" for method, _params in worker.requests)

    apply_typescript_migration(plan)

    assert proven_previous_target_api_digests(tmp_path, (worker.module,))
    sidecar = json.loads((tmp_path / "src/__generated__/math.jaunt.json").read_text())
    assert sidecar["semanticEnvironmentDigest"] == "sha256:semantic-environment-v2"


@pytest.mark.asyncio
async def test_typescript_migrate_reports_package_manager_as_tooling_provenance(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    worker.sidecar_value.update(
        {
            "toolingProvenanceRecords": [
                {
                    "id": "tooling:packageManager:package.json",
                    "digest": "sha256:pnpm-11.5.0",
                },
            ],
            "structuralDigest": "sha256:package-manager-structure-v2",
            "apiDigest": "sha256:package-manager-api-v2",
        }
    )
    worker.module.update(worker.sidecar_value)
    worker.refresh_expected_sidecar()

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert not plan.requires_rebuild
    assert any(action.classification == "free-recompose" for action in plan.actions)
    diagnostic = next(
        item for item in plan.diagnostics if item.code == "JAUNT_TS_MIGRATE_ENVIRONMENT_RECOMPOSE"
    )
    assert diagnostic.data["added"] == ["tooling:packageManager:package.json"]
    assert diagnostic.data["before_digest"] == diagnostic.data["after_digest"]
    assert all(method != "generate" for method, _params in worker.requests)


@pytest.mark.asyncio
async def test_typescript_migrate_environment_drift_does_not_hide_contract_change(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    worker.sidecar_value.update(
        {
            "semanticEnvironmentDigest": "sha256:semantic-environment-v2",
            "structuralDigest": "sha256:changed-contract",
            "apiDigest": "sha256:changed-api",
            "symbols": [{"name": "double", "kind": "function", "async": True}],
        }
    )
    worker.module.update(worker.sidecar_value)
    worker.refresh_expected_sidecar()

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert plan.requires_rebuild == ("ts:src/math",)
    assert all(action.classification != "free-recompose" for action in plan.actions)
    assert all(method != "validateOverlay" for method, _params in worker.requests)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("relative", "kind"),
    [
        ("src/math.ts", "facade"),
        ("src/__generated__/math.api.ts", "api-mirror"),
    ],
)
async def test_typescript_migrate_repairs_built_deterministic_artifact_drift(
    tmp_path: Path, relative: str, kind: str
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    artifact = tmp_path / relative
    expected = artifact.read_bytes()
    implementation = tmp_path / "src/__generated__/math.ts"
    implementation_before = implementation.read_bytes()
    artifact.write_text("// edited deterministic artifact\n", encoding="utf-8")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    action = next(action for action in plan.actions if action.kind == kind)
    assert action.classification == "deterministic-rewrite"
    apply_typescript_migration(plan)
    assert artifact.read_bytes() == expected
    assert implementation.read_bytes() == implementation_before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "old_value"),
    [
        ("schema", "contract-ir/0-preview"),
        ("protocol", "jaunt-ts/0-preview"),
        ("ir", "contract-ir/0-preview"),
    ],
)
async def test_typescript_migrate_requires_rebuild_for_incompatible_alpha_schemes(
    tmp_path: Path, field: str, old_value: str
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    if field == "schema":
        sidecar[field] = old_value
    else:
        sidecar["fingerprint"][field] = old_value
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8")
    before = sidecar_path.read_bytes()

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert plan.requires_rebuild == ("ts:src/math",)
    assert plan.actions[0].classification == "model-rebuild"
    assert plan.diagnostics[0].code == "JAUNT_TS_MIGRATE_ALPHA_SCHEME_INCOMPATIBLE"
    assert plan.writes == ()
    with pytest.raises(JauntConfigError, match="requires model rebuilds"):
        apply_typescript_migration(plan)
    assert sidecar_path.read_bytes() == before


@pytest.mark.asyncio
async def test_typescript_migrate_reports_incompatible_built_route_without_guessing(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    _write_built_artifacts(tmp_path, worker)
    sidecar_path = tmp_path / "src/__generated__/math.jaunt.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["implementationPath"] = "src/__generated__/impl.ts"
    sidecar_path.write_text(json.dumps(sidecar, sort_keys=True) + "\n", encoding="utf-8")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)

    assert plan.requires_rebuild == ("ts:src/math",)
    assert plan.diagnostics[0].code == "JAUNT_TS_MIGRATE_LAYOUT_INCOMPATIBLE"
    assert "implementationPath" in plan.diagnostics[0].message
    assert plan.writes == ()


@pytest.mark.asyncio
async def test_typescript_migrate_rejects_stale_plan_before_writing(tmp_path: Path) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)
    spec = tmp_path / "src/math.jaunt.ts"
    spec.write_text(spec.read_text() + "// concurrent edit\n", encoding="utf-8")

    with pytest.raises(JauntGenerationError, match="inputs changed after analysis"):
        apply_typescript_migration(plan)

    assert not (tmp_path / "src/math.ts").exists()
    assert not (tmp_path / "src/__generated__/math.ts").exists()


@pytest.mark.asyncio
async def test_typescript_migrate_rolls_back_partial_atomic_apply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path)
    worker = _MigrationWorker(tmp_path)
    plan = await plan_typescript_migration(tmp_path, config, worker_factory=lambda *_: worker)
    destinations = sorted(tmp_path / write.path for write in plan.writes)
    original_replace = os.replace

    def fail_second(source: str | Path, destination: str | Path) -> None:
        if Path(destination) == destinations[1]:
            raise OSError("migration replacement failed")
        original_replace(source, destination)

    monkeypatch.setattr("jaunt.typescript.builder.os.replace", fail_second)

    with pytest.raises(OSError, match="migration replacement failed"):
        apply_typescript_migration(plan)

    assert not any(path.exists() for path in destinations)
    assert not tuple((tmp_path / ".jaunt/transactions").glob("*.json"))


@pytest.mark.asyncio
async def test_typescript_migrate_does_not_guess_ambiguous_preview_layout(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    preview = tmp_path / "src/tokens"
    generated = preview / "__generated__"
    generated.mkdir(parents=True)
    (preview / "spec.jaunt.ts").write_text(
        'import { magic } from "@usejaunt/ts";\nexport const value = magic();\n',
        encoding="utf-8",
    )
    (preview / "index.ts").write_text('export * from "./__generated__/impl.js";\n')
    (generated / "impl.ts").write_text("export const value = 1;\n")

    def unexpected_worker(*_args: object) -> object:
        raise AssertionError("ambiguous legacy layouts must not reach the worker")

    plan = await plan_typescript_migration(tmp_path, config, worker_factory=unexpected_worker)

    assert plan.blocked
    assert plan.actions[0].classification == "manual-intervention"
    assert plan.diagnostics[0].code == "JAUNT_TS_MIGRATE_LAYOUT_AMBIGUOUS"
    with pytest.raises(JauntConfigError, match="manual intervention"):
        apply_typescript_migration(plan)
    assert (preview / "spec.jaunt.ts").is_file()
    assert (generated / "impl.ts").read_text() == "export const value = 1;\n"


def test_typescript_migrate_accepts_supported_root_marker_import(tmp_path: Path) -> None:
    config = _config(tmp_path)
    (tmp_path / "src/value.jaunt.ts").write_text(
        'import * as jaunt from "@usejaunt/ts";\n'
        "jaunt.magicModule();\n"
        "export function value(): number { return jaunt.magic(); }\n",
        encoding="utf-8",
    )

    actions, diagnostics, _inputs = _legacy_layout_plan(tmp_path, config)

    assert actions == ()
    assert diagnostics == ()


def test_typescript_only_plain_migrate_routes_to_ts_and_emits_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(tmp_path)
    action = TypeScriptMigrationAction(
        module_id="ts:src/math",
        path="src/math.ts",
        kind="facade",
        classification="deterministic-rewrite",
        description="repair facade",
    )
    plan = TypeScriptMigrationPlan(
        root=tmp_path,
        actions=(action,),
        diagnostics=(),
        expected_inputs={},
        writes=(),
        plan_digest="sha256:plan",
    )

    async def fake_plan(root: Path, config: JauntConfig) -> TypeScriptMigrationPlan:
        assert root == tmp_path
        assert config.target_languages == ("ts",)
        return plan

    monkeypatch.setattr("jaunt.typescript.migrate.plan_typescript_migration", fake_plan)

    assert main(["migrate", "--root", str(tmp_path), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["language"] == "ts"
    assert payload["applied"] is False
    assert payload["actions"][0]["classification"] == "deterministic-rewrite"


def test_typescript_migrate_apply_obeys_dirty_guard_and_force(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(tmp_path)
    write = _Write("src/math.ts", "export {};\n", "facade", "ts:src/math")
    plan = TypeScriptMigrationPlan(
        root=tmp_path,
        actions=(
            TypeScriptMigrationAction(
                module_id="ts:src/math",
                path=write.path,
                kind=write.kind,
                classification="deterministic-rewrite",
                description="repair facade",
            ),
        ),
        diagnostics=(),
        expected_inputs={write.path: "<missing>"},
        writes=(write,),
        plan_digest="sha256:plan",
    )

    async def fake_plan(_root: Path, _config: JauntConfig) -> TypeScriptMigrationPlan:
        return plan

    monkeypatch.setattr("jaunt.typescript.migrate.plan_typescript_migration", fake_plan)
    monkeypatch.setattr("jaunt.cli._is_dirty_worktree", lambda _root: True)

    command = ["migrate", "--language", "ts", "--root", str(tmp_path), "--apply", "--json"]
    assert main(command) == 2
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False
    assert refused["error"] == "dirty working tree"
    assert not (tmp_path / write.path).exists()

    assert main([*command[:-1], "--force", "--json"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["applied"] is True
    assert applied["applied_paths"] == [write.path]
    assert ".jaunt-vitest-cache/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert (tmp_path / write.path).read_text() == "export {};\n"


def test_typescript_migrate_apply_refuses_partial_plan_with_model_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _config(tmp_path)
    write = _Write("src/math.ts", "export {};\n", "facade", "ts:src/math")
    diagnostic = TypeScriptMigrationDiagnostic(
        code="JAUNT_TS_MIGRATE_REBUILD_REQUIRED",
        message="contract changed",
        classification="model-rebuild",
        module_id="ts:src/other",
    )
    plan = TypeScriptMigrationPlan(
        root=tmp_path,
        actions=(
            TypeScriptMigrationAction(
                module_id="ts:src/math",
                path=write.path,
                kind=write.kind,
                classification="deterministic-rewrite",
                description="repair facade",
            ),
        ),
        diagnostics=(diagnostic,),
        expected_inputs={write.path: "<missing>"},
        writes=(write,),
        plan_digest="sha256:plan",
    )

    async def fake_plan(_root: Path, _config: JauntConfig) -> TypeScriptMigrationPlan:
        return plan

    monkeypatch.setattr("jaunt.typescript.migrate.plan_typescript_migration", fake_plan)
    command = [
        "migrate",
        "--language",
        "ts",
        "--root",
        str(tmp_path),
        "--apply",
        "--force",
        "--json",
    ]

    assert main(command) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["requires_rebuild"] == ["ts:src/other"]
    assert "requires model rebuilds" in payload["error"]
    assert not (tmp_path / write.path).exists()
