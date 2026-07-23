from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import signal
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.config import JauntConfig, load_config
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import (
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
)
from jaunt.typescript.cli_bridge import human_lines, lifecycle_payload
from jaunt.typescript.contracts import (
    _MUTATION_SCHEME,
    _MUTATION_TIMEOUT_SECONDS,
    _battery_body_digest_issue,
    _battery_header_metadata,
    _battery_path,
    _parse_strength_metadata,
    _run_mutation_strength,
    _terminate_mutation_process,
    _with_header,
    _with_strength_metadata,
    run_adopt,
    run_reconcile,
)
from jaunt.typescript.builder import MISSING_INPUT
from jaunt.typescript.protocol import (
    InitializeParams,
    InitializeResult,
    PROTOCOL_VERSION,
    WorkspaceStamp,
)
from jaunt.typescript.status import run_check
from jaunt.typescript.tester import _fixture_resolution_preconditions
from jaunt.typescript.worker import REQUIRED_WORKER_CAPABILITIES, WorkerToolchainChangedError


def _digest(value: str) -> str:
    return f"sha256:{hashlib.sha256(value.encode()).hexdigest()}"


def _utf16_offset(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _projection_ranges(source: str, symbol: str) -> dict[str, int]:
    declaration = source.rfind(f"export function {symbol}")
    assert declaration >= 0
    result = {
        "declarationStart": _utf16_offset(source[:declaration]),
        "declarationEnd": _utf16_offset(source),
    }
    docs_start = source.rfind("/**", 0, declaration)
    if docs_start >= 0:
        docs_end = source.find("*/", docs_start, declaration)
        if docs_end >= 0 and not source[docs_end + 2 : declaration].strip():
            result["docsStart"] = _utf16_offset(source[:docs_start])
            result["docsEnd"] = _utf16_offset(source[: docs_end + 2])
    return result


def _with_no_fixture_provenance(
    root: Path,
    battery: Path,
    source: str,
    source_path: str,
    source_digest: str,
) -> str:
    topology = _fixture_resolution_preconditions(
        root,
        battery.relative_to(root).as_posix(),
    )
    return _with_header(
        source,
        source_path,
        source_digest,
        fixture_path=MISSING_INPUT,
        fixture_digest=MISSING_INPUT,
        fixture_topology=json.dumps(
            dict(sorted(topology.items())),
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def test_default_mutation_budget_covers_typecheck_and_runner_startup() -> None:
    assert _MUTATION_TIMEOUT_SECONDS == 15.0


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_posix_mutation_teardown_terminates_group_then_escalates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Process:
        pid = 4242
        returncode: int | None = None
        wait_calls = 0

        async def wait(self) -> int | None:
            self.wait_calls += 1
            if self.wait_calls == 1:
                raise TimeoutError
            return self.returncode

    process = Process()
    signals: list[tuple[int, signal.Signals]] = []

    def kill_group(pid: int, sent_signal: signal.Signals) -> None:
        signals.append((pid, sent_signal))
        if sent_signal == signal.SIGKILL:
            process.returncode = -signal.SIGKILL

    monkeypatch.setattr("jaunt.typescript.contracts.os.killpg", kill_group)

    await _terminate_mutation_process(process, platform="posix")

    assert signals == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert process.wait_calls == 2


@pytest.mark.asyncio
async def test_mutation_teardown_keeps_the_shared_windows_tree_kill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = SimpleNamespace(returncode=None)
    delegated: list[tuple[object, str | None]] = []

    async def terminate_tree(candidate: object, *, platform: str | None = None) -> None:
        delegated.append((candidate, platform))

    monkeypatch.setattr("jaunt.typescript.contracts._terminate_runner_process", terminate_tree)

    await _terminate_mutation_process(process, platform="nt")

    assert delegated == [(process, "nt")]


@pytest.mark.skipif(os.name != "posix", reason="POSIX process-group behavior")
@pytest.mark.asyncio
async def test_mutation_cancellation_terminates_the_isolated_process_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=True)
    mutation_runner = tmp_path / "dist/test/mutation.js"
    mutation_runner.parent.mkdir(parents=True)
    mutation_runner.write_text("// fake runner\n")

    class Process:
        pid = 4343
        returncode: int | None = None

        def __init__(self) -> None:
            self.communicating = asyncio.Event()
            self.terminated = asyncio.Event()

        async def communicate(self, _payload: bytes) -> tuple[bytes, bytes]:
            self.communicating.set()
            await asyncio.Future()
            raise AssertionError("unreachable")

        async def wait(self) -> int | None:
            await self.terminated.wait()
            return self.returncode

    process = Process()
    spawn_kwargs: dict[str, object] = {}

    async def create_subprocess_exec(*_args: object, **kwargs: object) -> Process:
        spawn_kwargs.update(kwargs)
        return process

    signals: list[tuple[int, signal.Signals]] = []

    def kill_group(pid: int, sent_signal: signal.Signals) -> None:
        signals.append((pid, sent_signal))
        process.returncode = -sent_signal
        process.terminated.set()

    monkeypatch.setattr(
        "jaunt.typescript.contracts.asyncio.create_subprocess_exec", create_subprocess_exec
    )
    monkeypatch.setattr("jaunt.typescript.contracts.os.killpg", kill_group)
    client = SimpleNamespace(
        installation=SimpleNamespace(
            compiler_module_path=tmp_path / "node_modules/typescript/lib/typescript.js",
            package_root=tmp_path,
            node="node",
        )
    )

    task = asyncio.create_task(
        _run_mutation_strength(
            client,
            tmp_path,
            config,
            source_path="src/contract.ts",
            symbol="clamp",
            battery_file="tests/contract/src/contract.clamp.contract.test.ts",
            owner_project="tsconfig.json",
            overlays={},
        )
    )
    await process.communicating.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert spawn_kwargs["start_new_session"] is True
    assert signals == [(4343, signal.SIGTERM)]


@pytest.mark.parametrize(
    "source",
    [
        "\ntest('x', () => {});\n",
        "   test('x', () => {});\n",
        "\r\ntest('x', () => {});\r\n",
    ],
)
def test_contract_body_digest_uses_one_canonical_form(source: str) -> None:
    battery = _with_header(source, "src/contract.ts", "sha256:" + "0" * 64)

    assert _battery_body_digest_issue(battery) is None


def _config(root: Path, *, strength: bool) -> JauntConfig:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "package.json").write_text('{"devDependencies":{"vitest":"^4.0.0"}}\n')
    vitest = root / "node_modules/vitest"
    vitest.mkdir(parents=True)
    (vitest / "package.json").write_text('{"name":"vitest","version":"4.1.10"}\n')
    (root / "tsconfig.json").write_text("{}\n")
    (root / "tsconfig.test.json").write_text("{}\n")
    (root / "jaunt.toml").write_text(
        f"""version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]

[contract]
strength = {str(strength).lower()}

[codex]
model = "gpt-5.6-sol"
"""
    )
    return load_config(root=root)


class _ContractWorker:
    def __init__(self, root: Path) -> None:
        self.root = root
        source = root / "src/contract.ts"
        source.write_text(
            "/** Clamp to zero.\n * @jauntContract\n */\n"
            "export function clamp(value: number): number {\n"
            "  return value < 0 ? 0 : value;\n"
            "}\n"
        )
        self.installation = SimpleNamespace(
            compiler_module_path=root / "node_modules/typescript/lib/typescript.js",
            package_root=root,
            node="node",
        )
        self.hashes = {"src/contract.ts": _digest(source.read_text())}

    async def __aenter__(self) -> _ContractWorker:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def initialize(self, _params: InitializeParams) -> InitializeResult:
        return InitializeResult(
            worker_version="0.1.0",
            protocol=PROTOCOL_VERSION,
            typescript_version="6.0.2",
            capabilities=REQUIRED_WORKER_CAPABILITIES,
            stamp=WorkspaceStamp("strength", 1, "snapshot", self.hashes),
        )

    def _stamp(self) -> dict[str, object]:
        return {
            "sessionId": "strength",
            "epoch": 1,
            "snapshot": "snapshot",
            "inputHashes": self.hashes,
        }

    async def request(self, method: str, _params: dict[str, Any]) -> dict[str, Any]:
        if method == "analyzeWorkspace":
            return {
                **self._stamp(),
                "projects": [
                    {"id": "tsconfig.json"},
                    {"id": "tsconfig.test.json", "role": "test"},
                ],
                "routes": [],
                "specs": [],
                "testSpecs": [],
                "contracts": [
                    {
                        "path": "src/contract.ts",
                        "project": "tsconfig.json",
                        "symbols": ["clamp"],
                    }
                ],
                "diagnostics": [],
            }
        if method == "analyzeContracts":
            return {**self._stamp(), "modules": []}
        if method == "projectContract":
            source = str(_params["source"])
            return {
                "source": "export function clamp(value: number): number;\n",
                "sourceDigest": _digest(source),
                "symbol": "clamp",
                "kind": "function",
                **_projection_ranges(source, "clamp"),
            }
        if method == "findOrphans":
            return {**self._stamp(), "artifacts": []}
        raise AssertionError(method)


class _RuntimeTrackingContractWorker(_ContractWorker):
    def __init__(self, root: Path, *, fail_verify: bool = False) -> None:
        super().__init__(root)
        self.active = False
        self.fail_verify = fail_verify
        self.runtime_events: list[str] = []

    async def __aenter__(self) -> _RuntimeTrackingContractWorker:
        self.active = True
        return self

    async def __aexit__(self, *_args: object) -> None:
        self.active = False

    def pin_full_runtime_identity(self) -> None:
        assert self.active
        self.runtime_events.append("pin")

    def verify_runtime_identity(self) -> None:
        assert self.active
        self.runtime_events.append("verify")
        if self.fail_verify:
            raise WorkerToolchainChangedError("simulated contract runtime drift")

    def seal_runtime_identity(self) -> None:
        assert self.active
        self.runtime_events.append("seal")


class _BatteryGenerator(GeneratorBackend):
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
        return (
            'import { expect, test } from "vitest";\n'
            'import { clamp } from "../../../src/contract.js";\n'
            'test("clamps", () => expect(clamp(-1)).toBe(0));\n',
            TokenUsage(10, 5, "fake-ts", "fake"),
            (),
        )


def _strength_report(*, survived: bool = False, complete: bool = True) -> dict[str, object]:
    case = {
        "id": "001:comparison:5:16",
        "kind": "comparison",
        "line": 5,
        "column": 16,
        "description": "change a comparison boundary",
        "outcome": "survived" if survived else "killed",
        **({} if survived else {"reason": "test-failed"}),
    }
    killed = [] if survived else [case]
    survivors = [case] if survived else []
    return {
        "protocol": _MUTATION_SCHEME,
        "sourcePath": "src/contract.ts",
        "symbol": "clamp",
        "concurrency": 1,
        "complete": complete,
        "killed": killed,
        "survived": survivors,
        "excluded": [],
        "score": {
            "killed": len(killed),
            "applicable": 1,
            "survived": len(survivors),
            "excluded": 0,
            "ratio": 0.0 if survived else 1.0,
        },
    }


def test_inconsistent_mutation_report_fails_before_metadata_is_written() -> None:
    report = _strength_report()
    score = report["score"]
    assert isinstance(score, dict)
    bad_score = {str(key): value for key, value in score.items()}
    bad_score["killed"] = 2
    report = {**report, "score": bad_score}

    with pytest.raises(JauntGenerationError, match="score does not match"):
        _with_strength_metadata(
            _with_header("export {};\n", "src/contract.ts", "sha256:" + "0" * 64),
            report,
        )


@pytest.mark.asyncio
async def test_reconcile_records_killed_mutants_without_touching_contract_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=True)
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    before = source.read_bytes()

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    async def killed(*_args: Any, **kwargs: Any) -> dict[str, object]:
        assert kwargs["source_path"] == "src/contract.ts"
        assert kwargs["battery_file"] in kwargs["overlays"]
        return _strength_report()

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    monkeypatch.setattr("jaunt.typescript.contracts._run_mutation_strength", killed)
    report = await run_reconcile(
        tmp_path,
        config,
        generator=_BatteryGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.ok
    assert source.read_bytes() == before
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    battery_source = battery.read_text()
    metadata = _parse_strength_metadata(battery_source)
    assert metadata is not None
    assert metadata["killed"] == metadata["applicable"] == 1
    assert _battery_body_digest_issue(battery_source) is None
    payload = lifecycle_payload(report)
    assert payload["strength"]["targets"]["src/contract.ts#clamp"]["score"]["killed"] == 1
    assert any("1/1 killed" in line for line in human_lines(payload))


@pytest.mark.asyncio
async def test_surviving_mutant_blocks_reconcile_and_preserves_old_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=True)
    worker = _ContractWorker(tmp_path)
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_bytes(b"old committed battery\n")
    source = tmp_path / "src/contract.ts"
    source_before = source.read_bytes()
    battery_before = battery.read_bytes()

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    async def survived(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return _strength_report(survived=True)

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    monkeypatch.setattr("jaunt.typescript.contracts._run_mutation_strength", survived)
    report = await run_reconcile(
        tmp_path,
        config,
        generator=_BatteryGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert [item.code for item in report.diagnostics] == ["JAUNT_TS_CONTRACT_MUTANT_SURVIVED"]
    assert "change a comparison boundary" in report.diagnostics[0].message
    assert source.read_bytes() == source_before
    assert battery.read_bytes() == battery_before


@pytest.mark.asyncio
async def test_disabled_strength_skips_mutation_and_writes_plain_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _ContractWorker(tmp_path)

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    async def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        raise AssertionError("strength=false must not start the mutation runner")

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    monkeypatch.setattr("jaunt.typescript.contracts._run_mutation_strength", unexpected)
    report = await run_reconcile(
        tmp_path,
        config,
        generator=_BatteryGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.ok
    assert report.metadata["strength"] == {"enabled": False}
    assert report.metadata["cache"] == {"hits": 0, "misses": 1}
    assert "npm_skills" not in report.metadata
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    assert _parse_strength_metadata(battery.read_text()) is None


@pytest.mark.asyncio
async def test_reconcile_rejects_vitest_config_change_after_battery_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _config(tmp_path, strength=False)
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
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    source_before = source.read_bytes()
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_bytes(b"old committed battery\n")
    battery_before = battery.read_bytes()
    mutated = False

    async def mutate_after_run(*_args: Any, **kwargs: Any) -> dict[str, object]:
        nonlocal mutated
        if not kwargs.get("typecheck_only"):
            setup.write_text('export const setupVersion = "v2";\n', encoding="utf-8")
            mutated = True
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", mutate_after_run)
    with pytest.raises(
        JauntGenerationError,
        match=r"(?:inputs changed.*setup\.ts|Vitest configuration changed)",
    ):
        await run_reconcile(
            tmp_path,
            config,
            generator=_BatteryGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert mutated
    assert source.read_bytes() == source_before
    assert battery.read_bytes() == battery_before


@pytest.mark.parametrize("drift", ["fixture-bytes", "nearer-selection"])
@pytest.mark.asyncio
async def test_reconcile_fixture_drift_aborts_the_atomic_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    from jaunt.typescript import contracts as contracts_module

    config = _config(tmp_path, strength=False)
    worker = _ContractWorker(tmp_path)
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    fixture = (
        tmp_path / "tests/contract/src/fixtures.ts"
        if drift == "fixture-bytes"
        else tmp_path / "tests/contract/fixtures.ts"
    )
    fixture.parent.mkdir(parents=True, exist_ok=True)
    fixture_source = 'export const fixtureVersion = "one";\r\n'
    fixture.write_bytes(fixture_source.encode())

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    real_atomic_write_manifest = contracts_module.atomic_write_manifest

    def drift_before_commit(root: Path, writes: Any, **kwargs: Any) -> Any:
        write = next(item for item in writes if item.path.endswith(".contract.test.ts"))
        assert isinstance(write.content, str)
        metadata = _battery_header_metadata(write.content)
        assert metadata is not None
        assert metadata["fixture_path"] == fixture.relative_to(tmp_path).as_posix()
        assert metadata["fixture_digest"] == _digest(fixture_source)
        topology = json.loads(metadata["fixture_topology"])
        expected_inputs = kwargs["expected_inputs"]
        assert expected_inputs[fixture.relative_to(tmp_path).as_posix()] == _digest(fixture_source)
        if drift == "fixture-bytes":
            assert topology["tests/contract/src/fixtures.tsx"] == MISSING_INPUT
            fixture.write_text('export const fixtureVersion = "two";\n')
        else:
            nearer = tmp_path / "tests/contract/src/fixtures.ts"
            assert topology[nearer.relative_to(tmp_path).as_posix()] == MISSING_INPUT
            assert expected_inputs[nearer.relative_to(tmp_path).as_posix()] == MISSING_INPUT
            nearer.parent.mkdir(parents=True, exist_ok=True)
            nearer.write_text('export const nearerFixture = "new";\n')
        return real_atomic_write_manifest(root, writes, **kwargs)

    monkeypatch.setattr(contracts_module, "_run_test_batches", green_batches)
    monkeypatch.setattr(contracts_module, "atomic_write_manifest", drift_before_commit)

    with pytest.raises(JauntGenerationError, match="inputs changed after analysis"):
        await run_reconcile(
            tmp_path,
            config,
            generator=_BatteryGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert not battery.exists()


@pytest.mark.asyncio
async def test_check_reports_fixture_byte_drift_from_contract_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _ContractWorker(tmp_path)
    fixture = tmp_path / "tests/contract/src/fixtures.ts"
    fixture.parent.mkdir(parents=True)
    fixture.write_text('export const fixtureVersion = "one";\n')

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    report = await run_reconcile(
        tmp_path,
        config,
        generator=_BatteryGenerator(),
        worker_factory=lambda *_: worker,
    )
    assert report.ok

    fixture.write_text('export const fixtureVersion = "two";\n')
    checked = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert checked.exit_code == 4
    assert [item["reason"] for item in checked.blocked] == ["stale-fixture"]


@pytest.mark.asyncio
async def test_adopt_keeps_runtime_pinned_and_seals_inside_the_worker_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _RuntimeTrackingContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    source.write_text(
        "/** Clamp to zero. */\n"
        "export function clamp(value: number): number {\n"
        "  return value < 0 ? 0 : value;\n"
        "}\n"
    )
    worker.hashes["src/contract.ts"] = _digest(source.read_text())

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    report = await run_adopt(
        tmp_path,
        config,
        target="src/contract.ts#clamp",
        generator=_BatteryGenerator(),
        worker_factory=lambda *_: worker,
    )

    assert report.ok
    assert worker.runtime_events == ["pin", "verify", "seal"]
    assert not worker.active
    assert "@jauntContract" in source.read_text()


@pytest.mark.asyncio
async def test_reconcile_runtime_drift_after_validation_preserves_committed_battery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _RuntimeTrackingContractWorker(tmp_path, fail_verify=True)
    battery = tmp_path / "tests/contract/src/contract.clamp.contract.test.ts"
    battery.parent.mkdir(parents=True)
    battery.write_bytes(b"old committed battery\n")
    before = battery.read_bytes()

    async def green_batches(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.contracts._run_test_batches", green_batches)
    with pytest.raises(WorkerToolchainChangedError, match="simulated contract runtime drift"):
        await run_reconcile(
            tmp_path,
            config,
            generator=_BatteryGenerator(),
            worker_factory=lambda *_: worker,
        )

    assert battery.read_bytes() == before
    assert worker.runtime_events == ["pin", "verify"]
    assert not worker.active


@pytest.mark.asyncio
async def test_check_blocks_legacy_battery_without_fixture_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    battery = _battery_path(tmp_path, config, source, "clamp")
    battery.parent.mkdir(parents=True)
    battery.write_text(
        _with_header(
            'import { test } from "vitest";\ntest("legacy", () => {});\n',
            "src/contract.ts",
            _digest(source.read_text()),
        )
    )
    runner_called = False

    async def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal runner_called
        runner_called = True
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unexpected)
    report = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert [item["reason"] for item in report.blocked] == ["missing-fixture-provenance"]
    assert "jaunt reconcile --language ts" in str(report.blocked[0]["guidance"])
    assert runner_called is False


@pytest.mark.asyncio
async def test_check_blocks_a_strength_enabled_battery_without_valid_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=True)
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    battery = _battery_path(tmp_path, config, source, "clamp")
    battery.parent.mkdir(parents=True)
    battery.write_text(
        _with_no_fixture_provenance(
            tmp_path,
            battery,
            'import { test } from "vitest";\ntest("placeholder", () => {});\n',
            "src/contract.ts",
            _digest(source.read_text()),
        )
    )
    runner_called = False

    async def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal runner_called
        runner_called = True
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unexpected)
    report = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert [item["reason"] for item in report.blocked] == ["missing-strength"]
    assert runner_called is False


@pytest.mark.parametrize(
    ("corrupt", "expected_reason"),
    [
        ("missing", "missing-body-digest"),
        ("malformed", "malformed-body-digest"),
        ("replacement", "body-digest-mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_check_rejects_contract_battery_body_provenance_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corrupt: str,
    expected_reason: str,
) -> None:
    config = _config(tmp_path, strength=True)
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    battery = _battery_path(tmp_path, config, source, "clamp")
    battery.parent.mkdir(parents=True)
    committed = _with_strength_metadata(
        _with_no_fixture_provenance(
            tmp_path,
            battery,
            'import { expect, test } from "vitest";\n'
            'import { clamp } from "../../../src/contract.js";\n'
            'test("clamps", () => expect(clamp(-1)).toBe(0));\n',
            "src/contract.ts",
            _digest(source.read_text()),
        ),
        _strength_report(),
    )
    if corrupt == "missing":
        committed = re.sub(r"(?m)^// jaunt:body_digest=.*\n", "", committed)
    elif corrupt == "malformed":
        committed = re.sub(
            r"(?m)^// jaunt:body_digest=.*$",
            "// jaunt:body_digest=sha256:not-a-digest",
            committed,
        )
    else:
        managed_header = committed.split("\n\n", 1)[0]
        committed = (
            f'{managed_header}\n\nimport {{ test }} from "vitest";\n'
            'test("passing no-op", () => {});\n'
        )
    battery.write_text(committed)
    runner_called = False

    async def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal runner_called
        runner_called = True
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unexpected)
    report = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert [item["reason"] for item in report.blocked] == [expected_reason]
    assert "jaunt reconcile --language ts" in str(report.blocked[0]["guidance"])
    assert runner_called is False


@pytest.mark.asyncio
async def test_check_never_accepts_source_provenance_from_the_battery_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config(tmp_path, strength=False)
    worker = _ContractWorker(tmp_path)
    source = tmp_path / "src/contract.ts"
    expected_digest = _digest(source.read_text())
    battery = _battery_path(tmp_path, config, source, "clamp")
    battery.parent.mkdir(parents=True)
    battery.write_text(
        _with_header(
            f"// jaunt:source_digest={expected_digest}\nexport {{}};\n",
            "src/contract.ts",
            "sha256:" + "0" * 64,
        )
    )
    runner_called = False

    async def unexpected(*_args: Any, **_kwargs: Any) -> dict[str, object]:
        nonlocal runner_called
        runner_called = True
        return {"ok": True}

    monkeypatch.setattr("jaunt.typescript.tester._run_test_batches", unexpected)
    report = await run_check(
        tmp_path,
        config,
        contracts_only=True,
        worker_factory=lambda *_: worker,
    )

    assert report.exit_code == 4
    assert [item["reason"] for item in report.blocked] == ["stale-battery"]
    assert runner_called is False
