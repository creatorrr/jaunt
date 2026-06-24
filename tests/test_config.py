from __future__ import annotations

from pathlib import Path

import pytest

from jaunt.config import find_project_root, load_config
from jaunt.errors import JauntConfigError


def test_load_minimal_config_defaults_apply(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)

    assert cfg.version == 1
    assert cfg.paths.source_roots == ["src", "."]
    assert cfg.paths.test_roots == ["tests"]
    assert cfg.paths.generated_dir == "__generated__"

    assert cfg.llm.provider == "openai"
    assert cfg.llm.model == "gpt-5.2"
    assert cfg.llm.api_key_env == "OPENAI_API_KEY"
    assert cfg.llm.reasoning_effort is None
    assert cfg.llm.anthropic_thinking_budget_tokens is None

    assert cfg.build.jobs == 8
    assert cfg.build.infer_deps is True
    assert cfg.build.ty_retry_attempts == 1
    assert cfg.build.async_runner == "asyncio"
    assert cfg.build.include_target_tests is False
    assert cfg.build.instructions == []

    assert cfg.test.jobs == 4
    assert cfg.test.infer_deps is True
    assert cfg.test.pytest_args == ["-q"]

    assert cfg.prompts.build_system == ""
    assert cfg.prompts.build_module == ""
    assert cfg.prompts.test_system == ""
    assert cfg.prompts.test_module == ""
    assert cfg.agent.engine == "codex"


def test_load_config_overrides_work(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                'test_roots = ["t"]',
                'generated_dir = "__gen__"',
                "",
                "[llm]",
                'provider = "openai"',
                'model = "gpt-4.1-mini"',
                'api_key_env = "X_API_KEY"',
                'reasoning_effort = "high"',
                "anthropic_thinking_budget_tokens = 2048",
                "",
                "[build]",
                "jobs = 2",
                "infer_deps = false",
                "ty_retry_attempts = 2",
                'async_runner = "anyio"',
                "include_target_tests = true",
                'instructions = ["Prefer helpers.", "Stay close to the spec."]',
                "",
                "[test]",
                "jobs = 3",
                "infer_deps = false",
                'pytest_args = ["-q", "-x"]',
                "",
                "[prompts]",
                'build_system = "bs"',
                'build_module = "bm"',
                'test_system = "ts"',
                'test_module = "tm"',
                "",
                "[agent]",
                'engine = "codex"',
                "",
                "[codex]",
                'model = "gpt-5.2-codex"',
                'reasoning_effort = "medium"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()

    cfg = load_config(root=tmp_path)
    assert cfg.paths.source_roots == ["src"]
    assert cfg.paths.test_roots == ["t"]
    assert cfg.paths.generated_dir == "__gen__"

    assert cfg.llm.model == "gpt-4.1-mini"
    assert cfg.llm.api_key_env == "X_API_KEY"
    assert cfg.llm.reasoning_effort == "high"
    assert cfg.llm.anthropic_thinking_budget_tokens == 2048

    assert cfg.build.jobs == 2
    assert cfg.build.infer_deps is False
    assert cfg.build.ty_retry_attempts == 2
    assert cfg.build.async_runner == "anyio"
    assert cfg.build.include_target_tests is True
    assert cfg.build.instructions == ["Prefer helpers.", "Stay close to the spec."]

    assert cfg.test.jobs == 3
    assert cfg.test.infer_deps is False
    assert cfg.test.pytest_args == ["-q", "-x"]

    assert cfg.prompts.build_system == "bs"
    assert cfg.prompts.build_module == "bm"
    assert cfg.prompts.test_system == "ts"
    assert cfg.prompts.test_module == "tm"
    assert cfg.agent.engine == "codex"
    assert cfg.codex.model == "gpt-5.2-codex"
    assert cfg.codex.reasoning_effort == "medium"


def test_codex_config_parsing(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[agent]",
                'engine = "codex"',
                "",
                "[codex]",
                'model = "gpt-5.2-codex"',
                'reasoning_effort = "medium"',
                'sandbox = "workspace-write"',
                'features = ["multi_agent", "search"]',
                "",
                "[codex.config]",
                'model_verbosity = "low"',
                "disable_response_storage = true",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.agent.engine == "codex"
    assert cfg.codex.model == "gpt-5.2-codex"
    assert cfg.codex.reasoning_effort == "medium"
    assert cfg.codex.sandbox == "workspace-write"
    assert cfg.codex.features == ["multi_agent", "search"]
    assert cfg.codex.config == {
        "model_verbosity": "low",
        "disable_response_storage": True,
    }


def test_codex_engine_defaults_load(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(["version = 1", "", "[agent]", 'engine = "codex"', ""]) + "\n",
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.agent.engine == "codex"
    assert cfg.codex.model == ""
    assert cfg.codex.reasoning_effort == "high"
    assert cfg.codex.sandbox == "workspace-write"
    assert cfg.codex.features == []
    assert cfg.codex.config == {}


def test_invalid_toml_raises(tmp_path: Path) -> None:
    p = tmp_path / "jaunt.toml"
    p.write_text("version = \n", encoding="utf-8")
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_missing_config_raises(tmp_path: Path) -> None:
    with pytest.raises(JauntConfigError):
        load_config(config_path=tmp_path / "jaunt.toml")


def test_find_project_root_success(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)

    assert find_project_root(deep) == tmp_path
    some_file = deep / "x.py"
    some_file.write_text("x=1\n", encoding="utf-8")
    assert find_project_root(some_file) == tmp_path


def test_find_project_root_failure(tmp_path: Path) -> None:
    deep = tmp_path / "a" / "b"
    deep.mkdir(parents=True)
    with pytest.raises(JauntConfigError) as ei:
        find_project_root(deep)
    assert "jaunt.toml" in str(ei.value)


def test_validation_bad_generated_dir_raises(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'generated_dir = "not-an-ident!"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_validation_jobs_must_be_ge_1(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[build]",
                "jobs = 0",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_validation_ty_retry_attempts_must_be_ge_0(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[build]",
                "ty_retry_attempts = -1",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_validation_anthropic_thinking_budget_tokens_must_be_int(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[llm]",
                'anthropic_thinking_budget_tokens = "oops"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError):
        load_config(root=tmp_path)


def test_validation_anthropic_thinking_budget_tokens_must_be_ge_1(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[llm]",
                "anthropic_thinking_budget_tokens = 0",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError, match="anthropic_thinking_budget_tokens"):
        load_config(root=tmp_path)


def test_validation_agent_engine_must_be_known(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[agent]",
                'engine = "unknown"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(JauntConfigError, match="agent.engine"):
        load_config(root=tmp_path)


def test_auto_class_tests_defaults_false_and_parses(tmp_path) -> None:
    from jaunt.config import load_config

    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n[test]\nauto_class_tests = true\n")
    cfg = load_config(root=tmp_path)
    assert cfg.test.auto_class_tests is True

    (tmp_path / "jaunt2.toml").write_text("version = 1\n")
    cfg2 = load_config(config_path=tmp_path / "jaunt2.toml", root=tmp_path)
    assert cfg2.test.auto_class_tests is False
