"""Tests that prompt templates contain critical quality guidance.

These tests validate that the rendered prompts include guidance for:
- Spec interpretation (docstrings, signatures, type hints)
- Code quality (type annotations, imports)
- Decorator prompt explanation
- Dependency import paths
- Test quality (happy path, edge cases, assertion quality)
"""

from __future__ import annotations

from jaunt.config import LLMConfig
from jaunt.generate.base import ModuleSpecContext
from jaunt.generate.openai_backend import OpenAIBackend


def _backend(monkeypatch) -> OpenAIBackend:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )


def _build_ctx(**overrides) -> ModuleSpecContext:
    defaults: dict = dict(
        kind="build",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo", "bar"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )
    defaults.update(overrides)
    return ModuleSpecContext(**defaults)


def _test_ctx(**overrides) -> ModuleSpecContext:
    defaults: dict = dict(
        kind="test",
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["test_foo", "test_bar"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )
    defaults.update(overrides)
    return ModuleSpecContext(**defaults)


def _render(backend: OpenAIBackend, ctx: ModuleSpecContext) -> tuple[str, str]:
    """Return (system_text, user_text) from rendered messages."""
    msgs = backend._render_messages(ctx, extra_error_context=None)
    system = msgs[0]["content"]
    user = msgs[-1]["content"]
    return system, user


# ---------------------------------------------------------------------------
# Build system prompt
# ---------------------------------------------------------------------------


def test_build_system_spec_interpretation_guidance(monkeypatch) -> None:
    """Build system prompt should guide the LLM to read spec docstrings and signatures."""
    backend = _backend(monkeypatch)
    system, _user = _render(backend, _build_ctx())
    text = system.lower()
    assert "docstring" in text
    assert "type hint" in text or "type annotation" in text
    assert "signature" in text or "parameter" in text


def test_build_system_code_quality_guidance(monkeypatch) -> None:
    """Build system prompt should set code quality expectations (type annotations, imports)."""
    backend = _backend(monkeypatch)
    system, _user = _render(backend, _build_ctx())
    text = system.lower()
    assert "type annotation" in text or "type hint" in text
    assert "import" in text


# ---------------------------------------------------------------------------
# Build module (user) prompt
# ---------------------------------------------------------------------------


def test_build_module_decorator_prompt_explanation(monkeypatch) -> None:
    """Build user prompt should explain what '# Decorator prompt' sections mean."""
    backend = _backend(monkeypatch)
    _system, user = _render(backend, _build_ctx())
    text = user.lower()
    assert "decorator prompt" in text
    assert "instruction" in text or "user-provided" in text or "supplement" in text


def test_build_module_import_guidance(monkeypatch) -> None:
    """Build user prompt should explain how to import from dependency modules."""
    backend = _backend(monkeypatch)
    _system, user = _render(backend, _build_ctx())
    text = user.lower()
    assert "import" in text
    assert "<module>" in text or "module" in text


def test_build_module_spec_reading_guidance(monkeypatch) -> None:
    """Build user prompt should tell the LLM how to read specs (docstrings, signatures)."""
    backend = _backend(monkeypatch)
    _system, user = _render(backend, _build_ctx())
    text = user.lower()
    assert "docstring" in text
    assert "signature" in text or "parameter" in text


# ---------------------------------------------------------------------------
# Test system prompt
# ---------------------------------------------------------------------------


def test_test_system_test_quality_guidance(monkeypatch) -> None:
    """Test system prompt should include test quality guidance (edge cases, assertions)."""
    backend = _backend(monkeypatch)
    system, _user = _render(backend, _test_ctx())
    text = system.lower()
    assert "edge case" in text or "boundary" in text
    assert "assert" in text


# ---------------------------------------------------------------------------
# Test module (user) prompt
# ---------------------------------------------------------------------------


def test_test_module_testing_strategy_guidance(monkeypatch) -> None:
    """Test user prompt should guide on testing strategy (happy path, edge cases, assertions)."""
    backend = _backend(monkeypatch)
    _system, user = _render(backend, _test_ctx())
    text = user.lower()
    assert "happy path" in text or "normal" in text or "expected" in text
    assert "edge case" in text or "error" in text or "boundary" in text
    assert "assert" in text


def test_test_module_import_path_guidance(monkeypatch) -> None:
    """Test user prompt should explain the <module>:<qualname> import convention."""
    backend = _backend(monkeypatch)
    _system, user = _render(backend, _test_ctx())
    assert "<module>:<qualname>" in user or "<module>" in user


# ---------------------------------------------------------------------------
# Preserved rules (regression guards)
# ---------------------------------------------------------------------------


def test_build_prompts_no_test_rule(monkeypatch) -> None:
    """Build prompts must still tell the LLM not to generate tests."""
    backend = _backend(monkeypatch)
    system, user = _render(backend, _build_ctx())
    assert "Do not write tests" in system or "Do not generate tests" in user


def test_test_prompts_test_only_rule(monkeypatch) -> None:
    """Test prompts must still tell the LLM to generate tests only."""
    backend = _backend(monkeypatch)
    system, user = _render(backend, _test_ctx())
    assert "tests only" in system or "Generate tests only" in user
    assert "Do not guess" in user
