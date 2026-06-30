"""Tests for jaunt.repo_context.overview: digest-cached project overview generation."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from jaunt.repo_context.overview import (
    build_project_docs_block,
    load_or_build_overview,
    project_spec_digest,
)
from jaunt.registry import SpecEntry
from jaunt.spec_ref import normalize_spec_ref


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _entry(*, module: str, qualname: str, source_file: str) -> SpecEntry:
    return SpecEntry(
        kind="magic",
        spec_ref=normalize_spec_ref(f"{module}:{qualname}"),
        module=module,
        qualname=qualname,
        source_file=source_file,
        obj=object(),
        decorator_kwargs={},
    )


def _stub_prompts() -> object:
    """Tiny prompts stub — overview.py reads project_overview_system/user via getattr."""
    return SimpleNamespace(project_overview_system="", project_overview_user="")


# ---------------------------------------------------------------------------
# FakeBackend
# ---------------------------------------------------------------------------


class _FakeBackend:
    """Records how many times complete_text was called."""

    def __init__(self, response: str = "Generated overview prose.") -> None:
        self._response = response
        self.calls: int = 0

    async def complete_text(self, *, system: str, user: str) -> str:
        self.calls += 1
        return self._response


class _ErrorBackend:
    """Raises AssertionError if complete_text is ever called."""

    async def complete_text(self, *, system: str, user: str) -> str:
        raise AssertionError("complete_text must not be called when enabled=False")


class _UsageBackend:
    """Reports token usage so the cost-tracking path can be exercised."""

    def __init__(self, usage: object, response: str = "Overview prose.") -> None:
        self._usage = usage
        self._response = response
        self.calls: int = 0

    async def complete_text_with_usage(self, *, system: str, user: str) -> tuple[str, object]:
        self.calls += 1
        return self._response, self._usage


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cache_hit_skips_model(tmp_path: Path) -> None:
    """Two calls with the same digest must produce exactly one model call."""
    state_dir = tmp_path / ".jaunt_state"
    backend = _FakeBackend("My project overview text.")
    prompts = _stub_prompts()

    # First call — model is invoked.
    result1 = asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="module-a\nmodule-b\n",
            project_docs="# README\nA sample project.",
            digest="abc123",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert result1 == "My project overview text."
    assert backend.calls == 1

    # PROJECT_OVERVIEW.md must be written.
    overview_file = state_dir / "PROJECT_OVERVIEW.md"
    assert overview_file.is_file(), "PROJECT_OVERVIEW.md was not written"
    assert overview_file.read_text(encoding="utf-8") == "My project overview text."

    # Second call with same digest — cache hit, no second model call.
    result2 = asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="module-a\nmodule-b\n",
            project_docs="# README\nA sample project.",
            digest="abc123",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert result2 == "My project overview text."
    assert backend.calls == 1, f"expected 1 model call total, got {backend.calls}"


def test_disabled_returns_empty_string(tmp_path: Path) -> None:
    """When enabled=False the backend is never called and '' is returned."""
    state_dir = tmp_path / ".jaunt_state"
    backend = _ErrorBackend()

    result = asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="anything",
            project_docs="anything",
            digest="digest-xyz",
            state_dir=state_dir,
            enabled=False,
            prompts=_stub_prompts(),
        )
    )
    assert result == ""


def test_stale_digest_triggers_new_model_call(tmp_path: Path) -> None:
    """A changed digest must bypass the cache and call the model again."""
    state_dir = tmp_path / ".jaunt_state"
    backend = _FakeBackend("New overview.")
    prompts = _stub_prompts()

    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs",
            digest="digest-v1",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert backend.calls == 1

    # Different digest → model is called again.
    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs",
            digest="digest-v2",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert backend.calls == 2


def test_build_project_docs_block_reads_readme_and_agents(tmp_path: Path) -> None:
    """build_project_docs_block returns README and AGENTS.md intros."""
    (tmp_path / "README.md").write_text(
        "# My Project\nIntro line.\n\n## Installation\nIgnored.",
        encoding="utf-8",
    )
    (tmp_path / "AGENTS.md").write_text(
        "# Agents\nAgent intro.\n\n## Details\nIgnored.",
        encoding="utf-8",
    )

    block = build_project_docs_block(tmp_path, max_chars=1000)
    assert "My Project" in block
    assert "Intro line." in block
    assert "Agent intro." in block
    # Sections past the first '## ' heading must be excluded.
    assert "Installation" not in block
    assert "Details" not in block


def test_build_project_docs_block_caps_at_max_chars(tmp_path: Path) -> None:
    """Long intros are truncated to max_chars and get a truncation marker."""
    long_text = "# README\n" + "x" * 2000
    (tmp_path / "README.md").write_text(long_text, encoding="utf-8")

    block = build_project_docs_block(tmp_path, max_chars=100)
    assert "[truncated]" in block
    # Block itself should be much shorter than the original.
    assert len(block) < 300


def test_build_project_docs_block_no_docs(tmp_path: Path) -> None:
    """Returns '' when no README/AGENTS/CLAUDE files exist."""
    block = build_project_docs_block(tmp_path, max_chars=500)
    assert block == ""


def test_project_spec_digest_is_stable(tmp_path: Path) -> None:
    """Same inputs produce the same digest; different inputs produce different digests."""
    spec_file = tmp_path / "spec.py"
    spec_file.write_text("def foo(): ...", encoding="utf-8")

    entry = _entry(module="mymod", qualname="foo", source_file=str(spec_file))
    module_specs: dict[str, list[SpecEntry]] = {"mymod": [entry]}

    d1 = project_spec_digest(module_specs, "repo-map-a")
    d2 = project_spec_digest(module_specs, "repo-map-a")
    d3 = project_spec_digest(module_specs, "repo-map-b")

    assert d1 == d2, "digest must be deterministic"
    assert d1 != d3, "different repo_map_block must produce different digest"


def test_cache_invalidates_when_project_docs_change(tmp_path: Path) -> None:
    """Editing README/AGENTS (project_docs) must invalidate the overview cache."""
    state_dir = tmp_path / ".jaunt_state"
    backend = _FakeBackend("overview")
    prompts = _stub_prompts()

    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs-v1",
            digest="same-spec-digest",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert backend.calls == 1

    # Same spec/repo digest but different docs → the model must be called again.
    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs-v2",
            digest="same-spec-digest",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts,
        )
    )
    assert backend.calls == 2, "changed project_docs must bust the overview cache"


def test_cache_invalidates_when_prompt_template_changes(tmp_path: Path) -> None:
    """Editing the overview prompt template must invalidate the cache."""
    state_dir = tmp_path / ".jaunt_state"
    backend = _FakeBackend("overview")

    tmpl1 = tmp_path / "ov_system_v1.md"
    tmpl1.write_text("System prompt v1", encoding="utf-8")
    prompts1 = SimpleNamespace(project_overview_system=str(tmpl1), project_overview_user="")
    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs",
            digest="same",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts1,
        )
    )
    assert backend.calls == 1

    tmpl2 = tmp_path / "ov_system_v2.md"
    tmpl2.write_text("System prompt v2 — materially different", encoding="utf-8")
    prompts2 = SimpleNamespace(project_overview_system=str(tmpl2), project_overview_user="")
    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs",
            digest="same",
            state_dir=state_dir,
            enabled=True,
            prompts=prompts2,
        )
    )
    assert backend.calls == 2, "changed prompt template must bust the overview cache"


def test_overview_records_usage_against_cost_tracker(tmp_path: Path) -> None:
    """A fresh overview model call charges its token usage to the cost tracker."""
    from jaunt.cost import CostTracker
    from jaunt.generate.base import TokenUsage

    usage = TokenUsage(
        prompt_tokens=100,
        completion_tokens=20,
        model="gpt-5.5",
        provider="codex",
        cached_prompt_tokens=0,
    )
    backend = _UsageBackend(usage)
    tracker = CostTracker(max_cost=None)

    asyncio.run(
        load_or_build_overview(
            backend,
            repo_map_block="map",
            project_docs="docs",
            digest="d",
            state_dir=tmp_path / ".jaunt_state",
            enabled=True,
            prompts=_stub_prompts(),
            cost_tracker=tracker,
        )
    )
    assert backend.calls == 1
    assert tracker.api_calls == 1
    assert tracker.total_prompt_tokens == 100
    assert tracker.total_completion_tokens == 20
