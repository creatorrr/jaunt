from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from jaunt.config import CodexConfig, LLMConfig
from jaunt.errors import (
    JauntGenerationError,
    JauntQuotaGenerationError,
    JauntTransientGenerationError,
)
from jaunt.generate.base import (
    GenerationRequest,
    GeneratorBackend,
    ModuleSpecContext,
    generation_request_cache_key,
)
from jaunt.generate.codex_backend import CodexBackend, CodexExecResult


class GenericBackend(GeneratorBackend):
    def __init__(self) -> None:
        self.calls = 0
        self.seeds: list[str] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ):
        return "def legacy():\n    return True\n", None

    async def generate_request(
        self, request: GenerationRequest, *, extra_error_context: list[str] | None = None
    ):
        self.calls += 1
        self.seeds.append(request.seed_target_content)
        return ("bad" if self.calls == 1 else "good"), None


def test_generic_retry_uses_async_request_validator() -> None:
    seen: list[str] = []

    async def validate(source: str) -> list[str]:
        seen.append(source)
        return [] if source == "good" else ["candidate is not good"]

    request = GenerationRequest(
        language="ts",
        kind="build",
        target_path="src/generated.ts",
        context_files={"_context/spec.ts": "export function f(): string;\n"},
        prompt="Implement f.",
        cache_payload={"contract": "digest"},
        validator=validate,
    )
    backend = GenericBackend()
    result = asyncio.run(backend.generate_request_with_retry(request))

    assert result.source == "good"
    assert result.attempts == 2
    assert result.errors == []
    assert seen == ["bad", "good"]
    assert backend.seeds == ["", "bad"]


def test_generic_retry_separates_transient_infrastructure_from_candidate_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapacityBackend(GenericBackend):
        async def generate_request(self, request: GenerationRequest, **_kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise JauntTransientGenerationError("Selected model is at capacity")
            return "good", None

    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path="tests/generated.test.ts",
        context_files={},
        prompt="Generate tests.",
        cache_payload={},
        validator=lambda source: [] if source == "good" else ["bad"],
    )
    result = asyncio.run(CapacityBackend().generate_request_with_retry(request))

    assert result.attempts == 1
    assert result.infrastructure_retries == 2
    assert len(result.infrastructure_errors) == 2
    assert result.infrastructure_exhausted is False
    assert sleeps == [1.0, 2.0]


def test_generic_retry_returns_structured_result_when_capacity_stays_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class CapacityBackend(GenericBackend):
        async def generate_request(self, request: GenerationRequest, **_kwargs):
            self.calls += 1
            raise JauntTransientGenerationError("Selected model is at capacity")

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path="tests/generated.test.ts",
        context_files={},
        prompt="Generate tests.",
        cache_payload={},
        validator=lambda _source: [],
    )
    backend = CapacityBackend()
    result = asyncio.run(backend.generate_request_with_retry(request))

    assert backend.calls == 3
    assert result.attempts == 0
    assert result.infrastructure_retries == 2
    assert len(result.infrastructure_errors) == 3
    assert result.infrastructure_exhausted is True
    assert result.errors == ["Selected model is at capacity"]


def test_quota_wait_does_not_consume_capacity_retry_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class QuotaThenCapacityBackend(GenericBackend):
        @property
        def quota_wait_minutes(self) -> float:
            return 1.0

        async def generate_request(self, request: GenerationRequest, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise JauntQuotaGenerationError("You've hit your usage limit")
            if self.calls <= 3:
                raise JauntTransientGenerationError("Selected model is at capacity")
            return "good", None

    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path="tests/generated.test.ts",
        context_files={},
        prompt="Generate tests.",
        cache_payload={},
        validator=lambda source: [] if source == "good" else ["bad"],
    )
    result = asyncio.run(QuotaThenCapacityBackend().generate_request_with_retry(request))

    assert result.attempts == 1
    assert result.infrastructure_retries == 2
    assert len(result.infrastructure_errors) == 2
    assert sleeps == [60.0, 1.0, 2.0]


def test_codex_quota_wait_retries_same_candidate_with_exponential_backoff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []
    progress: list[tuple[str, str]] = []

    async def fake_exec(**kwargs):
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise JauntQuotaGenerationError("You've hit your usage limit")
        cwd = Path(kwargs["cwd"])
        (cwd / "out").mkdir(exist_ok=True)
        (cwd / "out/index.ts").write_text("export const answer = 42;\n", encoding="utf-8")
        return CodexExecResult(
            returncode=0,
            final_message="ADVISORIES: none",
            usage_input=1,
            usage_output=2,
            usage_cached=0,
            stderr="",
        )

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.codex_backend.run_codex_exec", fake_exec)
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    backend = CodexBackend(
        CodexConfig(quota_wait_minutes=3),
        LLMConfig("openai", "unused", "OPENAI_API_KEY"),
    )
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path="out/index.ts",
        context_files={},
        prompt="Generate the target.",
        cache_payload={},
        validator=lambda source: [] if "answer" in source else ["missing answer"],
        project_root=tmp_path,
    )

    result = asyncio.run(
        backend.generate_request_with_retry(
            request,
            progress=lambda stage, detail: progress.append((stage, detail)),
        )
    )

    assert calls == 3
    assert sleeps == [60.0, 120.0]
    assert result.attempts == 1
    assert result.source == "export const answer = 42;\n"
    quota_progress = [detail for stage, detail in progress if stage == "quota-wait"]
    assert len(quota_progress) == 2
    assert quota_progress[0].startswith("waiting 1 minute(s)")
    assert quota_progress[1].startswith("waiting 2 minute(s)")


@pytest.mark.parametrize(
    ("budget_minutes", "expected_calls", "expected_sleeps"),
    [(0.0, 1, []), (1.0, 2, [60.0])],
)
def test_codex_quota_wait_exhaustion_rethrows_without_candidate_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    budget_minutes: float,
    expected_calls: int,
    expected_sleeps: list[float],
) -> None:
    calls = 0
    sleeps: list[float] = []

    async def fake_exec(**_kwargs):
        nonlocal calls
        calls += 1
        raise JauntQuotaGenerationError("You've hit your usage limit")

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.codex_backend.run_codex_exec", fake_exec)
    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    backend = CodexBackend(
        CodexConfig(quota_wait_minutes=budget_minutes),
        LLMConfig("openai", "unused", "OPENAI_API_KEY"),
    )
    request = GenerationRequest(
        language="ts",
        kind="test",
        target_path="out/index.ts",
        context_files={},
        prompt="Generate the target.",
        cache_payload={},
        validator=lambda _source: [],
        project_root=tmp_path,
    )

    with pytest.raises(JauntQuotaGenerationError, match="usage limit"):
        asyncio.run(backend.generate_request_with_retry(request, max_attempts=2))

    assert calls == expected_calls
    assert sleeps == expected_sleeps


class _QuotaAcrossArtifactsBackend(GeneratorBackend):
    def __init__(self, budget_minutes: float) -> None:
        self.budget_minutes = budget_minutes
        self.module_calls = 0
        self.request_calls = 0

    @property
    def quota_wait_minutes(self) -> float:
        return self.budget_minutes

    async def generate_module(
        self,
        ctx: ModuleSpecContext,
        *,
        extra_error_context: list[str] | None = None,
    ):
        del ctx, extra_error_context
        self.module_calls += 1
        if self.module_calls == 1:
            raise JauntQuotaGenerationError("module usage limit")
        return "def generated():\n    return True\n", None

    async def generate_request(
        self,
        request: GenerationRequest,
        *,
        extra_error_context: list[str] | None = None,
    ):
        del request, extra_error_context
        self.request_calls += 1
        if self.request_calls == 1:
            raise JauntQuotaGenerationError("battery usage limit")
        return "export const generated = true;\n", None


def _quota_module_context() -> ModuleSpecContext:
    return ModuleSpecContext(
        kind="build",
        spec_module="example.spec",
        generated_module="example.generated",
        expected_names=["generated"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )


def _quota_request() -> GenerationRequest:
    return GenerationRequest(
        language="ts",
        kind="test",
        target_path="tests/generated.test.ts",
        context_files={},
        prompt="Generate a battery.",
        cache_payload={},
        validator=lambda _source: [],
    )


def test_quota_wait_budget_and_backoff_are_shared_across_sequential_artifacts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)
    backend = _QuotaAcrossArtifactsBackend(3.0)

    async def generate_both():
        module = await backend.generate_with_retry(_quota_module_context())
        battery = await backend.generate_request_with_retry(_quota_request())
        return module, battery

    module, battery = asyncio.run(generate_both())

    assert module.errors == []
    assert battery.errors == []
    assert backend.module_calls == 2
    assert backend.request_calls == 2
    assert sleeps == [60.0, 120.0]


def test_concurrent_artifacts_cannot_overdraw_shared_quota_wait_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    original_sleep = asyncio.sleep

    async def yielding_sleep(delay: float) -> None:
        sleeps.append(delay)
        await original_sleep(0)

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", yielding_sleep)
    backend = _QuotaAcrossArtifactsBackend(1.0)

    async def generate_both():
        return await asyncio.gather(
            backend.generate_with_retry(_quota_module_context()),
            backend.generate_request_with_retry(_quota_request()),
            return_exceptions=True,
        )

    results = asyncio.run(generate_both())

    assert sum(isinstance(result, JauntQuotaGenerationError) for result in results) == 1
    successful = [result for result in results if not isinstance(result, BaseException)]
    assert len(successful) == 1
    assert successful[0].errors == []
    assert backend.module_calls + backend.request_calls == 3
    assert sleeps == [60.0]


def test_fresh_command_backend_resets_quota_wait_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []

    async def no_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("jaunt.generate.base.asyncio.sleep", no_sleep)

    for _command in range(2):
        backend = _QuotaAcrossArtifactsBackend(1.0)
        result = asyncio.run(backend.generate_request_with_retry(_quota_request()))
        assert result.errors == []
        assert backend.request_calls == 2

    assert sleeps == [60.0, 60.0]


def test_generic_cache_key_is_language_and_target_namespaced() -> None:
    def validate(_source: str) -> list[str]:
        return []

    ts = GenerationRequest(
        language="ts",
        kind="build",
        target_path="out/index.ts",
        context_files={},
        prompt="ignored for explicit cache payload",
        cache_payload={"ir": "abc"},
        validator=validate,
    )
    py = replace(ts, language="py")
    ts_key = generation_request_cache_key(ts, model="m", provider="p")
    py_key = generation_request_cache_key(py, model="m", provider="p")
    assert ts_key != py_key


def test_codex_generic_request_writes_only_safe_workspace_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    observed: dict[str, str] = {}
    flags: dict[str, bool] = {}

    async def fake_exec(**kwargs):
        cwd = Path(kwargs["cwd"])
        observed["context"] = (cwd / "_context/spec.ts").read_text(encoding="utf-8")
        observed["prompt"] = kwargs["prompt"]
        flags["ignore_user_config"] = kwargs["ignore_user_config"]
        (cwd / "out").mkdir(exist_ok=True)
        (cwd / "out/index.ts").write_text("export const answer = 42;\n", encoding="utf-8")
        return CodexExecResult(
            returncode=0,
            final_message="ADVISORIES: none",
            usage_input=1,
            usage_output=2,
            usage_cached=0,
            stderr="",
        )

    monkeypatch.setattr("jaunt.generate.codex_backend.run_codex_exec", fake_exec)
    backend = CodexBackend(CodexConfig(), LLMConfig("openai", "unused", "OPENAI_API_KEY"))
    request = GenerationRequest(
        language="ts",
        kind="build",
        target_path="out/index.ts",
        context_files={"_context/spec.ts": "export declare const answer: number;\n"},
        prompt="Implement the reserved binding.",
        cache_payload={},
        validator=lambda source: [],
        project_root=tmp_path,
    )

    source, usage, advisories = asyncio.run(backend.generate_request(request))

    assert source == "export const answer = 42;\n"
    assert usage is not None and usage.completion_tokens == 2
    assert advisories == ()
    assert observed["context"].startswith("export declare")
    assert "out/index.ts" in observed["prompt"]
    assert flags["ignore_user_config"] is True


def test_codex_generic_request_rejects_workspace_escape() -> None:
    backend = CodexBackend(CodexConfig(), LLMConfig("openai", "unused", "OPENAI_API_KEY"))
    request = GenerationRequest(
        language="ts",
        kind="build",
        target_path="../escape.ts",
        context_files={},
        prompt="bad",
        cache_payload={},
        validator=lambda source: [],
    )
    with pytest.raises(JauntGenerationError, match="safe root-relative"):
        asyncio.run(backend.generate_request(request))
