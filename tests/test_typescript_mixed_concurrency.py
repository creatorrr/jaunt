from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from jaunt.cli import (
    _cmd_build_async,
    _cmd_mixed_build,
    _cmd_mixed_reconcile,
    _cmd_mixed_specs,
    _cmd_mixed_test,
    _command_semantic_exec,
    _capture_python_json,
    _mixed_runtime_args,
    _mixed_python_preflight,
    _mixed_typescript_preflight,
    _validated_typescript_contract_targets,
    parse_args,
)
from jaunt.config import load_config
from jaunt.errors import JauntConfigError, JauntDiscoveryError, JauntGenerationError
from jaunt.generate.base import (
    GenerationModuleResult,
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    TokenUsage,
)
from jaunt.targets.base import (
    TargetBuildReport,
    TargetDiagnostic,
    TargetTestReport,
    TargetWorkspace,
)
from jaunt.targets.runtime import MixedTargetRuntime
from jaunt.typescript.contracts import LifecycleReport


def _mixed_config(root: Path):
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "tsconfig.json").write_text("{}\n", encoding="utf-8")
    (root / "jaunt.toml").write_text(
        """\
version = 2
[target.py]
source_roots = ["src"]
test_roots = ["tests"]
[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
""",
        encoding="utf-8",
    )
    return load_config(root=root)


def test_mixed_build_runs_languages_concurrently_and_preserves_exit_precedence(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    config = _mixed_config(tmp_path)
    barrier = threading.Barrier(2)
    observed: dict[str, object] = {}

    def capture_python(_command, child_args):
        observed["python_runtime"] = child_args._mixed_runtime
        barrier.wait(timeout=2)
        return 4, {
            "command": "build",
            "ok": False,
            "generated": [],
            "skipped": [],
            "refrozen": [],
            "failed": {"python.module": ["failed"]},
        }

    async def run_typescript(*_args, **kwargs):
        observed.update(kwargs)
        barrier.wait(timeout=2)
        return TargetBuildReport(
            language="ts",
            refrozen=frozenset({"ts:src/math"}),
            metadata={"recomposed": ("ts:src/math",)},
            exit_code=3,
        )

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_: None)
    monkeypatch.setattr("jaunt.cli._capture_python_json", capture_python)
    monkeypatch.setattr("jaunt.typescript.builder.run_build", run_typescript)
    args = parse_args(["build", "--root", str(tmp_path), "--jobs", "2", "--json"])

    assert _cmd_mixed_build(args, tmp_path, config) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["targets"]["py"]["failed"]
    assert payload["targets"]["ts"]["generated"] == []
    assert payload["recomposed"] == ["ts:src/math"]
    assert payload["targets"]["ts"]["recomposed"] == ["src/math"]
    assert observed["jobs"] == 2
    assert observed["generator"] is not None
    assert observed["cost_tracker"] is not None
    assert isinstance(observed["python_runtime"], MixedTargetRuntime)
    assert observed["python_runtime"].jobs == 2
    assert observed["repo_map_enabled"] is True
    assert isinstance(observed["repo_map_block_override"], str)


def test_mixed_clean_preflight_supplies_status_only_defaults(tmp_path: Path) -> None:
    _mixed_config(tmp_path)
    (tmp_path / "src" / "example.py").write_text(
        "import jaunt\n\n@jaunt.magic\ndef answer() -> int: ...\n",
        encoding="utf-8",
    )
    args = parse_args(["clean", "--root", str(tmp_path), "--orphans", "--dry-run", "--json"])
    assert not hasattr(args, "jobs")

    _mixed_python_preflight("clean", args)


def test_mixed_test_runs_languages_concurrently(tmp_path: Path, monkeypatch, capsys) -> None:
    config = _mixed_config(tmp_path)
    barrier = threading.Barrier(2)
    observed: dict[str, object] = {}

    def capture_python(_command, child_args):
        observed["python_runtime"] = child_args._mixed_runtime
        barrier.wait(timeout=2)
        return 0, {
            "command": "test",
            "ok": True,
            "generated": [],
            "skipped": [],
            "refrozen": [],
            "failed": {},
        }

    async def run_typescript(*_args, **kwargs):
        observed.update(kwargs)
        barrier.wait(timeout=2)
        return TargetTestReport(language="ts")

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_: None)
    monkeypatch.setattr("jaunt.cli._capture_python_json", capture_python)
    monkeypatch.setattr("jaunt.typescript.tester.run_test", run_typescript)
    args = parse_args(["test", "--root", str(tmp_path), "--jobs", "3", "--json"])

    assert _cmd_mixed_test(args, tmp_path, config) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 2
    assert payload["targets"]["py"]["failed"] == {}
    assert payload["targets"]["ts"]["generated"] == []
    assert observed["jobs"] == 3
    assert observed["generator"] is not None
    assert observed["cost_tracker"] is not None
    assert isinstance(observed["python_runtime"], MixedTargetRuntime)
    assert observed["python_runtime"].jobs == 3
    assert observed["repo_map_enabled"] is True
    assert isinstance(observed["repo_map_block_override"], str)


class _PeakCounter:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0

    def enter(self) -> None:
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)

    def leave(self) -> None:
        with self.lock:
            self.active -= 1


class _DelayedBackend(GeneratorBackend):
    def __init__(self, counter: _PeakCounter, *, delay: float = 0.03) -> None:
        self.counter = counter
        self.delay = delay

    async def _call(self) -> GenerationModuleResult:
        self.counter.enter()
        try:
            await asyncio.sleep(self.delay)
        finally:
            self.counter.leave()
        return "export const ok = true;\n", None

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ) -> GenerationModuleResult:
        del ctx, extra_error_context
        return await self._call()

    async def generate_request(
        self,
        request: GenerationRequest,
        *,
        extra_error_context: list[str] | None = None,
    ) -> GenerationModuleResult:
        del request, extra_error_context
        return await self._call()


def _request() -> GenerationRequest:
    return GenerationRequest(
        language="ts",
        kind="test",
        target_path="out.ts",
        context_files={},
        prompt="test",
        cache_payload={},
        validator=lambda _source: [],
    )


def test_shared_runtime_caps_combined_backend_peak_across_event_loop_threads() -> None:
    runtime = MixedTargetRuntime(jobs=2, max_cost=None)
    counter = _PeakCounter()
    py_backend = runtime.backend("py", lambda: _DelayedBackend(counter))
    ts_backend = runtime.backend("ts", lambda: _DelayedBackend(counter))
    errors: list[BaseException] = []

    def run_many(backend: GeneratorBackend) -> None:
        async def run() -> None:
            await asyncio.gather(*(backend.generate_request(_request()) for _ in range(4)))

        try:
            asyncio.run(run())
        except BaseException as error:  # pragma: no cover - diagnostic aid
            errors.append(error)

    threads = [
        threading.Thread(target=run_many, args=(py_backend,)),
        threading.Thread(target=run_many, args=(ts_backend,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert counter.peak == 2


@pytest.mark.asyncio
async def test_shared_runtime_releases_backend_slot_on_cancellation() -> None:
    runtime = MixedTargetRuntime(jobs=1, max_cost=None)
    counter = _PeakCounter()
    backend = runtime.backend("ts", lambda: _DelayedBackend(counter, delay=10))
    task = asyncio.create_task(backend.generate_request(_request()))
    while counter.active == 0:
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    replacement = runtime.backend("py", lambda: _DelayedBackend(counter, delay=0))
    await asyncio.wait_for(replacement.generate_request(_request()), timeout=1)
    assert counter.active == 0


@pytest.mark.asyncio
async def test_shared_runtime_returns_slot_when_queued_waiter_is_cancelled() -> None:
    runtime = MixedTargetRuntime(jobs=1, max_cost=None)
    counter = _PeakCounter()
    backend = runtime.backend("ts", lambda: _DelayedBackend(counter, delay=10))
    holder = asyncio.create_task(backend.generate_request(_request()))
    while counter.active == 0:
        await asyncio.sleep(0)
    queued = asyncio.create_task(backend.generate_request(_request()))
    await asyncio.sleep(0.02)
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    holder.cancel()
    with pytest.raises(asyncio.CancelledError):
        await holder

    replacement = runtime.backend("py", lambda: _DelayedBackend(counter, delay=0))
    await asyncio.wait_for(replacement.generate_request(_request()), timeout=1)
    assert counter.active == 0


def test_shared_runtime_cancels_active_calls_on_other_event_loop_threads() -> None:
    runtime = MixedTargetRuntime(jobs=2, max_cost=None)
    counter = _PeakCounter()
    backend = runtime.backend("ts", lambda: _DelayedBackend(counter, delay=10))
    errors: list[BaseException] = []

    def run() -> None:
        try:
            asyncio.run(backend.generate_request(_request()))
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    while counter.active < 2:
        threading.Event().wait(0.005)
    runtime.cancel()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert len(errors) == 2
    assert all(isinstance(error, asyncio.CancelledError) for error in errors)
    assert counter.active == 0


def test_shared_runtime_combines_language_usage_under_one_budget() -> None:
    runtime = MixedTargetRuntime(jobs=2, max_cost=0.000003)
    usage = TokenUsage(1, 0, "gpt-5.6-sol", "codex")
    barrier = threading.Barrier(2)
    errors: list[BaseException] = []

    def charge(language: str) -> None:
        barrier.wait(timeout=2)
        try:
            runtime.cost_tracker(language).record(f"{language}:one", usage)  # type: ignore[arg-type]
        except BaseException as error:
            errors.append(error)

    threads = [threading.Thread(target=charge, args=(language,)) for language in ("py", "ts")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert len(errors) == 1
    assert isinstance(errors[0], JauntGenerationError)
    assert runtime.summary()["api_calls"] == 2
    assert runtime.summary("py")["api_calls"] == 1
    assert runtime.summary("ts")["api_calls"] == 1


@pytest.mark.asyncio
async def test_python_semantic_gate_usage_charges_shared_ledger_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _mixed_config(tmp_path)
    args = _mixed_runtime_args(
        parse_args(["build", "--root", str(tmp_path)]),
        config,
        command="build",
    )

    async def fake_exec(**_kwargs: Any) -> SimpleNamespace:
        return SimpleNamespace(usage_input=2, usage_output=1, usage_cached=1)

    monkeypatch.setattr("jaunt.generate.codex_backend.run_codex_exec", fake_exec)
    run_exec = _command_semantic_exec(args)
    assert run_exec is not None
    await run_exec(model="gpt-5.6-luna")

    runtime = args._mixed_runtime
    assert runtime.summary("py")["api_calls"] == 1
    assert runtime.summary("ts")["api_calls"] == 0


def test_mixed_runtime_uses_command_specific_default_jobs(tmp_path: Path) -> None:
    config = _mixed_config(tmp_path)
    build = _mixed_runtime_args(
        parse_args(["build", "--root", str(tmp_path)]), config, command="build"
    )
    test = _mixed_runtime_args(
        parse_args(["test", "--root", str(tmp_path)]), config, command="test"
    )

    assert build._mixed_runtime.jobs == config.build.jobs
    assert test._mixed_runtime.jobs == config.test.jobs


def test_qualified_python_target_round_trips_into_python_command(tmp_path: Path) -> None:
    seen: list[str] = []

    def command(args: Any) -> int:
        seen.extend(args.target)
        print(json.dumps({"ok": True}))
        return 0

    args = parse_args(["build", "--root", str(tmp_path), "--target", "py:pkg.module", "--json"])
    code, payload = _capture_python_json(command, args)

    assert code == 0
    assert payload["ok"] is True
    assert seen == ["pkg.module"]


def test_mixed_specs_routes_qualified_module_filter_to_one_language(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    python_modules: list[str | None] = []
    typescript_targets: list[tuple[str, ...]] = []

    def python_command(args: Any) -> int:
        python_modules.append(args.module)
        print(json.dumps({"command": "specs", "ok": True, "specs": []}))
        return 0

    async def typescript_command(
        _root: Path,
        _config: object,
        *,
        target_ids: tuple[str, ...],
    ) -> TargetWorkspace:
        typescript_targets.append(target_ids)
        return TargetWorkspace(language="ts")

    monkeypatch.setattr("jaunt.cli.cmd_specs", python_command)
    monkeypatch.setattr("jaunt.typescript.status.run_specs", typescript_command)

    python_args = parse_args(
        ["specs", "--root", str(tmp_path), "--module", "py:pkg.module", "--json"]
    )
    assert _cmd_mixed_specs(python_args, tmp_path, config) == 0
    json.loads(capsys.readouterr().out)
    assert python_modules == ["pkg.module"]
    assert typescript_targets == []

    typescript_args = parse_args(
        ["specs", "--root", str(tmp_path), "--module", "ts:src/math", "--json"]
    )
    assert _cmd_mixed_specs(typescript_args, tmp_path, config) == 0
    json.loads(capsys.readouterr().out)
    assert python_modules == ["pkg.module"]
    assert typescript_targets == [("ts:src/math",)]


def test_mixed_test_preflight_plans_contract_authored_and_implicit_batteries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _mixed_config(tmp_path)
    test_spec_path = "tests/math.jaunt-test.ts"
    (tmp_path / test_spec_path).write_text(
        "/** @prop given value: number :: double(value) equals value * 2 */\n",
        encoding="utf-8",
    )
    (tmp_path / "src/contract.ts").write_text(
        "export function contractValue(): number { return 1; }\n",
        encoding="utf-8",
    )
    contract_battery = tmp_path / "tests/contract/src/contract.contractValue.contract.test.ts"
    contract_battery.parent.mkdir(parents=True)
    contract_battery.write_text("export {};\n", encoding="utf-8")
    module = {
        "moduleId": "ts:src/math",
        "specPath": "src/math.jaunt.ts",
        "specSource": "export function double(value: number): number;",
        "symbols": [
            {"name": "double", "kind": "function"},
            {"name": "Counter", "kind": "class", "options": {"test": True}},
        ],
    }
    analysis = SimpleNamespace(
        modules=(module,),
        workspace={
            "projects": [
                {
                    "id": "tsconfig.json",
                    "configPath": "tsconfig.json",
                    "role": "test",
                    "rootFiles": [],
                }
            ],
            "testSpecs": [
                {
                    "path": test_spec_path,
                    "project": "tsconfig.json",
                    "targets": ["ts:src/math#double"],
                }
            ],
            "contracts": [{"path": "src/contract.ts", "symbols": [{"name": "contractValue"}]}],
        },
    )

    @asynccontextmanager
    async def fake_worker_session(*_args: object, **_kwargs: object):
        yield object(), object()

    analyze_targets: list[tuple[str, ...]] = []

    async def fake_analyze(*_args: object, **kwargs: object) -> object:
        raw_targets = kwargs.get("target_ids", ())
        assert isinstance(raw_targets, (list, tuple))
        analyze_targets.append(tuple(str(item) for item in raw_targets))
        return analysis

    captured: dict[str, object] = {}

    def capture_groups(
        _root: Path,
        _config: object,
        _workspace: object,
        files: tuple[str, ...],
        *,
        explicit_owners: dict[str, str],
    ) -> dict[str, tuple[str, ...]]:
        captured["files"] = files
        captured["owners"] = explicit_owners
        return {"tsconfig.json": files}

    def capture_dependencies(
        _root: Path,
        _workspace: object,
        _grouped: object,
        *,
        require_fast_check: bool,
    ) -> None:
        captured["require_fast_check"] = require_fast_check

    monkeypatch.setattr("jaunt.typescript.builder.worker_session", fake_worker_session)
    monkeypatch.setattr("jaunt.typescript.builder.analyze", fake_analyze)
    monkeypatch.setattr("jaunt.typescript.tester._runner_path", lambda _client: None)
    monkeypatch.setattr("jaunt.typescript.tester._group_test_files", capture_groups)
    monkeypatch.setattr(
        "jaunt.typescript.tester._validate_test_owner_dependencies",
        capture_dependencies,
    )

    assert _mixed_typescript_preflight(tmp_path, config, (), for_test=True) is analysis
    raw_files = captured["files"]
    assert isinstance(raw_files, tuple)
    files = {str(item) for item in raw_files}
    owners = captured["owners"]
    assert isinstance(owners, dict)
    assert "tests/__generated__/math.example.test.ts" in files
    assert any("auto.src-math-Counter.example.test.ts" in path for path in files)
    assert any("contractValue.contract.test.ts" in path for path in files)
    assert any("contractValue.contract.test.ts" in str(path) for path in owners)
    assert captured["require_fast_check"] is True

    assert (
        _mixed_typescript_preflight(
            tmp_path,
            config,
            ("ts:src/math",),
            for_test=True,
        )
        is analysis
    )
    assert analyze_targets == [(), ("ts:src/math",)]
    raw_targeted_files = captured["files"]
    assert isinstance(raw_targeted_files, tuple)
    targeted_files = {str(item) for item in raw_targeted_files}
    assert not any("contractValue.contract.test.ts" in path for path in targeted_files)


@pytest.mark.asyncio
async def test_mixed_python_build_defers_auto_skill_model_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    args = _mixed_runtime_args(
        parse_args(["build", "--root", str(tmp_path), "--json"]),
        config,
        command="build",
    )

    async def forbidden(**_kwargs: Any) -> None:
        raise AssertionError("auto skill model work escaped the mixed runtime")

    monkeypatch.setattr("jaunt.skills_auto.ensure_pypi_skills", forbidden)
    assert await _cmd_build_async(args) == 0
    assert "deferred automatic PyPI skill generation" in capsys.readouterr().err


def test_mixed_reconcile_runs_both_targets_and_aggregates_v2_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    order: list[str] = []

    def preflight(
        _root: Path,
        _config: object,
        target_ids: tuple[str, ...],
        **_kwargs: object,
    ) -> None:
        assert target_ids == ()
        order.append("preflight")

    def python(_command: object, _args: object) -> tuple[int, dict[str, object]]:
        order.append("py")
        return 0, {
            "command": "reconcile",
            "ok": True,
            "reconciled": [{"ref": "pkg.contract:f", "strength": 1, "wrote": True}],
            "failed": [],
        }

    async def typescript(*_args: object, **kwargs: object) -> LifecycleReport:
        assert kwargs["target_ids"] == ()
        order.append("ts")
        return LifecycleReport(
            command="reconcile",
            targets=("src/contract.ts#f",),
            changed=("tests/contract/f.test.ts",),
        )

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", preflight)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_args: None)
    monkeypatch.setattr("jaunt.cli._capture_python_json", python)
    monkeypatch.setattr("jaunt.typescript.contracts.run_reconcile", typescript)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 0
    payload = json.loads(capsys.readouterr().out)
    assert order[0] == "preflight"
    assert set(order[1:]) == {"py", "ts"}
    assert payload["schema_version"] == 2
    assert payload["reconciled"][0]["ref"] == "py:pkg.contract:f"
    assert payload["changed"] == ["tests/contract/f.test.ts"]
    assert payload["targets"]["py"]["reconciled"]
    assert payload["targets"]["ts"]["selected"] == ["src/contract.ts#f"]


def test_mixed_reconcile_budget_failure_cancels_typescript_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    loaded = _mixed_config(tmp_path)
    config = replace(loaded, llm=replace(loaded.llm, max_cost_per_build=0.0))
    typescript_started = threading.Event()
    typescript_cancelled = threading.Event()

    def python(_command: object, child_args: Any) -> tuple[int, dict[str, object]]:
        assert typescript_started.wait(timeout=2)
        try:
            child_args._mixed_runtime.cost_tracker("py").record(
                "pkg.contract:f",
                TokenUsage(1000, 1000, "gpt-5.6-sol", "codex"),
            )
        except JauntGenerationError as error:
            return 3, {
                "command": "reconcile",
                "ok": False,
                "failed": [{"ref": "pkg.contract:f", "error": str(error)}],
            }
        raise AssertionError("the shared budget must reject this usage")

    async def typescript(*_args: object, **_kwargs: object) -> LifecycleReport:
        typescript_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            typescript_cancelled.set()
            raise
        raise AssertionError("unreachable")

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_args: None)
    monkeypatch.setattr("jaunt.cli._capture_python_json", python)
    monkeypatch.setattr("jaunt.typescript.contracts.run_reconcile", typescript)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 3
    assert typescript_cancelled.wait(timeout=1)
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"]["ts"]["skipped"] is True
    assert payload["cost"]["api_calls"] == 1


def test_mixed_reconcile_ts_only_filter_skips_python_and_reports_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    selected = "ts:src/contract#f"

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_args: None)
    monkeypatch.setattr(
        "jaunt.cli._validated_typescript_contract_targets",
        lambda _analysis, requested: requested,
    )
    monkeypatch.setattr(
        "jaunt.cli._capture_python_json",
        lambda *_args: (0, {"command": "reconcile", "ok": True}),
    )

    async def failed(*_args: object, **kwargs: object) -> LifecycleReport:
        assert kwargs["target_ids"] == (selected,)
        return LifecycleReport(
            command="reconcile",
            targets=("src/contract.ts#f",),
            diagnostics=(TargetDiagnostic(code="JAUNT_TS_CONTRACT_FAILED", message="bad"),),
            exit_code=4,
        )

    monkeypatch.setattr("jaunt.typescript.contracts.run_reconcile", failed)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--target", selected, "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 4
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"]["py"]["skipped"] is True
    assert payload["targets"]["ts"]["ok"] is False
    assert payload["targets"]["ts"]["diagnostics"][0]["code"] == "JAUNT_TS_CONTRACT_FAILED"
    assert payload["failed"][0]["target"] == "ts"


def test_typescript_contract_target_preflight_expands_modules_and_rejects_unknown() -> None:
    analysis = SimpleNamespace(
        workspace={
            "contracts": [
                {
                    "path": "src/contract.ts",
                    "symbols": [{"name": "first"}, {"name": "second"}],
                }
            ]
        }
    )

    assert _validated_typescript_contract_targets(analysis, ("ts:src/contract",)) == (
        "ts:src/contract#first",
        "ts:src/contract#second",
    )
    with pytest.raises(JauntConfigError, match="No TypeScript contract matches"):
        _validated_typescript_contract_targets(analysis, ("ts:missing#f",))


def test_mixed_reconcile_python_only_filter_skips_typescript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    typescript_ran = False

    def python(_command: object, child_args: Any) -> tuple[int, dict[str, object]]:
        assert child_args.target == ["pkg.contract"]
        return 0, {
            "command": "reconcile",
            "ok": True,
            "reconciled": [{"ref": "pkg.contract:f"}],
            "failed": [],
        }

    async def typescript(*_args: object, **_kwargs: object) -> LifecycleReport:
        nonlocal typescript_ran
        typescript_ran = True
        return LifecycleReport(command="reconcile")

    monkeypatch.setattr("jaunt.cli._capture_python_json", python)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_args: None)
    monkeypatch.setattr("jaunt.typescript.contracts.run_reconcile", typescript)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--target", "pkg.contract", "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 0
    payload = json.loads(capsys.readouterr().out)
    assert typescript_ran is False
    assert payload["targets"]["py"]["reconciled"]
    assert payload["targets"]["ts"]["skipped"] is True


def test_mixed_reconcile_preflight_failure_prevents_python_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    python_ran = False

    def fail_preflight(*_args: object, **_kwargs: object) -> None:
        raise JauntDiscoveryError("worker unavailable")

    def python(*_args: object) -> tuple[int, dict[str, object]]:
        nonlocal python_ran
        python_ran = True
        return 0, {"ok": True}

    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", fail_preflight)
    monkeypatch.setattr("jaunt.cli._capture_python_json", python)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 2
    assert python_ran is False
    payload = json.loads(capsys.readouterr().out)
    assert payload["targets"]["py"]["skipped"] is True


def test_mixed_reconcile_exit_precedence_prefers_generation_over_contract_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    config = _mixed_config(tmp_path)
    monkeypatch.setattr("jaunt.cli._mixed_typescript_preflight", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("jaunt.cli._mixed_python_preflight", lambda *_args: None)
    monkeypatch.setattr(
        "jaunt.cli._capture_python_json",
        lambda *_args: (4, {"command": "reconcile", "ok": False, "failed": []}),
    )

    async def typescript(*_args: object, **_kwargs: object) -> LifecycleReport:
        return LifecycleReport(
            command="reconcile",
            diagnostics=(TargetDiagnostic(code="JAUNT_TS_GENERATION", message="bad"),),
            exit_code=3,
        )

    monkeypatch.setattr("jaunt.typescript.contracts.run_reconcile", typescript)
    args = parse_args(["reconcile", "--root", str(tmp_path), "--json"])

    assert _cmd_mixed_reconcile(args, tmp_path, config) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["targets"]["ts"]["ok"] is False
