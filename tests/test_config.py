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
    assert cfg.build.check_generated_imports is True
    assert cfg.build.generated_import_allowlist == []
    assert cfg.build.instructions == []

    assert cfg.test.jobs == 4
    assert cfg.test.infer_deps is True
    assert cfg.test.pytest_args == ["-q"]

    assert cfg.prompts.build_system == ""
    assert cfg.prompts.build_module == ""
    assert cfg.prompts.test_system == ""
    assert cfg.prompts.test_module == ""
    assert cfg.agent.engine == "codex"
    assert cfg.codex.fingerprint_cli_version is True


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
                "check_generated_imports = false",
                'generated_import_allowlist = ["intentional_extra"]',
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
                "fingerprint_cli_version = false",
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
    assert cfg.build.check_generated_imports is False
    assert cfg.build.generated_import_allowlist == ["intentional_extra"]
    assert cfg.build.instructions == ["Prefer helpers.", "Stay close to the spec."]

    assert cfg.test.jobs == 3
    assert cfg.test.infer_deps is False
    assert cfg.test.pytest_args == ["-q", "-x"]

    assert cfg.prompts.build_system == str((tmp_path / "bs").resolve())
    assert cfg.prompts.build_module == str((tmp_path / "bm").resolve())
    assert cfg.prompts.test_system == str((tmp_path / "ts").resolve())
    assert cfg.prompts.test_module == str((tmp_path / "tm").resolve())
    assert cfg.agent.engine == "codex"
    assert cfg.codex.model == "gpt-5.2-codex"
    assert cfg.codex.reasoning_effort == "medium"
    assert cfg.codex.fingerprint_cli_version is False


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
    assert cfg.codex.fingerprint_cli_version is True
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
    assert cfg.codex.model == "gpt-5.5"
    assert cfg.codex.reasoning_effort == "high"
    assert cfg.codex.sandbox == "workspace-write"
    assert cfg.codex.features == []
    assert cfg.codex.config == {}


def test_skills_config_defaults_and_parses(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)
    assert cfg.skills.auto is True
    assert cfg.skills.max_chars_per_skill == 8000
    assert cfg.skills.inject_user_skills == []

    (tmp_path / "jaunt-skills.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[skills]",
                "auto = false",
                "max_chars_per_skill = 1234",
                'inject_user_skills = ["local-api"]',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg2 = load_config(config_path=tmp_path / "jaunt-skills.toml", root=tmp_path)
    assert cfg2.skills.auto is False
    assert cfg2.skills.max_chars_per_skill == 1234
    assert cfg2.skills.inject_user_skills == ["local-api"]


def test_skills_builtin_defaults(tmp_path: Path) -> None:
    from jaunt.skills_builtin import DEFAULT_BUILTIN_SKILLS

    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.skills.builtin is True
    assert cfg.skills.builtin_skills == list(DEFAULT_BUILTIN_SKILLS)


def test_skills_builtin_overrides(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                "",
                "[skills]",
                "builtin = false",
                'builtin_skills = ["ruff", "pytest"]',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cfg = load_config(root=tmp_path)

    assert cfg.skills.builtin is False
    assert cfg.skills.builtin_skills == ["ruff", "pytest"]


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


def test_context_config_defaults(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path, config_path=tmp_path / "jaunt.toml")
    assert cfg.context.repo_map is True
    assert cfg.context.repo_map_file == "treedocs.yaml"
    assert cfg.context.enrich is False
    assert cfg.context.max_chars == 6000
    assert cfg.context.search.enabled is False
    assert cfg.context.search.internal_retrieval is True
    assert cfg.context.search.max_hits == 8


def test_context_config_parsed(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n\n[context]\nrepo_map = false\nmax_chars = 4000\n"
        "\n[context.search]\nenabled = true\nmax_hits = 12\n",
        encoding="utf-8",
    )
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path, config_path=tmp_path / "jaunt.toml")
    assert cfg.context.repo_map is False
    assert cfg.context.max_chars == 4000
    assert cfg.context.search.enabled is True
    assert cfg.context.search.max_hits == 12
