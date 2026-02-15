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

    assert cfg.test.jobs == 4
    assert cfg.test.infer_deps is True
    assert cfg.test.pytest_args == ["-q"]

    assert cfg.prompts.build_system == ""
    assert cfg.prompts.build_module == ""
    assert cfg.prompts.test_system == ""
    assert cfg.prompts.test_module == ""


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

    assert cfg.test.jobs == 3
    assert cfg.test.infer_deps is False
    assert cfg.test.pytest_args == ["-q", "-x"]

    assert cfg.prompts.build_system == "bs"
    assert cfg.prompts.build_module == "bm"
    assert cfg.prompts.test_system == "ts"
    assert cfg.prompts.test_module == "tm"


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
