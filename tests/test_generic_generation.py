from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

import pytest

from jaunt.config import CodexConfig, LLMConfig
from jaunt.errors import JauntGenerationError
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

    async def fake_exec(**kwargs):
        cwd = Path(kwargs["cwd"])
        observed["context"] = (cwd / "_context/spec.ts").read_text(encoding="utf-8")
        observed["prompt"] = kwargs["prompt"]
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
