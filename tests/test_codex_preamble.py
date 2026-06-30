"""Tests for the static Jaunt preamble prepended to CodexBackend build prompts."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jaunt.config import CodexConfig, LLMConfig
from jaunt.generate.codex_backend import CodexBackend


def _ctx(**overrides):
    values = {
        "kind": "build",
        "generated_module": "pkg.__generated__.thing",
        "expected_names": ["alpha", "beta"],
        "spec_sources": {"pkg.specs:alpha": "def alpha(): ...\n"},
        "dependency_apis": {"pkg.deps:helper": "def helper() -> str: ...\n"},
        "build_instructions_block": "",
        "module_contract_block": "",
        "base_contract_block": "",
        "package_context_block": "",
        "skills_digest": "",
        "seed_target_content": "",
        "whole_class_contract_block": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _backend() -> CodexBackend:
    return CodexBackend(
        CodexConfig(model="gpt-test"),
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
    )


def test_build_prompt_starts_with_preamble() -> None:
    """_build_prompt output must open with the packaged preamble text verbatim."""
    backend = _backend()
    ctx = _ctx()
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/thing.py"), None)

    # Load the expected preamble from the packaged file to avoid hard-coding it here.
    from jaunt.generate.shared import load_prompt

    preamble = load_prompt("codex_preamble.md", None).strip()
    assert prompt.startswith(preamble), (
        f"Prompt does not start with preamble.\n"
        f"Expected prefix:\n{preamble[:200]!r}\n\n"
        f"Actual start:\n{prompt[:200]!r}"
    )


def test_build_prompt_preamble_contains_required_terms() -> None:
    """Preamble must contain key Jaunt-orientation terms (case-insensitive)."""
    backend = _backend()
    ctx = _ctx()
    prompt = backend._build_prompt(ctx, Path("pkg/__generated__/thing.py"), None)
    lower = prompt.lower()

    required_terms = [
        "jaunt", "spec-driven", "signature", "docstring", "__generated__", "no placeholder"
    ]
    for term in required_terms:
        assert term.lower() in lower, f"Required term {term!r} not found in prompt"
