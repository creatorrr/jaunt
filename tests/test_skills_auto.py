from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.config import AgentConfig, CodexConfig, LLMConfig, SkillsConfig
from jaunt.external_imports import discover_external_distributions
from jaunt.skills_auto import _format_generated_skill_file, ensure_pypi_skills, skill_md_path


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_scan_external_imports_filters_stdlib_and_internal(tmp_path: Path, monkeypatch) -> None:
    src = tmp_path / "src"
    _write(src / "my_app" / "__init__.py", "")
    _write(
        src / "my_app" / "mod.py",
        "\n".join(
            [
                "import os",
                "import jaunt",
                "import my_app",
                "import external_lib",
                "from external_lib.sub import thing",
                "from . import rel",  # relative import should be ignored
                "",
            ]
        ),
    )

    import jaunt.external_imports as ei

    def fake_packages_distributions():
        return {"external_lib": ["external-lib"], "my_app": ["my-app"], "jaunt": ["jaunt"]}

    def fake_version(name: str) -> str:
        if name == "external-lib":
            return "1.2.3"
        if name == "jaunt":
            return "0.1.0"
        raise ei.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(ei.metadata, "packages_distributions", fake_packages_distributions)
    monkeypatch.setattr(ei.metadata, "version", fake_version)

    dists = discover_external_distributions([src], generated_dir="__generated__")
    assert dists == {"external-lib": "1.2.3"}
    assert "jaunt" not in dists


def test_skill_path_layout(tmp_path: Path) -> None:
    p = skill_md_path(project_root=tmp_path, dist="typing_extensions")
    assert p == (tmp_path / ".agents" / "skills" / "typing-extensions" / "SKILL.md").resolve()


def test_frontmatter_roundtrip():
    from jaunt.skills_auto import _format_generated_skill_file, parse_generated_skill_meta

    text = _format_generated_skill_file(dist="httpx", version="0.25.0", body_md="# httpx\nbody\n")
    assert text.startswith("---\n")
    assert parse_generated_skill_meta(text) == ("httpx", "0.25.0")
    assert parse_generated_skill_meta("# just a heading\n") is None


def test_auto_false_disables_injection(tmp_path: Path, monkeypatch) -> None:
    import jaunt.skills_auto as sa

    discovery_called = False

    def fail_discover(*_a, **_k):
        nonlocal discovery_called
        discovery_called = True
        raise AssertionError("discovery should not be called")

    monkeypatch.setattr(sa, "discover_external_distributions_with_warnings", fail_discover)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            skills=SkillsConfig(auto=False),
        )
    )
    assert res.warnings == []
    assert res.generation_failures == 0
    assert res.dists == {}
    assert discovery_called is False


def test_existing_generated_skill_same_version_skips_regen(tmp_path: Path, monkeypatch) -> None:
    dist = "external-lib"
    version = "1.2.3"
    path = skill_md_path(project_root=tmp_path, dist=dist)
    _write(path, _format_generated_skill_file(dist=dist, version=version, body_md="BODY"))

    import jaunt.skills_auto as sa

    def fake_discover(*_a, **_k):
        return {dist: version}, []

    def fail_fetch(*_a, **_k):
        raise AssertionError("fetch_readme called")

    monkeypatch.setattr(sa, "discover_external_distributions_with_warnings", fake_discover)
    monkeypatch.setattr(sa, "fetch_readme", fail_fetch)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
        )
    )
    assert res.warnings == []
    assert res.dists == {dist: version}
    assert path.is_file()
    on_disk = path.read_text(encoding="utf-8")
    assert "BODY" in on_disk
    assert "x-jaunt-dist" in on_disk


def test_existing_generated_skill_version_change_regenerates(tmp_path: Path, monkeypatch) -> None:
    dist = "external-lib"
    old_version = "0.1.0"
    new_version = "1.2.3"
    path = skill_md_path(project_root=tmp_path, dist=dist)
    _write(path, _format_generated_skill_file(dist=dist, version=old_version, body_md="OLD"))

    import jaunt.skillgen as sg
    import jaunt.skills_auto as sa

    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: ({dist: new_version}, []),
    )
    monkeypatch.setattr(sa, "fetch_readme", lambda *_a, **_k: ("README", "text/markdown"))

    calls: list[tuple[str, str]] = []

    class DummyGen:
        def __init__(self, llm, agent, codex):  # noqa: ANN001
            self.llm = llm

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            calls.append((dist, version))
            return "NEW SKILL"

    monkeypatch.setattr(sg, "CodexSkillGenerator", DummyGen)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
        )
    )
    assert calls == [(dist, new_version)]

    on_disk = path.read_text(encoding="utf-8")
    assert f"x-jaunt-version: {new_version}" in on_disk
    assert "x-jaunt-dist" in on_disk
    assert "NEW SKILL" in on_disk
    assert res.dists == {dist: new_version}


def test_resolve_dist_by_name_heuristic_is_memoized(monkeypatch) -> None:
    """_resolve_dist_by_name_heuristic should cache results to avoid repeated metadata lookups."""
    import jaunt.external_imports as ei

    # Clear any prior cache state.
    ei._resolve_dist_by_name_heuristic.cache_clear()

    call_count = 0

    def counting_version(name: str) -> str:
        nonlocal call_count
        call_count += 1
        if name == "requests":
            return "2.31.0"
        raise ei.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(ei.metadata, "version", counting_version)

    # Call twice with the same input
    r1 = ei._resolve_dist_by_name_heuristic("requests")
    r2 = ei._resolve_dist_by_name_heuristic("requests")
    assert r1 == ("requests", "2.31.0")
    assert r1 == r2
    # Second call should be cached — only 1 metadata.version call
    assert call_count == 1

    # Clean up cache so other tests aren't affected.
    ei._resolve_dist_by_name_heuristic.cache_clear()


def test_skill_generation_runs_concurrently(tmp_path: Path, monkeypatch) -> None:
    """When multiple skills need generation, they should be generated concurrently."""
    import jaunt.skillgen as sg
    import jaunt.skills_auto as sa

    dists = {"lib-a": "1.0.0", "lib-b": "2.0.0", "lib-c": "3.0.0"}
    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: (dists, []),
    )
    monkeypatch.setattr(sa, "fetch_readme", lambda *_a, **_k: ("README", "text/markdown"))

    generation_order: list[str] = []
    concurrency_high_water: list[int] = [0]
    active_count = [0]

    class ConcurrencyTrackingGen:
        def __init__(self, llm, agent, codex):  # noqa: ANN001
            pass

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            active_count[0] += 1
            concurrency_high_water[0] = max(concurrency_high_water[0], active_count[0])
            await asyncio.sleep(0.01)  # Simulate async work
            generation_order.append(dist)
            active_count[0] -= 1
            return f"SKILL for {dist}"

    monkeypatch.setattr(sg, "CodexSkillGenerator", ConcurrencyTrackingGen)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
        )
    )
    assert res.warnings == []
    # All 3 skills should have been generated
    assert len(generation_order) == 3
    # With parallel generation, concurrency should be > 1
    assert concurrency_high_water[0] > 1, (
        f"Expected concurrent generation but high water mark was {concurrency_high_water[0]}"
    )
    for dist, version in dists.items():
        path = skill_md_path(project_root=tmp_path, dist=dist)
        assert path.is_file()
        on_disk = path.read_text(encoding="utf-8")
        assert f"x-jaunt-version: {version}" in on_disk
        assert "x-jaunt-dist" in on_disk


def test_no_pypi_dists_leaves_user_skills_untouched(tmp_path: Path, monkeypatch) -> None:
    # Create a user skill (NOT matching any detected PyPI dist)
    path = tmp_path / ".agents" / "skills" / "my-internal-api" / "SKILL.md"
    _write(path, "# my-internal-api\nInternal API docs\n")

    import jaunt.skills_auto as sa

    # No PyPI dists detected at all
    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: ({}, []),
    )

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert res.dists == {}
    assert path.read_text(encoding="utf-8") == "# my-internal-api\nInternal API docs\n"


def test_user_managed_skill_never_overwritten(tmp_path: Path, monkeypatch) -> None:
    dist = "external-lib"
    version = "9.9.9"
    path = skill_md_path(project_root=tmp_path, dist=dist)
    _write(path, "USER SKILL\n")

    import jaunt.skills_auto as sa

    def fake_discover(*_a, **_k):
        return {dist: version}, []

    def fail_fetch(*_a, **_k):
        raise AssertionError("fetch_readme called")

    monkeypatch.setattr(sa, "discover_external_distributions_with_warnings", fake_discover)
    monkeypatch.setattr(sa, "fetch_readme", fail_fetch)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert path.read_text(encoding="utf-8") == "USER SKILL\n"
    assert res.dists == {dist: version}


def test_is_local_install_detects_dir_info(monkeypatch) -> None:
    import jaunt.skills_auto as sa

    class FakeDist:
        def read_text(self, name: str) -> str:
            assert name == "direct_url.json"
            return '{"url": "file:///work/pkg", "dir_info": {"editable": true}}'

    monkeypatch.setattr(sa.metadata, "distribution", lambda name: FakeDist())
    assert sa._is_local_install("memory-store-utils") is True


def test_is_local_install_detects_file_url(monkeypatch) -> None:
    import jaunt.skills_auto as sa

    class FakeDist:
        def read_text(self, name: str) -> str:
            return '{"url": "file:///tmp/wheels/foo-1.0-py3-none-any.whl", "archive_info": {}}'

    monkeypatch.setattr(sa.metadata, "distribution", lambda name: FakeDist())
    assert sa._is_local_install("foo") is True


def test_is_local_install_false_for_index_install(monkeypatch) -> None:
    import jaunt.skills_auto as sa

    class FakeDist:
        def read_text(self, name: str) -> None:
            # Index installs have no direct_url.json; importlib returns None.
            return None

    monkeypatch.setattr(sa.metadata, "distribution", lambda name: FakeDist())
    assert sa._is_local_install("httpx") is False


def test_is_local_install_false_when_distribution_missing(monkeypatch) -> None:
    import jaunt.skills_auto as sa

    def missing(name: str):
        raise sa.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(sa.metadata, "distribution", missing)
    assert sa._is_local_install("nope") is False


def test_workspace_internal_dist_skips_pypi_lookup(tmp_path: Path, monkeypatch) -> None:
    import jaunt.skills_auto as sa

    dist, version = "memory-store-utils", "0.1.0"
    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: ({dist: version}, []),
    )
    monkeypatch.setattr(sa, "_is_local_install", lambda d: d == dist)

    def fail_fetch(*_a, **_k):
        raise AssertionError("fetch_readme must not run for a workspace-internal dist")

    monkeypatch.setattr(sa, "fetch_readme", fail_fetch)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
        )
    )

    # Skipped quietly: no warning, no failure, and no skill written.
    assert res.warnings == []
    assert res.generation_failures == 0
    assert res.dists == {dist: version}
    assert not skill_md_path(project_root=tmp_path, dist=dist).exists()


def test_missing_heading_warnings_deduped_into_summary(tmp_path: Path, monkeypatch) -> None:
    import jaunt.skillgen as sg
    import jaunt.skills_auto as sa

    dists = {"lib-a": "1.0.0", "lib-b": "2.0.0"}
    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: (dists, []),
    )
    monkeypatch.setattr(sa, "fetch_readme", lambda *_a, **_k: ("README", "text/markdown"))
    monkeypatch.setattr(sa, "_is_local_install", lambda d: False)

    class HeadingFailGen:
        def __init__(self, llm, agent, codex):  # noqa: ANN001
            pass

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            from jaunt.skill_agent import validate_skill_markdown

            errs = validate_skill_markdown("# skill\nno required sections here\n")
            raise RuntimeError("; ".join(errs))

    monkeypatch.setattr(sg, "CodexSkillGenerator", HeadingFailGen)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
        )
    )

    heading_warnings = [w for w in res.warnings if "heading" in w.lower()]
    assert len(heading_warnings) == 1
    summary = heading_warnings[0]
    assert "lib-a" in summary
    assert "lib-b" in summary
    # Both dists still counted as generation failures.
    assert res.generation_failures == 2


def test_codex_skill_generator_selected_when_agent_engine_is_codex(
    tmp_path: Path, monkeypatch
) -> None:
    import jaunt.skillgen as sg
    import jaunt.skills_auto as sa

    dist = "external-lib"
    version = "1.2.3"
    monkeypatch.setattr(
        sa,
        "discover_external_distributions_with_warnings",
        lambda *_a, **_k: ({dist: version}, []),
    )
    monkeypatch.setattr(sa, "fetch_readme", lambda *_a, **_k: ("README", "text/markdown"))

    calls: list[tuple[str, str]] = []

    class DummyCodexGen:
        def __init__(self, llm, agent, codex):  # noqa: ANN001
            calls.append(("init", agent.engine))

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            calls.append((dist, version))
            return "\n".join(
                [
                    "# skill",
                    "## What it is",
                    "x",
                    "## Core concepts",
                    "y",
                    "## Common patterns",
                    "z",
                    "## Gotchas",
                    "g",
                    "## Testing notes",
                    "t",
                    "",
                ]
            )

    monkeypatch.setattr(sg, "CodexSkillGenerator", DummyCodexGen)

    res = asyncio.run(
        ensure_pypi_skills(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
            agent=AgentConfig(engine="codex"),
            codex=CodexConfig(),
        )
    )
    assert res.warnings == []
    assert calls[0] == ("init", "codex")
    assert calls[1] == (dist, version)
    on_disk = skill_md_path(project_root=tmp_path, dist=dist).read_text(encoding="utf-8")
    assert "x-jaunt-dist" in on_disk
    assert "x" in on_disk
