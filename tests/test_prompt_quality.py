"""Tests that prompt templates contain critical quality guidance.

These tests validate that the rendered prompts include guidance for:
- Spec interpretation (docstrings, signatures, type hints)
- Code quality (type annotations, imports)
- Decorator prompt explanation
- Dependency import paths
- Test quality (happy path, edge cases, assertion quality)
"""

from __future__ import annotations

from jaunt.generate.base import ModuleSpecContext
from jaunt.generate.shared import async_test_info, fmt_kv_block, load_prompt, render_template


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
        module_contract_block="# Mark\nkind: class\nsignature: class Mark(StrEnum)\n",
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
        module_contract_block="# Mark\nkind: class\nsignature: class Mark(StrEnum)\n",
    )
    defaults.update(overrides)
    return ModuleSpecContext(**defaults)


def _render(ctx: ModuleSpecContext) -> tuple[str, str]:
    """Return (system_text, user_text) by rendering the packaged prompt templates.

    Backend-independent: mirrors how prompt templates are filled from a
    ``ModuleSpecContext`` so quality assertions track the templates themselves.
    """
    spec_items: list[tuple[str, str]] = []
    for ref, source in sorted(ctx.spec_sources.items(), key=lambda kv: str(kv[0])):
        prompt = ctx.decorator_prompts.get(ref)
        if prompt:
            source = f"{source.rstrip()}\n\n# Decorator prompt\n{prompt.rstrip()}\n"
        spec_items.append((str(ref), source))

    deps_api_items = [
        (str(r), a) for r, a in sorted(ctx.dependency_apis.items(), key=lambda kv: str(kv[0]))
    ]
    deps_gen_items = [
        (m, s) for m, s in sorted(ctx.dependency_generated_modules.items(), key=lambda kv: kv[0])
    ]
    decorator_api_items = [
        (str(r), a) for r, a in sorted(ctx.decorator_apis.items(), key=lambda kv: str(kv[0]))
    ]

    mapping = {
        "spec_module": ctx.spec_module,
        "generated_module": ctx.generated_module,
        "expected_names": ", ".join(ctx.expected_names),
        "specs_block": fmt_kv_block(spec_items),
        "deps_api_block": fmt_kv_block(deps_api_items),
        "deps_generated_block": fmt_kv_block(deps_gen_items),
        "decorator_apis_block": fmt_kv_block(decorator_api_items),
        "module_contract_block": ctx.module_contract_block or "(none)\n",
        "base_contract_block": ctx.base_contract_block or "(none)\n",
        "blueprint_source_block": ctx.blueprint_source or "(none)\n",
        "build_instructions_block": ctx.build_instructions_block or "(none)\n",
        "attached_test_specs_block": ctx.attached_test_specs_block or "(none)\n",
        "package_context_block": ctx.package_context_block or "(none)\n",
        "error_context_block": fmt_kv_block([]),
        "async_test_info": async_test_info(ctx.async_runner),
    }

    if ctx.kind == "build":
        system_t = load_prompt("build_system.md", None)
        user_t = load_prompt("build_module.md", None)
    else:
        system_t = load_prompt("test_system.md", None)
        user_t = load_prompt("test_module.md", None)

    system = render_template(system_t, mapping).strip() + "\n"
    user = render_template(user_t, mapping).strip() + "\n"
    return system, user


# ---------------------------------------------------------------------------
# Build system prompt
# ---------------------------------------------------------------------------


def test_build_system_spec_interpretation_guidance(monkeypatch) -> None:
    """Build system prompt should guide the LLM to read spec docstrings and signatures."""
    system, _user = _render(_build_ctx())
    text = system.lower()
    assert "docstring" in text
    assert "type hint" in text or "type annotation" in text
    assert "signature" in text or "parameter" in text


def test_build_system_code_quality_guidance(monkeypatch) -> None:
    """Build system prompt should set code quality expectations (type annotations, imports)."""
    system, _user = _render(_build_ctx())
    text = system.lower()
    assert "type annotation" in text or "type hint" in text
    assert "import" in text


# ---------------------------------------------------------------------------
# Build module (user) prompt
# ---------------------------------------------------------------------------


def test_build_module_decorator_prompt_explanation(monkeypatch) -> None:
    """Build user prompt should explain what '# Decorator prompt' sections mean."""
    _system, user = _render(_build_ctx())
    text = user.lower()
    assert "decorator prompt" in text
    assert "instruction" in text or "user-provided" in text or "supplement" in text


def test_build_module_import_guidance(monkeypatch) -> None:
    """Build user prompt should explain how to import from dependency modules."""
    _system, user = _render(_build_ctx())
    text = user.lower()
    assert "import" in text
    assert "<module>" in text or "module" in text


def test_build_module_decorator_api_guidance(monkeypatch) -> None:
    """Build user prompt should explain decorator-derived API context."""
    _system, user = _render(_build_ctx())
    text = user.lower()
    assert "decorator dependency apis" in text
    assert "effective_signature" in text


def test_build_module_spec_reading_guidance(monkeypatch) -> None:
    """Build user prompt should tell the LLM how to read specs (docstrings, signatures)."""
    _system, user = _render(_build_ctx())
    text = user.lower()
    assert "docstring" in text
    assert "signature" in text or "parameter" in text


def test_build_module_mentions_handwritten_symbol_reuse(monkeypatch) -> None:
    _system, user = _render(_build_ctx())
    text = user.lower()
    assert "handwritten source-module symbols" in text
    assert "do not redefine" in text


def test_build_module_mentions_additional_build_instructions(monkeypatch) -> None:
    _system, user = _render(
        _build_ctx(build_instructions_block="- Prefer composable helpers.\n"),
    )
    text = user.lower()
    assert "additional build instructions" in text
    assert "composable helpers" in text


# ---------------------------------------------------------------------------
# Test system prompt
# ---------------------------------------------------------------------------


def test_test_system_test_quality_guidance(monkeypatch) -> None:
    """Test system prompt should include test quality guidance (edge cases, assertions)."""
    system, _user = _render(_test_ctx())
    text = system.lower()
    assert "edge case" in text or "boundary" in text
    assert "assert" in text


# ---------------------------------------------------------------------------
# Test module (user) prompt
# ---------------------------------------------------------------------------


def test_test_module_testing_strategy_guidance(monkeypatch) -> None:
    """Test user prompt should guide on testing strategy (happy path, edge cases, assertions)."""
    _system, user = _render(_test_ctx())
    text = user.lower()
    assert "happy path" in text or "normal" in text or "expected" in text
    assert "edge case" in text or "error" in text or "boundary" in text
    assert "assert" in text


def test_test_module_import_path_guidance(monkeypatch) -> None:
    """Test user prompt should explain the <module>:<qualname> import convention."""
    _system, user = _render(_test_ctx())
    assert "<module>:<qualname>" in user or "<module>" in user


def test_test_module_mentions_public_api_only_policy(monkeypatch) -> None:
    _system, user = _render(_test_ctx())
    text = user.lower()
    assert "public api only" in text
    assert "wrapper internals" in text or "generated module internals" in text


# ---------------------------------------------------------------------------
# Preserved rules (regression guards)
# ---------------------------------------------------------------------------


def test_build_prompts_no_test_rule(monkeypatch) -> None:
    """Build prompts must still tell the LLM not to generate tests."""
    system, user = _render(_build_ctx())
    assert "Do not write tests" in system or "Do not generate tests" in user


def test_test_prompts_test_only_rule(monkeypatch) -> None:
    """Test prompts must still tell the LLM to generate tests only."""
    system, user = _render(_test_ctx())
    assert "tests only" in system or "Generate tests only" in user
    assert "Do not guess" in user
