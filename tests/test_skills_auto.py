from __future__ import annotations

import asyncio
from pathlib import Path

from jaunt.config import LLMConfig
from jaunt.external_imports import discover_external_distributions
from jaunt.skills_auto import ensure_pypi_skills_and_block, skill_md_path


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


def test_existing_generated_skill_same_version_skips_regen(tmp_path: Path, monkeypatch) -> None:
    dist = "external-lib"
    version = "1.2.3"
    path = skill_md_path(project_root=tmp_path, dist=dist)
    _write(path, f"<!-- jaunt:skill=pypi dist={dist} version={version} -->\nBODY\n")

    import jaunt.skills_auto as sa

    def fake_discover(*_a, **_k):
        return {dist: version}, []

    def fail_fetch(*_a, **_k):
        raise AssertionError("fetch_readme called")

    monkeypatch.setattr(sa, "discover_external_distributions_with_warnings", fake_discover)
    monkeypatch.setattr(sa, "fetch_readme", fail_fetch)

    res = asyncio.run(
        ensure_pypi_skills_and_block(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert res.warnings == []
    assert "BODY" in res.skills_block
    assert "jaunt:skill=pypi" not in res.skills_block


def test_existing_generated_skill_version_change_regenerates(tmp_path: Path, monkeypatch) -> None:
    dist = "external-lib"
    old_version = "0.1.0"
    new_version = "1.2.3"
    path = skill_md_path(project_root=tmp_path, dist=dist)
    _write(path, f"<!-- jaunt:skill=pypi dist={dist} version={old_version} -->\nOLD\n")

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
        def __init__(self, llm):  # noqa: ANN001
            self.llm = llm

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            calls.append((dist, version))
            return "NEW SKILL"

    monkeypatch.setattr(sg, "OpenAISkillGenerator", DummyGen)

    res = asyncio.run(
        ensure_pypi_skills_and_block(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert calls == [(dist, new_version)]

    on_disk = path.read_text(encoding="utf-8")
    assert f"version={new_version}" in on_disk.splitlines()[0]
    assert "NEW SKILL" in on_disk
    assert "NEW SKILL" in res.skills_block
    assert "jaunt:skill=pypi" not in res.skills_block


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
        def __init__(self, llm):  # noqa: ANN001
            pass

        async def generate_skill_markdown(self, dist, version, readme, readme_type):  # noqa: ANN001
            active_count[0] += 1
            concurrency_high_water[0] = max(concurrency_high_water[0], active_count[0])
            await asyncio.sleep(0.01)  # Simulate async work
            generation_order.append(dist)
            active_count[0] -= 1
            return f"SKILL for {dist}"

    monkeypatch.setattr(sg, "OpenAISkillGenerator", ConcurrencyTrackingGen)

    res = asyncio.run(
        ensure_pypi_skills_and_block(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert res.warnings == []
    # All 3 skills should have been generated
    assert len(generation_order) == 3
    # With parallel generation, concurrency should be > 1
    assert concurrency_high_water[0] > 1, (
        f"Expected concurrent generation but high water mark was {concurrency_high_water[0]}"
    )


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
        ensure_pypi_skills_and_block(
            project_root=tmp_path,
            source_roots=[],
            generated_dir="__generated__",
            llm=LLMConfig(provider="openai", model="gpt-test", api_key_env="OPENAI_API_KEY"),
        )
    )
    assert path.read_text(encoding="utf-8") == "USER SKILL\n"
    assert "USER SKILL" in res.skills_block
