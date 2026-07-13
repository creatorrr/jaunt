from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from jaunt.config import load_config
from jaunt.targets.base import (
    Language,
    TargetArtifact,
    TargetBuildReport,
    TargetCheckReport,
    TargetRequest,
    TargetStatus,
    TargetTestReport,
    TargetWorkspace,
)
from jaunt.targets.orchestrator import TargetOrchestrator, aggregate_exit_code
from jaunt.targets.python import PythonTargetAdapter, PythonTargetServices
from jaunt.targets.typescript import TypeScriptTargetAdapter
from jaunt.typescript.artifacts import artifact_plan, commit_artifact_plan
from jaunt.typescript.protocol import OverlayArtifact, ProtocolValidationError
from jaunt.typescript.workspace import TypeScriptWorkspace


def _request(tmp_path: Path) -> TargetRequest:
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    return TargetRequest(root=tmp_path, config=load_config(root=tmp_path))


def _services(language: Language, exit_code: int) -> PythonTargetServices:
    return PythonTargetServices(
        discover=lambda request: TargetWorkspace(language=language),
        status=lambda request: TargetStatus(language=language),
        build=lambda request: TargetBuildReport(language=language, exit_code=exit_code),
        test=lambda request: TargetTestReport(language=language),
        check=lambda request: TargetCheckReport(language=language),
        find_orphans=lambda request: (),
    )


def test_orchestrator_applies_documented_exit_precedence(tmp_path: Path) -> None:
    py = PythonTargetAdapter(_services("py", 4))
    ts = TypeScriptTargetAdapter(_services("ts", 2))
    result = asyncio.run(TargetOrchestrator([py, ts]).build(_request(tmp_path)))
    assert result.exit_code == 2
    assert tuple(result.targets) == ("py", "ts")
    assert aggregate_exit_code([0, 4, 3]) == 3


def test_python_adapter_accepts_sync_and_async_service_seams(tmp_path: Path) -> None:
    async def build(request: TargetRequest) -> TargetBuildReport:
        return TargetBuildReport(language="py", generated=frozenset({"pkg.spec"}))

    services = _services("py", 0)
    adapter = PythonTargetAdapter(
        PythonTargetServices(
            discover=services.discover,
            status=services.status,
            build=build,
            test=services.test,
            check=services.check,
            find_orphans=lambda request: (
                TargetArtifact(path=request.root / "old.py", kind="generated"),
            ),
        )
    )
    report = asyncio.run(adapter.build(_request(tmp_path)))
    assert report.generated == frozenset({"pkg.spec"})


def test_workspace_parser_accepts_canonical_route_fields() -> None:
    workspace = TypeScriptWorkspace.from_wire(
        {
            "sessionId": "s",
            "epoch": 1,
            "snapshot": "snap",
            "inputHashes": {},
            "projects": [{"id": "app", "configPath": "tsconfig.json", "role": "production"}],
            "routes": [
                {
                    "moduleId": "ts:src/token",
                    "project": "app",
                    "packageOwner": ".",
                    "specPath": "src/token.jaunt.ts",
                    "facadePath": "src/token.ts",
                    "apiMirrorPath": "src/__generated__/token.api.ts",
                    "implementationPath": "src/__generated__/token.ts",
                    "sidecarPath": "src/__generated__/token.ts.contract.json",
                }
            ],
            "specs": [],
            "testSpecs": [],
            "contracts": [],
            "diagnostics": [],
        }
    )
    route = workspace.routes[0]
    assert route.api_path.endswith("token.api.ts")
    assert route.project_id == "app"
    assert route.sidecar_path.endswith("contract.json")


def test_artifact_plan_checks_hash_containment_and_commits(tmp_path: Path) -> None:
    content = "export const answer = 42;\n"
    digest = hashlib.sha256(content.encode()).hexdigest()
    artifact = OverlayArtifact(
        path="src/__generated__/answer.ts",
        content=content,
        sha256=digest,
        kind="implementation",
        module_id="ts:src/answer",
    )
    plan = artifact_plan(tmp_path, (artifact,))
    commit_artifact_plan(plan)
    assert (tmp_path / artifact.path).read_text(encoding="utf-8") == content
    assert not plan.manifest_path.exists()

    with pytest.raises(ProtocolValidationError, match="safe root-relative"):
        artifact_plan(
            tmp_path,
            (
                OverlayArtifact(
                    path="../escape.ts",
                    content=content,
                    sha256=digest,
                    kind="implementation",
                    module_id="ts:escape",
                ),
            ),
        )
