"""Snapshot tests for rendered prompts.

Approve snapshot changes with:
    pytest --snapshot-update tests/test_prompt_snapshots.py
"""

from __future__ import annotations

import pytest

try:
    from syrupy.extensions.amber import AmberSnapshotExtension
except ImportError:  # pragma: no cover - optional dev dependency
    pytest.skip("syrupy is not installed", allow_module_level=True)

from jaunt.config import LLMConfig
from jaunt.generate.base import ModuleSpecContext
from jaunt.generate.openai_backend import OpenAIBackend
from jaunt.spec_ref import normalize_spec_ref


class PromptSnapshotExtension(AmberSnapshotExtension):
    snapshot_dirname = "snapshots"


def _backend(monkeypatch: pytest.MonkeyPatch) -> OpenAIBackend:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    return OpenAIBackend(
        LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY")
    )


def _ctx(kind: str) -> ModuleSpecContext:
    foo_ref = normalize_spec_ref("pkg.specs:foo")
    dep_ref = normalize_spec_ref("pkg.deps:normalize")
    return ModuleSpecContext(
        kind=kind,  # type: ignore[arg-type]
        spec_module="pkg.specs",
        generated_module="pkg.__generated__.specs",
        expected_names=["foo", "bar"],
        spec_sources={
            foo_ref: (
                "def foo(x: int) -> int:\n"
                "    \"\"\"Return x + 1.\"\"\"\n"
                "    raise RuntimeError()\n"
            )
        },
        decorator_prompts={foo_ref: "Prefer straightforward, readable code."},
        dependency_apis={dep_ref: "def normalize(s: str) -> str: ...\n"},
        dependency_generated_modules={"pkg.deps": "def normalize(s: str) -> str:\n    return s\n"},
    )


def test_build_prompt_snapshot(monkeypatch: pytest.MonkeyPatch, snapshot) -> None:
    backend = _backend(monkeypatch)
    messages = backend._render_messages(
        _ctx("build"), extra_error_context=["missing import for math"]
    )
    snap = snapshot.with_defaults(extension_class=PromptSnapshotExtension)
    assert messages == snap


def test_test_prompt_snapshot(monkeypatch: pytest.MonkeyPatch, snapshot) -> None:
    backend = _backend(monkeypatch)
    messages = backend._render_messages(_ctx("test"), extra_error_context=["missing pytest import"])
    snap = snapshot.with_defaults(extension_class=PromptSnapshotExtension)
    assert messages == snap
