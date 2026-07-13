from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaunt.typescript.protocol import (
    PROTOCOL_VERSION,
    InitializeParams,
    InitializeResult,
    ProjectContractResult,
    ProtocolRequest,
    ProtocolResponse,
    ProtocolValidationError,
    ValidateOverlayParams,
    ValidateOverlayResult,
)


def _stamp() -> dict[str, object]:
    return {
        "sessionId": "session-1",
        "epoch": 2,
        "snapshot": "sha256:snapshot",
        "inputHashes": {"tsconfig.json": "sha256:config"},
    }


@pytest.mark.parametrize(
    ("stem", "method"),
    [
        ("initialize", "initialize"),
        ("analyze-workspace", "analyzeWorkspace"),
        ("analyze-contracts", "analyzeContracts"),
        ("project-contract", "projectContract"),
        ("validate-overlay", "validateOverlay"),
        ("find-orphans", "findOrphans"),
        ("invalidate", "invalidate"),
        ("cancel", "cancel"),
        ("shutdown", "shutdown"),
    ],
)
def test_python_envelope_consumes_every_shared_method_fixture(stem: str, method: str) -> None:
    fixture_root = Path(__file__).parents[1] / "schemas" / "jaunt-ts" / "fixtures"
    request = json.loads((fixture_root / f"{stem}.request.json").read_text(encoding="utf-8"))
    assert request["protocol"] == PROTOCOL_VERSION
    assert request["method"] == method
    assert isinstance(request["params"], dict)

    response = ProtocolResponse.from_wire(
        json.loads((fixture_root / f"{stem}.response.json").read_text(encoding="utf-8"))
    )
    assert response.ok is True
    assert response.result is not None


def test_request_uses_pinned_jsonl_wire_names() -> None:
    request = ProtocolRequest(
        id="17",
        method="initialize",
        params={"root": "/repo"},
        deadline_ms=500,
    )
    assert request.to_wire() == {
        "protocol": PROTOCOL_VERSION,
        "id": "17",
        "method": "initialize",
        "params": {"root": "/repo"},
        "deadlineMs": 500,
    }


def test_initialize_params_and_result_match_protocol_draft() -> None:
    params = InitializeParams(
        root="/repo",
        projects=("tsconfig.json",),
        test_projects=(),
        source_roots=("src",),
        test_roots=("tests",),
        generated_dir="__generated__",
        tool_owner=".",
        compiler_module_path="/repo/node_modules/typescript/lib/typescript.js",
        client_version="1.0",
        tool_version="0.1",
    )
    assert params.to_wire()["compilerModulePath"].endswith("typescript.js")

    result = InitializeResult.from_wire(
        {
            "workerVersion": "0.1",
            "protocol": PROTOCOL_VERSION,
            "typescriptVersion": "5.9.0",
            "packageManager": "pnpm@10",
            "capabilities": ["analyze", "overlay"],
            **_stamp(),
        }
    )
    assert result.stamp.session_id == "session-1"
    assert result.stamp.epoch == 2
    assert result.typescript_version == "5.9.0"
    assert result.package_manager == "pnpm@10"


def test_python_models_consume_shared_initialize_fixture() -> None:
    fixture_root = Path(__file__).parents[1] / "schemas" / "jaunt-ts" / "fixtures"
    response = ProtocolResponse.from_wire(
        json.loads((fixture_root / "initialize.response.json").read_text(encoding="utf-8"))
    )
    assert response.result is not None
    result = InitializeResult.from_wire(response.result)
    assert result.protocol == PROTOCOL_VERSION
    assert result.package_manager == "npm@11"
    assert result.capabilities == (
        "analyze",
        "overlay",
        "sync",
        "orphans",
        "invalidate",
        "contract-projection",
    )

    request = json.loads((fixture_root / "initialize.request.json").read_text(encoding="utf-8"))
    params = InitializeParams(
        root="/workspace",
        projects=("tsconfig.json",),
        test_projects=(),
        source_roots=("src",),
        test_roots=("tests",),
        generated_dir="__generated__",
        tool_owner=".",
        compiler_module_path="/workspace/node_modules/typescript/lib/typescript.js",
        client_version="1.7.0",
        tool_version="0.1.0-alpha.0",
        generation_fingerprint=(
            "sha256:0000000000000000000000000000000000000000000000000000000000000000"
        ),
    )
    assert request["params"] == params.to_wire()

    failure = ProtocolResponse.from_wire(
        json.loads((fixture_root / "error.response.json").read_text(encoding="utf-8"))
    )
    assert failure.error is not None
    assert failure.error.code == "INVALID_REQUEST"


def test_python_models_consume_shared_project_contract_fixtures() -> None:
    fixture_root = Path(__file__).parents[1] / "schemas" / "jaunt-ts" / "fixtures"
    request = json.loads(
        (fixture_root / "project-contract.request.json").read_text(encoding="utf-8")
    )
    assert request["method"] == "projectContract"
    response = ProtocolResponse.from_wire(
        json.loads((fixture_root / "project-contract.response.json").read_text(encoding="utf-8"))
    )
    assert response.result is not None
    result = ProjectContractResult.from_wire(response.result)
    assert result.symbol == "value"
    assert result.kind == "function"
    assert result.declaration_start == 24
    assert result.declaration_end == 86
    assert (result.docs_start, result.docs_end) == (0, 23)


def test_project_contract_result_rejects_unpaired_or_invalid_ranges() -> None:
    valid = {
        "source": "export function value(): string;\n",
        "sourceDigest": "sha256:" + "0" * 64,
        "symbol": "value",
        "kind": "function",
        "declarationStart": 10,
        "declarationEnd": 20,
    }
    with pytest.raises(ProtocolValidationError, match="provide docsStart and docsEnd together"):
        ProjectContractResult.from_wire({**valid, "docsStart": 0})
    with pytest.raises(ProtocolValidationError, match="precede the declaration"):
        ProjectContractResult.from_wire({**valid, "docsStart": 0, "docsEnd": 11})


def test_response_envelope_rejects_ambiguous_or_untyped_messages() -> None:
    with pytest.raises(ProtocolValidationError, match="must not contain error"):
        ProtocolResponse.from_wire(
            {
                "protocol": PROTOCOL_VERSION,
                "id": "1",
                "ok": True,
                "result": {},
                "error": {},
            }
        )
    with pytest.raises(ProtocolValidationError, match="id"):
        ProtocolResponse.from_wire(
            {"protocol": PROTOCOL_VERSION, "id": 1, "ok": True, "result": {}}
        )


def test_failed_response_preserves_structured_diagnostics() -> None:
    response = ProtocolResponse.from_wire(
        {
            "protocol": PROTOCOL_VERSION,
            "id": "3",
            "ok": False,
            "error": {
                "code": "TS_CONFIG",
                "message": "bad config",
                "retryable": False,
                "diagnostics": [
                    {
                        "code": "TS100",
                        "severity": "error",
                        "message": "nope",
                        "path": "tsconfig.json",
                        "start": 4,
                    }
                ],
            },
        }
    )
    assert response.error is not None
    assert response.error.diagnostics[0].path == "tsconfig.json"
    assert response.error.diagnostics[0].start == 4


def test_validate_overlay_result_carries_commit_fence_and_exact_bytes() -> None:
    result = ValidateOverlayResult.from_wire(
        {
            **_stamp(),
            "valid": True,
            "artifacts": [
                {
                    "path": "src/__generated__/token.ts",
                    "content": "export const token = 1;\n",
                    "sha256": "abc",
                    "kind": "implementation",
                    "moduleId": "ts:src/token",
                }
            ],
            "diagnostics": [],
            "affectedProjects": ["app"],
        }
    )
    assert result.valid is True
    assert result.stamp.snapshot == "sha256:snapshot"
    assert result.artifacts[0].content == "export const token = 1;\n"
    assert result.affected_projects == ("app",)


def test_validate_overlay_params_serialize_deterministic_sync_ids() -> None:
    params = ValidateOverlayParams(
        session_id="session-1",
        expected_epoch=2,
        expected_snapshot="sha256:snapshot",
        candidates={},
        sync_module_ids=("ts:src/token",),
        restamp_module_ids=("ts:src/restamp",),
    )
    assert params.to_wire()["syncModuleIds"] == ["ts:src/token"]
    assert params.to_wire()["restampModuleIds"] == ["ts:src/restamp"]
