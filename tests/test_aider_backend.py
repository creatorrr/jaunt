from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from jaunt.agent_runtime import AgentFile, AgentTask, AgentTaskExecutionError
from jaunt.aider_executor import AiderExecutor
from jaunt.config import (
    AgentConfig,
    AiderConfig,
    BuildConfig,
    JauntConfig,
    LLMConfig,
    PathsConfig,
    PromptsConfig,
    TestConfig,
)
from jaunt.generate.aider_backend import AiderGeneratorBackend
from jaunt.generate.aider_contract import (
    aider_contract_addendum,
    aider_generation_fingerprint_parts,
)
from jaunt.generate.base import ModuleSpecContext, TokenUsage
from jaunt.generate.fingerprint import build_generation_fingerprint
from jaunt.generation_fingerprint import generation_fingerprint
from jaunt.spec_ref import normalize_spec_ref


def _fake_aider_classes():
    return (
        type("Coder", (), {"create": staticmethod(lambda **_: object())}),
        type("IO", (), {}),
        type("Model", (), {}),
    )


def test_aider_executor_materializes_workspace_and_returns_target_content(monkeypatch) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")

    seen: dict[str, object] = {}

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            seen["model_args"] = args
            seen["model_kwargs"] = kwargs

    class FakeIO:
        def __init__(self, *args, **kwargs) -> None:
            seen["io_kwargs"] = kwargs

    class FakeCoder:
        total_tokens_sent = 12
        total_tokens_received = 7

        @staticmethod
        def create(**kwargs):
            seen["create_kwargs"] = kwargs

            class _Runner:
                total_tokens_sent = 12
                total_tokens_received = 7

                def run(self, instruction: str) -> None:
                    target = Path(kwargs["fnames"][0])
                    ro_path = Path(kwargs["read_only_fnames"][0])
                    seen["instruction"] = instruction
                    seen["ro_text"] = ro_path.read_text(encoding="utf-8")
                    target.write_text("updated\n", encoding="utf-8")

            return _Runner()

    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(lambda: (FakeCoder, FakeIO, FakeModel)),
    )

    executor = AiderExecutor(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY"),
        AiderConfig(),
    )
    task = AgentTask(
        kind="skill_update",
        mode="code",
        instruction="update the skill",
        target_file=AgentFile(relative_path="workspace/SKILL.md", content="old\n"),
        read_only_files=[AgentFile(relative_path="context/contract.md", content="contract\n")],
    )

    result = asyncio.run(executor.run_task(task))
    assert result.output == "updated\n"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 12
    assert result.usage.completion_tokens == 7
    assert "contract" in str(seen["ro_text"])


def test_aider_executor_applies_reasoning_effort_to_main_and_editor_models(monkeypatch) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")

    seen: dict[str, object] = {}

    class FakeEditorModel:
        def set_reasoning_effort(self, effort: str) -> None:
            seen["editor_effort"] = effort

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            seen["model_args"] = args
            seen["model_kwargs"] = kwargs
            self.editor_model = FakeEditorModel() if kwargs.get("editor_model") else self

        def set_reasoning_effort(self, effort: str) -> None:
            seen["main_effort"] = effort

    class FakeIO:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeCoder:
        @staticmethod
        def create(**kwargs):
            class _Runner:
                total_tokens_sent = 0
                total_tokens_received = 0

                def run(self, instruction: str) -> None:
                    target = Path(kwargs["fnames"][0])
                    target.write_text("updated\n", encoding="utf-8")

            return _Runner()

    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(lambda: (FakeCoder, FakeIO, FakeModel)),
    )

    executor = AiderExecutor(
        LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="TEST_KEY",
            reasoning_effort="high",
        ),
        AiderConfig(editor_model="gpt-5.4"),
    )

    asyncio.run(
        executor.run_task(
            AgentTask(
                kind="build_module",
                mode="architect",
                instruction="write",
                target_file=AgentFile(relative_path="workspace/specs.py", content=""),
            )
        )
    )

    assert seen["main_effort"] == "high"
    assert seen["editor_effort"] == "high"


def test_aider_executor_respects_task_reasoning_overrides(monkeypatch) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")

    seen: dict[str, object] = {}

    class FakeEditorModel:
        def set_reasoning_effort(self, effort: str) -> None:
            seen["editor_effort"] = effort

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            self.editor_model = FakeEditorModel()

        def set_reasoning_effort(self, effort: str) -> None:
            seen["main_effort"] = effort

    class FakeIO:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeCoder:
        @staticmethod
        def create(**kwargs):
            class _Runner:
                total_tokens_sent = 0
                total_tokens_received = 0

                def run(self, instruction: str) -> None:
                    Path(kwargs["fnames"][0]).write_text("updated\n", encoding="utf-8")

            return _Runner()

    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(lambda: (FakeCoder, FakeIO, FakeModel)),
    )

    executor = AiderExecutor(
        LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="TEST_KEY",
            reasoning_effort="high",
        ),
        AiderConfig(editor_model="gpt-5.4"),
    )

    asyncio.run(
        executor.run_task(
            AgentTask(
                kind="build_module",
                mode="architect",
                instruction="write",
                target_file=AgentFile(relative_path="workspace/specs.py", content=""),
                main_reasoning_effort="medium",
                editor_reasoning_effort="low",
            )
        )
    )

    assert seen["main_effort"] == "medium"
    assert seen["editor_effort"] == "low"


def test_aider_executor_wraps_failure_with_partial_output_and_usage(monkeypatch) -> None:
    monkeypatch.setenv("TEST_KEY", "secret")

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeIO:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeCoder:
        total_tokens_sent = 5
        total_tokens_received = 2

        @staticmethod
        def create(**kwargs):
            class _Runner:
                total_tokens_sent = 5
                total_tokens_received = 2

                def run(self, instruction: str) -> None:
                    target = Path(kwargs["fnames"][0])
                    target.write_text("partial candidate\n", encoding="utf-8")
                    raise ValueError("SEARCH/REPLACE block failed to match")

            return _Runner()

    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(lambda: (FakeCoder, FakeIO, FakeModel)),
    )

    executor = AiderExecutor(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="TEST_KEY"),
        AiderConfig(),
    )

    try:
        asyncio.run(
            executor.run_task(
                AgentTask(
                    kind="build_module",
                    mode="code",
                    instruction="write",
                    target_file=AgentFile(relative_path="workspace/specs.py", content=""),
                )
            )
        )
        raise AssertionError("expected AgentTaskExecutionError")
    except AgentTaskExecutionError as e:
        assert "SEARCH/REPLACE" in str(e)
        assert e.output == "partial candidate\n"
        assert e.usage is not None
        assert e.usage.prompt_tokens == 5
        assert e.usage.completion_tokens == 2


def test_aider_executor_custom_api_key_env_stays_stable_across_parallel_tasks(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("KEY_ALPHA", "alpha-secret")
    monkeypatch.setenv("KEY_BETA", "beta-secret")

    barrier = threading.Barrier(2, timeout=0.2)
    seen_values: list[str] = []

    class FakeModel:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeIO:
        def __init__(self, *args, **kwargs) -> None:
            pass

    class FakeCoder:
        @staticmethod
        def create(**kwargs):
            class _Runner:
                total_tokens_sent = 0
                total_tokens_received = 0

                def run(self, instruction: str) -> None:
                    target = Path(kwargs["fnames"][0])
                    expected = target.read_text(encoding="utf-8").strip()
                    try:
                        barrier.wait()
                    except threading.BrokenBarrierError:
                        pass
                    seen_values.append(os.environ["OPENAI_API_KEY"])
                    assert os.environ["OPENAI_API_KEY"] == expected
                    target.write_text("updated\n", encoding="utf-8")

            return _Runner()

    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(lambda: (FakeCoder, FakeIO, FakeModel)),
    )

    async def _run_one(api_key_env: str, expected: str) -> str:
        executor = AiderExecutor(
            LLMConfig(provider="openai", model="gpt-test", api_key_env=api_key_env),
            AiderConfig(),
        )
        result = await executor.run_task(
            AgentTask(
                kind="build_module",
                mode="code",
                instruction="write",
                target_file=AgentFile(
                    relative_path=f"workspace/{expected}.py",
                    content=expected + "\n",
                ),
            )
        )
        return result.output

    async def _run_parallel() -> tuple[str, str]:
        return await asyncio.gather(
            _run_one("KEY_ALPHA", "alpha-secret"),
            _run_one("KEY_BETA", "beta-secret"),
        )

    outputs = asyncio.run(_run_parallel())

    assert list(outputs) == ["updated\n", "updated\n"]
    assert seen_values == ["alpha-secret", "beta-secret"]
    assert "OPENAI_API_KEY" not in os.environ


def test_aider_backend_build_task_includes_dependency_snapshot(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", test_mode="code"),
    )

    seen: dict[str, object] = {}

    async def fake_run_task(task: AgentTask):
        seen["task"] = task
        return type("Result", (), {"output": "def foo():\n    return 1\n", "usage": None})()

    monkeypatch.setattr(backend._executor, "run_task", fake_run_task)

    foo_ref = normalize_spec_ref("pkg.specs:foo")
    dep_ref = normalize_spec_ref("pkg.dep:normalize")
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={foo_ref: "def foo() -> int:\n    raise RuntimeError()\n"},
        decorator_prompts={},
        dependency_apis={dep_ref: "def normalize(s: str) -> str: ...\n"},
        dependency_generated_modules={"pkg.dep": "def normalize(s: str) -> str:\n    return s\n"},
        skills_block="## requests\nUse requests.get\n",
    )

    source, usage = asyncio.run(
        backend.generate_module(ctx, extra_error_context=["missing import for math"])
    )
    assert "def foo" in source
    task = seen["task"]
    assert isinstance(task, AgentTask)
    assert task.mode == "architect"
    assert task.edit_format == "architect"
    assert task.editor_edit_format == "editor-diff"
    assert task.editor_reasoning_effort == "low"
    ro_files = {f.relative_path: f.content for f in task.read_only_files}
    assert "pkg.dep" in ro_files["context/dependency_generated_modules.md"]
    assert "missing import for math" in ro_files["context/error_context.md"]
    assert "requests.get" in ro_files["context/external_skills.md"]
    assert "Aider Test Coverage Policy" not in ro_files["context/contract.md"]
    assert "Aider Build Policy" in ro_files["context/contract.md"]
    assert "Do not use `importlib`" in ro_files["context/contract.md"]
    assert "visible headings, prompts, help text" in ro_files["context/contract.md"]


def test_aider_backend_test_task_includes_bounded_coverage_guidance(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", test_mode="code"),
    )

    seen: dict[str, object] = {}

    async def fake_run_task(task: AgentTask):
        seen["task"] = task
        return type(
            "Result",
            (),
            {"output": "def test_foo() -> None:\n    pass\n", "usage": None},
        )()

    monkeypatch.setattr(backend._executor, "run_task", fake_run_task)

    foo_ref = normalize_spec_ref("tests.specs:test_foo")
    dep_ref = normalize_spec_ref("pkg.api:slugify")
    ctx = ModuleSpecContext(
        kind="test",
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        expected_names=["test_foo"],
        spec_sources={foo_ref: "def test_foo() -> None:\n    raise AssertionError()\n"},
        decorator_prompts={},
        dependency_apis={dep_ref: "def slugify(title: str) -> str: ...\n"},
        dependency_generated_modules={},
    )

    source, usage = asyncio.run(backend.generate_module(ctx, extra_error_context=None))
    assert "def test_foo" in source
    assert usage is None
    task = seen["task"]
    assert isinstance(task, AgentTask)
    assert task.edit_format == "diff"
    ro_files = {f.relative_path: f.content for f in task.read_only_files}
    contract = ro_files["context/contract.md"]
    assert "Aider Test Coverage Policy" in contract
    assert "Aider Runtime Policy" in contract
    assert "Add at most 1-2 extra cases" in contract
    assert "boundary/error symmetry" in contract
    assert "smallest ones that exercise nearby" in contract
    assert "Do not monkeypatch" in contract
    assert "Only assert a specific exception type" in contract
    assert "formatted or styled output" in contract
    assert "interactive input flows" in contract
    assert "public-API-first" in contract
    assert "context/retry_strategy.md" not in ro_files


def test_aider_backend_typecheck_retry_reuses_previous_candidate_with_whole_file_repair(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", test_mode="code"),
    )

    tasks: list[AgentTask] = []

    async def fake_run_task(task: AgentTask):
        tasks.append(task)
        if len(tasks) == 1:
            return type(
                "Result",
                (),
                {
                    "output": "from rich.console import Group\n\n"
                    "def render() -> object:\n"
                    "    renderables: list[object] = []\n"
                    "    return Group(*renderables)\n",
                    "usage": TokenUsage(
                        prompt_tokens=10,
                        completion_tokens=3,
                        model="gpt-test",
                        provider="aider",
                    ),
                },
            )()
        return type(
            "Result",
            (),
            {
                "output": "from rich.console import Group, RenderableType\n\n"
                "def render() -> Group:\n"
                "    renderables: list[RenderableType] = []\n"
                "    return Group(*renderables)\n",
                "usage": TokenUsage(
                    prompt_tokens=4,
                    completion_tokens=2,
                    model="gpt-test",
                    provider="aider",
                ),
            },
        )()

    monkeypatch.setattr(backend._executor, "run_task", fake_run_task)

    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["render"],
        spec_sources={normalize_spec_ref("pkg.specs:render"): "def render() -> Group:\n    ...\n"},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    result = asyncio.run(
        backend.generate_with_retry(
            ctx,
            max_attempts=2,
            extra_validator=lambda source: (
                ["ty check failed for pkg.specs: error[invalid-argument-type]"]
                if "RenderableType" not in source
                else []
            ),
        )
    )

    assert result.errors == []
    assert result.source is not None
    assert "RenderableType" in result.source
    assert result.usage is not None
    assert result.usage.prompt_tokens == 14
    assert result.usage.completion_tokens == 5
    assert len(tasks) == 2
    first_task, second_task = tasks
    assert first_task.mode == "architect"
    assert first_task.editor_edit_format == "editor-diff"
    assert second_task.mode == "code"
    assert second_task.edit_format == "whole"
    assert "list[object]" in second_task.target_file.content
    second_ro = {f.relative_path: f.content for f in second_task.read_only_files}
    assert "context/retry_strategy.md" in second_ro
    assert "smallest change needed" in second_ro["context/retry_strategy.md"]
    assert "type-check issue" in second_ro["context/error_context.md"]


def test_aider_backend_edit_apply_retry_switches_architect_editor_to_whole(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", test_mode="code"),
    )

    tasks: list[AgentTask] = []

    async def fake_run_task(task: AgentTask):
        tasks.append(task)
        if len(tasks) == 1:
            raise AgentTaskExecutionError(
                "1 SEARCH/REPLACE block failed to match",
                output="partial candidate\n",
                usage=TokenUsage(
                    prompt_tokens=11,
                    completion_tokens=6,
                    model="gpt-test",
                    provider="aider",
                ),
            )
        return type(
            "Result",
            (),
            {
                "output": "def foo() -> int:\n    return 1\n",
                "usage": TokenUsage(
                    prompt_tokens=5,
                    completion_tokens=2,
                    model="gpt-test",
                    provider="aider",
                ),
            },
        )()

    monkeypatch.setattr(backend._executor, "run_task", fake_run_task)

    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={normalize_spec_ref("pkg.specs:foo"): "def foo() -> int:\n    ...\n"},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    result = asyncio.run(backend.generate_with_retry(ctx, max_attempts=2))

    assert result.errors == []
    assert result.source == "def foo() -> int:\n    return 1\n"
    assert result.usage is not None
    assert result.usage.prompt_tokens == 16
    assert result.usage.completion_tokens == 8
    assert len(tasks) == 2
    first_task, second_task = tasks
    assert first_task.editor_edit_format == "editor-diff"
    assert second_task.mode == "architect"
    assert second_task.edit_format == "architect"
    assert second_task.editor_edit_format == "editor-whole"
    assert second_task.target_file.content == "partial candidate\n"
    second_ro = {f.relative_path: f.content for f in second_task.read_only_files}
    assert "context/retry_strategy.md" in second_ro
    assert "Rewrite the target file directly" in second_ro["context/retry_strategy.md"]
    assert "diff/search-replace edit failed to apply" in second_ro["context/error_context.md"]


def test_aider_backend_generation_fingerprint_changes_with_mode(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    backend_a = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect"),
    )
    backend_b = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="code"),
    )

    assert backend_a.generation_fingerprint(ctx) != backend_b.generation_fingerprint(ctx)


def test_aider_backend_test_generation_fingerprint_includes_runtime_guidance(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    ctx = ModuleSpecContext(
        kind="test",
        spec_module="tests.specs",
        generated_module="tests.__generated__.specs",
        expected_names=["test_foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(test_mode="code"),
    )

    assert backend.generation_fingerprint(ctx) == build_generation_fingerprint(
        engine="aider",
        kind="test",
        mode="code",
        prompt_parts=[backend._test_system, backend._test_module],
        runtime_parts=aider_generation_fingerprint_parts("test"),
    )
    assert backend.generation_fingerprint(ctx) != build_generation_fingerprint(
        engine="aider",
        kind="test",
        mode="code",
        prompt_parts=[backend._test_system, backend._test_module],
    )


def test_aider_backend_generation_fingerprint_changes_with_editor_model(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    backend_a = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", editor_model="gpt-editor-a"),
    )
    backend_b = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="architect", editor_model="gpt-editor-b"),
    )

    assert backend_a.generation_fingerprint(ctx) != backend_b.generation_fingerprint(ctx)


def test_aider_backend_generation_fingerprint_changes_with_reasoning_effort(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    backend_a = AiderGeneratorBackend(
        LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="OPENAI_API_KEY",
            reasoning_effort="high",
        ),
        AiderConfig(build_mode="architect", editor_model="gpt-5.4"),
    )
    backend_b = AiderGeneratorBackend(
        LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="OPENAI_API_KEY",
            reasoning_effort="medium",
        ),
        AiderConfig(build_mode="architect", editor_model="gpt-5.4"),
    )

    assert backend_a.generation_fingerprint(ctx) != backend_b.generation_fingerprint(ctx)


def test_aider_backend_build_generation_fingerprint_uses_build_runtime_guidance(
    monkeypatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(
        AiderExecutor,
        "_load_aider_classes",
        staticmethod(_fake_aider_classes),
    )
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    backend = AiderGeneratorBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        AiderConfig(build_mode="code"),
    )

    assert backend.generation_fingerprint(ctx) == build_generation_fingerprint(
        engine="aider",
        kind="build",
        mode="code",
        prompt_parts=[backend._build_system, backend._build_module],
        runtime_parts=aider_generation_fingerprint_parts("build"),
    )


def test_aider_test_guidance_stays_domain_agnostic() -> None:
    guidance = aider_contract_addendum("test")
    assert "Add at most 1-2 extra cases" in guidance
    assert "smallest ones that exercise nearby" in guidance
    assert "Do not monkeypatch" in guidance
    assert "Only assert a specific exception type" in guidance
    assert "formatted or styled output" in guidance
    assert "interactive input flows" in guidance
    assert "Rich/CLI output" not in guidance


def test_config_generation_fingerprint_changes_with_editor_model_only_in_architect_mode() -> None:
    base = JauntConfig(
        version=1,
        paths=PathsConfig(
            source_roots=["src"],
            test_roots=["tests"],
            generated_dir="__generated__",
        ),
        llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        agent=AgentConfig(engine="aider"),
        aider=AiderConfig(build_mode="architect", editor_model="editor-a"),
    )
    changed_editor = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=base.llm,
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=base.agent,
        aider=AiderConfig(build_mode="architect", editor_model="editor-b"),
    )
    code_mode = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=base.llm,
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=base.agent,
        aider=AiderConfig(build_mode="code", editor_model="editor-a"),
    )
    code_mode_changed = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=base.llm,
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=base.agent,
        aider=AiderConfig(build_mode="code", editor_model="editor-b"),
    )

    assert generation_fingerprint(base, kind="build") != generation_fingerprint(
        changed_editor,
        kind="build",
    )
    assert generation_fingerprint(code_mode, kind="build") == generation_fingerprint(
        code_mode_changed,
        kind="build",
    )


def test_config_generation_fingerprint_changes_with_reasoning_effort_for_aider() -> None:
    base = JauntConfig(
        version=1,
        paths=PathsConfig(
            source_roots=["src"],
            test_roots=["tests"],
            generated_dir="__generated__",
        ),
        llm=LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="OPENAI_API_KEY",
            reasoning_effort="high",
        ),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        agent=AgentConfig(engine="aider"),
        aider=AiderConfig(build_mode="architect", editor_model="gpt-5.4"),
    )
    changed_effort = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="OPENAI_API_KEY",
            reasoning_effort="medium",
        ),
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=base.agent,
        aider=base.aider,
    )
    legacy = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=base.llm,
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=AgentConfig(engine="legacy"),
        aider=base.aider,
    )
    legacy_changed = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=LLMConfig(
            provider="openai",
            model="gpt-5.4",
            api_key_env="OPENAI_API_KEY",
            reasoning_effort="medium",
        ),
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=AgentConfig(engine="legacy"),
        aider=base.aider,
    )

    assert generation_fingerprint(base, kind="build") != generation_fingerprint(
        changed_effort,
        kind="build",
    )
    assert generation_fingerprint(legacy, kind="build") == generation_fingerprint(
        legacy_changed,
        kind="build",
    )


def test_config_generation_fingerprint_legacy_test_ignores_aider_runtime_settings() -> None:
    base = JauntConfig(
        version=1,
        paths=PathsConfig(
            source_roots=["src"],
            test_roots=["tests"],
            generated_dir="__generated__",
        ),
        llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=[]),
        prompts=PromptsConfig(build_system="", build_module="", test_system="", test_module=""),
        agent=AgentConfig(engine="legacy"),
        aider=AiderConfig(test_mode="code", editor_model="editor-a"),
    )
    changed_aider = JauntConfig(
        version=base.version,
        paths=base.paths,
        llm=base.llm,
        build=base.build,
        test=base.test,
        prompts=base.prompts,
        agent=base.agent,
        aider=AiderConfig(test_mode="architect", editor_model="editor-b"),
    )

    assert generation_fingerprint(base, kind="test") == generation_fingerprint(
        changed_aider,
        kind="test",
    )
