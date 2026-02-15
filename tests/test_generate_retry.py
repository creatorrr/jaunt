from __future__ import annotations

import asyncio

from jaunt.generate.base import GeneratorBackend, ModuleSpecContext


class DummyBackend(GeneratorBackend):
    def __init__(self) -> None:
        self.calls: int = 0
        self.extra_contexts: list[list[str] | None] = []

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, None]:
        self.calls += 1
        self.extra_contexts.append(extra_error_context)
        if self.calls == 1:
            # Valid Python but missing the required symbol.
            return "def not_it():\n    return 1\n", None
        return "def foo():\n    return 1\n", None


def test_generate_with_retry_calls_twice_and_succeeds() -> None:
    backend = DummyBackend()
    ctx = ModuleSpecContext(
        kind="build",
        spec_module="pkg.specs",
        generated_module="__generated__.pkg.specs",
        expected_names=["foo"],
        spec_sources={},
        decorator_prompts={},
        dependency_apis={},
        dependency_generated_modules={},
    )

    res = asyncio.run(backend.generate_with_retry(ctx))
    assert backend.calls == 2
    assert res.attempts == 2
    assert res.source is not None and "def foo" in res.source
    assert res.errors == []

    # First attempt has no extra context; second attempt should.
    assert backend.extra_contexts[0] is None
    assert backend.extra_contexts[1] is not None
    assert any("previous output errors:" in s for s in backend.extra_contexts[1] or [])


def test_base_backend_supports_structured_output_default_false() -> None:
    backend = DummyBackend()
    assert backend.supports_structured_output is False
