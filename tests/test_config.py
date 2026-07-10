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
    assert cfg.codex.fingerprint_cli_version is False
    assert cfg.daemon.poll_interval == 2.0
    assert cfg.daemon.max_jobs == 0
    assert cfg.daemon.notify_command == ""


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
    assert cfg.codex.fingerprint_cli_version is False
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
    assert cfg.codex.model == "gpt-5.6-sol"
    assert cfg.codex.reasoning_effort == "medium"
    assert cfg.codex.sandbox == "workspace-write"
    assert cfg.codex.features == []
    assert cfg.codex.config == {}


def test_daemon_config_defaults_and_parses(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)
    assert cfg.daemon.poll_interval == 2.0
    assert cfg.daemon.max_jobs == 0
    assert cfg.daemon.notify_command == ""
    assert cfg.daemon.auto_commit is False

    (tmp_path / "jaunt-daemon.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[daemon]",
                "poll_interval = 5.0",
                "max_jobs = 2",
                'notify_command = "notify-send jaunt"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    cfg2 = load_config(config_path=tmp_path / "jaunt-daemon.toml", root=tmp_path)
    assert cfg2.daemon.poll_interval == 5.0
    assert cfg2.daemon.max_jobs == 2
    assert cfg2.daemon.notify_command == "notify-send jaunt"


def test_daemon_auto_commit_defaults_false(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)
    assert cfg.daemon.auto_commit is False


def test_daemon_auto_commit_parses_true(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n[daemon]\nauto_commit = true\n", encoding="utf-8"
    )
    cfg = load_config(root=tmp_path)
    assert cfg.daemon.auto_commit is True


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


def test_context_overview_default_false(tmp_path: Path) -> None:
    """context.overview defaults to False when not set in jaunt.toml."""
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path)
    assert cfg.context.overview is False


def test_context_overview_can_be_enabled(tmp_path: Path) -> None:
    """context.overview = true is parsed correctly."""
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n\n[context]\noverview = true\n", encoding="utf-8"
    )
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path)
    assert cfg.context.overview is True


def test_prompts_config_project_overview_defaults(tmp_path: Path) -> None:
    """prompts.project_overview_system and _user default to empty string."""
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path)
    assert cfg.prompts.project_overview_system == ""
    assert cfg.prompts.project_overview_user == ""


def test_prompts_config_project_overview_parsed(tmp_path: Path) -> None:
    """prompts.project_overview_system and _user are read and resolved from project root."""
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n\n[prompts]\n"
        'project_overview_system = "custom-sys"\n'
        'project_overview_user = "custom-user"\n',
        encoding="utf-8",
    )
    from jaunt.config import load_config

    cfg = load_config(root=tmp_path)
    assert cfg.prompts.project_overview_system == str((tmp_path / "custom-sys").resolve())
    assert cfg.prompts.project_overview_user == str((tmp_path / "custom-user").resolve())


def test_prompts_build_preamble_default_and_override(tmp_path: Path) -> None:
    """prompts.build_preamble defaults to '' and an override is resolved from project root."""
    from jaunt.config import load_config

    # Default: empty string.
    (tmp_path / "jaunt.toml").write_text("version = 1\n", encoding="utf-8")
    cfg = load_config(root=tmp_path)
    assert cfg.prompts.build_preamble == ""

    # Override: a relative path is resolved against the project root.
    override_root = tmp_path / "override"
    override_root.mkdir()
    (override_root / "jaunt.toml").write_text(
        'version = 1\n\n[prompts]\nbuild_preamble = "my_preamble.md"\n', encoding="utf-8"
    )
    cfg2 = load_config(root=override_root)
    assert cfg2.prompts.build_preamble == str((override_root / "my_preamble.md").resolve())


def test_unknown_section_rejected(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[gate]\nmodel = "gpt-5.4-mini"\n', encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    with pytest.raises(JauntConfigError, match="semantic_gate"):
        load_config(root=tmp_path)


def test_unknown_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[semantic_gate]\nreasoning-effort = "high"\n', encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    with pytest.raises(JauntConfigError, match="reasoning_effort"):
        load_config(root=tmp_path)


def test_unknown_search_key_rejected(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text(
        "version = 1\n[context.search]\nmax-hits = 3\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir()
    with pytest.raises(JauntConfigError, match="max_hits"):
        load_config(root=tmp_path)


def test_init_template_roundtrips(tmp_path: Path) -> None:
    from jaunt.init_template import INIT_TEMPLATE

    (tmp_path / "jaunt.toml").write_text(INIT_TEMPLATE, encoding="utf-8")
    (tmp_path / "src").mkdir()
    cfg = load_config(root=tmp_path)
    assert cfg.version == 1
    assert cfg.codex.model == "gpt-5.6-sol"
    assert cfg.codex.reasoning_effort == "medium"


def test_full_schema_template_roundtrips(tmp_path: Path) -> None:
    from jaunt.init_template import FULL_SCHEMA_TEMPLATE

    (tmp_path / "jaunt.toml").write_text(FULL_SCHEMA_TEMPLATE, encoding="utf-8")
    (tmp_path / "src").mkdir()
    cfg = load_config(root=tmp_path)
    assert cfg.version == 1
    assert cfg.codex.model == "gpt-5.6-sol"
    assert cfg.codex.reasoning_effort == "medium"


def test_full_schema_template_covers_all_allowlists() -> None:
    """The pre-init schema shown by `jaunt instructions` must stay a superset of the
    config allowlists so a documented section/key can never be silently rejected
    (finding 3, PR #63)."""
    import tomllib

    from jaunt import config as cfg_mod
    from jaunt.init_template import FULL_SCHEMA_TEMPLATE

    data = tomllib.loads(FULL_SCHEMA_TEMPLATE)

    assert "version" in data
    section_allowlists = {
        "paths": cfg_mod._PATHS_KEYS,
        "llm": cfg_mod._LLM_KEYS,
        "build": cfg_mod._BUILD_KEYS,
        "test": cfg_mod._TEST_KEYS,
        "prompts": cfg_mod._PROMPTS_KEYS,
        "agent": cfg_mod._AGENT_KEYS,
        "codex": cfg_mod._CODEX_KEYS,
        "daemon": cfg_mod._DAEMON_KEYS,
        "skills": cfg_mod._SKILLS_KEYS,
        "contract": cfg_mod._CONTRACT_KEYS,
        "semantic_gate": cfg_mod._SEMANTIC_GATE_KEYS,
        "context": cfg_mod._CONTEXT_KEYS,
    }
    # Every allowlisted top-level section must be present.
    for section in cfg_mod._ALLOWED_SECTIONS - {"version"}:
        assert section in data, f"section [{section}] missing from FULL_SCHEMA_TEMPLATE"

    # Every key in each section allowlist must appear (nested sub-tables count as keys).
    for section, keys in section_allowlists.items():
        present = set(data.get(section, {}))
        missing = set(keys) - present
        assert not missing, f"[{section}] missing keys in FULL_SCHEMA_TEMPLATE: {sorted(missing)}"

    # Nested [context.search] must cover its own allowlist.
    search = data["context"]["search"]
    missing_search = set(cfg_mod._CONTEXT_SEARCH_KEYS) - set(search)
    assert not missing_search, f"[context.search] missing keys: {sorted(missing_search)}"
