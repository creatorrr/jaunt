from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from jaunt.config import (
    AgentConfig,
    BuildConfig,
    CodexConfig,
    JauntConfig,
    LLMConfig,
    PathsConfig,
    PromptsConfig,
    TestConfig,
    load_config,
)
from jaunt.generate import fingerprint


def _config(*, engine: str = "codex", codex: CodexConfig | None = None) -> JauntConfig:
    return JauntConfig(
        version=1,
        paths=PathsConfig(
            source_roots=["src"],
            test_roots=["tests"],
            generated_dir="__generated__",
        ),
        llm=LLMConfig(
            provider="openai",
            model="gpt-5.2",
            api_key_env="OPENAI_API_KEY",
        ),
        build=BuildConfig(jobs=1, infer_deps=True),
        test=TestConfig(jobs=1, infer_deps=True, pytest_args=["-q"]),
        prompts=PromptsConfig(
            build_system="",
            build_module="",
            test_system="",
            test_module="",
        ),
        agent=AgentConfig(engine=engine),
        codex=codex
        or CodexConfig(
            model="gpt-5.5",
            reasoning_effort="high",
            sandbox="workspace-write",
        ),
    )


def test_codex_fingerprint_includes_prompt_template_content(monkeypatch) -> None:
    prompt_version = "one"

    def fake_load_prompt(default_name: str, override_path: str | None) -> str:
        return f"{prompt_version}:{default_name}:{override_path or ''}"

    monkeypatch.setattr(fingerprint, "load_prompt", fake_load_prompt)

    cfg = _config(engine="codex")
    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    prompt_version = "two"
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert first != second


def test_codex_fingerprint_respects_prompt_overrides(tmp_path: Path) -> None:
    first_prompt = tmp_path / "build_module.md"
    first_prompt.write_text("first", encoding="utf-8")
    cfg = _config(engine="codex")
    cfg = replace(
        cfg,
        prompts=replace(cfg.prompts, build_module=str(first_prompt)),
    )

    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    first_prompt.write_text("second", encoding="utf-8")
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert first != second


def test_codex_fingerprint_respects_build_preamble_override(tmp_path: Path) -> None:
    """Editing the Jaunt preamble (build_preamble) must invalidate already-built modules."""
    preamble = tmp_path / "codex_preamble.md"
    preamble.write_text("preamble one", encoding="utf-8")
    cfg = _config(engine="codex")
    cfg = replace(cfg, prompts=replace(cfg.prompts, build_preamble=str(preamble)))

    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    preamble.write_text("preamble two — materially different", encoding="utf-8")
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert first != second


def test_codex_fingerprint_changes_when_overview_enabled() -> None:
    """Enabling context.overview must change the build fingerprint (feature takes effect)."""
    cfg_off = _config(engine="codex")
    cfg_on = replace(cfg_off, context=replace(cfg_off.context, overview=True))

    off = fingerprint.generation_fingerprint_from_config(
        cfg_off,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    on = fingerprint.generation_fingerprint_from_config(
        cfg_on,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    assert off != on, "enabling the project overview must invalidate already-built modules"

    # The overview is build-only orientation: the test-kind fingerprint must be unaffected
    # so `jaunt build` and `jaunt test` agree on freshness.
    off_test = fingerprint.generation_fingerprint_from_config(
        cfg_off,
        kind="test",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    on_test = fingerprint.generation_fingerprint_from_config(
        cfg_on,
        kind="test",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    assert off_test == on_test, "overview flag must not affect the test fingerprint"


def test_codex_fingerprint_uses_project_relative_prompt_overrides(
    monkeypatch, tmp_path: Path
) -> None:
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    (project / "src").mkdir(parents=True)
    (project / "prompts").mkdir()
    outside.mkdir()
    (project / "prompts" / "build.md").write_text("project prompt", encoding="utf-8")
    (project / "jaunt.toml").write_text(
        "\n".join(
            [
                "version = 1",
                "",
                "[paths]",
                'source_roots = ["src"]',
                "",
                "[prompts]",
                'build_module = "prompts/build.md"',
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(outside)

    cfg = load_config(root=project)
    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert cfg.prompts.build_module == str((project / "prompts" / "build.md").resolve())
    assert first == second


def test_codex_fingerprint_includes_cli_version() -> None:
    # Opt-in since 1.3.1: the default no longer embeds the CLI version.
    cfg = _config(engine="codex", codex=CodexConfig(fingerprint_cli_version=True))
    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.1",
    )

    assert first != second


def test_codex_fingerprint_is_stable_for_identical_inputs() -> None:
    cfg = _config(engine="codex")
    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert first == second


def test_include_target_tests_does_not_change_global_build_fingerprint() -> None:
    cfg = _config(engine="codex")
    disabled = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        include_target_tests=False,
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    enabled = fingerprint.generation_fingerprint_from_config(
        replace(cfg, build=replace(cfg.build, include_target_tests=True)),
        kind="build",
        include_target_tests=True,
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )

    assert enabled == disabled


def test_codex_fingerprint_cli_version_switch_can_disable_churn() -> None:
    cfg = _config(
        engine="codex",
        codex=CodexConfig(fingerprint_cli_version=False),
    )
    first = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.0",
    )
    second = fingerprint.generation_fingerprint_from_config(
        cfg,
        kind="build",
        codex_version_resolver=lambda: "codex-cli 1.0.1",
    )

    assert first == second


def test_non_codex_fingerprint_includes_prompt_templates(monkeypatch) -> None:
    prompt_version = "one"

    def fake_load_prompt(default_name: str, override_path: str | None) -> str:
        return f"{prompt_version}:{default_name}:{override_path or ''}"

    monkeypatch.setattr(fingerprint, "load_prompt", fake_load_prompt)

    cfg = _config(engine="legacy")
    first = fingerprint.generation_fingerprint_from_config(cfg, kind="build")
    prompt_version = "two"
    second = fingerprint.generation_fingerprint_from_config(cfg, kind="build")

    assert first != second
