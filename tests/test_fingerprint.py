from __future__ import annotations

from jaunt.config import (
    AgentConfig,
    BuildConfig,
    CodexConfig,
    JauntConfig,
    LLMConfig,
    PathsConfig,
    PromptsConfig,
    TestConfig,
)
from jaunt.generate import fingerprint


def _config(*, engine: str = "codex") -> JauntConfig:
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
        codex=CodexConfig(
            model="gpt-5.5",
            reasoning_effort="high",
            sandbox="workspace-write",
        ),
    )


def test_codex_fingerprint_does_not_read_prompt_templates(monkeypatch) -> None:
    calls: list[tuple[str, str | None]] = []

    def fake_load_prompt(default_name: str, override_path: str | None) -> str:
        calls.append((default_name, override_path))
        return f"changed:{default_name}"

    monkeypatch.setattr(fingerprint, "load_prompt", fake_load_prompt)

    cfg = _config(engine="codex")
    first_build = fingerprint.generation_fingerprint_from_config(cfg, kind="build")
    second_build = fingerprint.generation_fingerprint_from_config(cfg, kind="build")
    fingerprint.generation_fingerprint_from_config(cfg, kind="test")

    assert first_build == second_build
    assert calls == []


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
