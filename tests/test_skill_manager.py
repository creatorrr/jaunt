from __future__ import annotations

import json
from pathlib import Path

import pytest

from jaunt.skill_manager import (
    SkillMeta,
    add_skill,
    build_skills_block,
    discover_all_skills,
    import_skills,
    read_skill_meta,
    remove_auto_skills,
    remove_skill,
    show_skill,
    validate_skill_name,
    write_skill_meta,
)


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _auto_header(dist: str, version: str) -> str:
    return f"<!-- jaunt:skill=pypi dist={dist} version={version} -->"


# --- discover_all_skills ---


def test_discover_all_skills_finds_auto_and_user(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agents/skills/requests/SKILL.md",
        f"{_auto_header('requests', '2.31.0')}\nRequests body\n",
    )
    _write(
        tmp_path / ".agents/skills/my-tool/SKILL.md",
        "# my-tool\nUser skill content\n",
    )

    skills = discover_all_skills(tmp_path)
    assert len(skills) == 2

    auto = [s for s in skills if s.source == "auto"]
    user = [s for s in skills if s.source == "user"]
    assert len(auto) == 1
    assert auto[0].dist == "requests"
    assert auto[0].version == "2.31.0"
    assert len(user) == 1
    assert user[0].name == "my-tool"
    assert user[0].dist is None


def test_discover_all_skills_empty(tmp_path: Path) -> None:
    assert discover_all_skills(tmp_path) == []


def test_discover_all_skills_sorted(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/Zebra/SKILL.md", "# Zebra\nz\n")
    _write(tmp_path / ".agents/skills/alpha/SKILL.md", "# alpha\na\n")
    _write(tmp_path / ".agents/skills/Beta/SKILL.md", "# Beta\nb\n")

    skills = discover_all_skills(tmp_path)
    names = [s.name for s in skills]
    assert names == ["alpha", "Beta", "Zebra"]


# --- build_skills_block ---


def test_build_skills_block_includes_all(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agents/skills/requests/SKILL.md",
        f"{_auto_header('requests', '2.31.0')}\nRequests body\n",
    )
    _write(
        tmp_path / ".agents/skills/my-tool/SKILL.md",
        "# my-tool\nUser content\n",
    )

    block = build_skills_block(tmp_path)
    assert "Requests body" in block
    assert "User content" in block


def test_build_skills_block_strips_header(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agents/skills/lib/SKILL.md",
        f"{_auto_header('lib', '1.0')}\nBody\n",
    )

    block = build_skills_block(tmp_path)
    assert "jaunt:skill=pypi" not in block
    assert "Body" in block


def test_build_skills_block_deterministic_order(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/zzz/SKILL.md", "# zzz\nZ content\n")
    _write(tmp_path / ".agents/skills/aaa/SKILL.md", "# aaa\nA content\n")
    _write(tmp_path / ".agents/skills/mmm/SKILL.md", "# mmm\nM content\n")

    block1 = build_skills_block(tmp_path)
    block2 = build_skills_block(tmp_path)
    assert block1 == block2
    # Verify ordering: aaa before mmm before zzz
    assert block1.index("A content") < block1.index("M content") < block1.index("Z content")


# --- add_skill ---


def test_add_skill_creates_template(tmp_path: Path) -> None:
    path = add_skill(tmp_path, "my-custom-tool")
    assert path.exists()
    content = path.read_text(encoding="utf-8")
    assert "# my-custom-tool" in content
    assert "## What it is" in content
    assert "## Common patterns" in content


def test_add_skill_raises_if_exists(tmp_path: Path) -> None:
    add_skill(tmp_path, "existing")
    with pytest.raises(FileExistsError):
        add_skill(tmp_path, "existing")


# --- remove_skill ---


def test_remove_skill_deletes_dir(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/to-remove/SKILL.md", "content\n")
    path = remove_skill(tmp_path, "to-remove")
    assert not path.exists()


def test_remove_skill_raises_if_missing(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        remove_skill(tmp_path, "nonexistent")


# --- show_skill ---


def test_show_skill_returns_content(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/my-skill/SKILL.md", "Hello World\n")
    assert show_skill(tmp_path, "my-skill") == "Hello World\n"


# --- validate_skill_name ---


@pytest.mark.parametrize(
    "bad_name",
    ["../foo", "foo/bar", "foo\\bar", "foo\x00bar", "", "  "],
)
def test_validate_skill_name_rejects_traversal(bad_name: str) -> None:
    with pytest.raises(ValueError):
        validate_skill_name(bad_name)


@pytest.mark.parametrize(
    "good_name",
    ["requests", "my-tool", "MyTool", "tool_v2", "a"],
)
def test_validate_skill_name_accepts_valid(good_name: str) -> None:
    assert validate_skill_name(good_name) == good_name


# --- remove_auto_skills ---


def test_remove_auto_skills_only_removes_auto(tmp_path: Path) -> None:
    _write(
        tmp_path / ".agents/skills/auto-lib/SKILL.md",
        f"{_auto_header('auto-lib', '1.0')}\nauto body\n",
    )
    _write(
        tmp_path / ".agents/skills/user-tool/SKILL.md",
        "# user-tool\nuser body\n",
    )

    removed = remove_auto_skills(tmp_path)
    assert removed == ["auto-lib"]
    assert not (tmp_path / ".agents/skills/auto-lib").exists()
    assert (tmp_path / ".agents/skills/user-tool/SKILL.md").exists()


# --- import_skills ---


def test_import_finds_claude_and_codex_dirs(tmp_path: Path, monkeypatch) -> None:
    # Set up ancestor dirs with skills
    _write(tmp_path / ".claude/skills/claude-tool/SKILL.md", "claude skill\n")
    _write(tmp_path / ".codex/skills/codex-tool/SKILL.md", "codex skill\n")

    project = tmp_path / "project"
    project.mkdir()

    # Patch git toplevel to return tmp_path
    import jaunt.skill_manager as sm

    monkeypatch.setattr(sm, "_git_toplevel", lambda _: tmp_path)

    results = import_skills(project)
    assert len(results) == 2
    names = {r[0] for r in results}
    assert "claude-tool" in names
    assert "codex-tool" in names
    # Verify files were copied
    assert (project / ".agents/skills/claude-tool/SKILL.md").exists()


def test_import_skips_existing(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / ".claude/skills/tool/SKILL.md", "original\n")

    project = tmp_path / "project"
    project.mkdir()
    _write(project / ".agents/skills/tool/SKILL.md", "existing\n")

    import jaunt.skill_manager as sm

    monkeypatch.setattr(sm, "_git_toplevel", lambda _: tmp_path)

    results = import_skills(project)
    assert len(results) == 1
    assert results[0][2] == "skipped"
    # Existing content preserved
    assert (project / ".agents/skills/tool/SKILL.md").read_text() == "existing\n"


def test_import_dry_run(tmp_path: Path, monkeypatch) -> None:
    _write(tmp_path / ".claude/skills/tool/SKILL.md", "content\n")

    project = tmp_path / "project"
    project.mkdir()

    import jaunt.skill_manager as sm

    monkeypatch.setattr(sm, "_git_toplevel", lambda _: tmp_path)

    results = import_skills(project, dry_run=True)
    assert len(results) == 1
    assert results[0][2] == "imported"
    # File should NOT be created
    assert not (project / ".agents/skills/tool/SKILL.md").exists()


def test_import_from_explicit_dir(tmp_path: Path) -> None:
    source = tmp_path / "source_skills"
    _write(source / "my-tool/SKILL.md", "tool content\n")

    project = tmp_path / "project"
    project.mkdir()

    results = import_skills(project, from_dir=source)
    assert len(results) == 1
    assert results[0][0] == "my-tool"
    assert results[0][2] == "imported"
    assert (project / ".agents/skills/my-tool/SKILL.md").exists()


def test_import_from_explicit_dir_selected_names(tmp_path: Path) -> None:
    source = tmp_path / "source_skills"
    _write(source / "my-tool/SKILL.md", "tool content\n")
    _write(source / "other-tool/SKILL.md", "other content\n")

    project = tmp_path / "project"
    project.mkdir()

    results = import_skills(project, from_dir=source, names=["other-tool"])
    assert [result[0] for result in results] == ["other-tool"]
    assert not (project / ".agents/skills/my-tool").exists()
    assert (project / ".agents/skills/other-tool/SKILL.md").exists()


def test_import_selected_names_missing_raises(tmp_path: Path) -> None:
    source = tmp_path / "source_skills"
    _write(source / "my-tool/SKILL.md", "tool content\n")

    project = tmp_path / "project"
    project.mkdir()

    with pytest.raises(ValueError, match="Unknown importable skill"):
        import_skills(project, from_dir=source, names=["missing-tool"])


def test_import_copies_sibling_files(tmp_path: Path) -> None:
    """Import should copy the entire skill directory, not just SKILL.md."""
    source = tmp_path / "source_skills"
    _write(source / "my-tool/SKILL.md", "tool content\n")
    _write(source / "my-tool/references/api.md", "api docs\n")
    _write(source / "my-tool/assets/example.py", "print('hello')\n")

    project = tmp_path / "project"
    project.mkdir()

    results = import_skills(project, from_dir=source)
    assert results[0][2] == "imported"
    assert (project / ".agents/skills/my-tool/SKILL.md").exists()
    assert (project / ".agents/skills/my-tool/references/api.md").exists()
    assert (project / ".agents/skills/my-tool/assets/example.py").exists()


def test_build_skills_block_filters_stale_auto_skills(tmp_path: Path) -> None:
    """Auto skills for no-longer-imported dists should be excluded when pypi_dists is given."""
    # "requests" is stale (not in pypi_dists), "httpx" is active
    _write(
        tmp_path / ".agents/skills/requests/SKILL.md",
        f"{_auto_header('requests', '2.31.0')}\nRequests body\n",
    )
    _write(
        tmp_path / ".agents/skills/httpx/SKILL.md",
        f"{_auto_header('httpx', '0.25.0')}\nHTTPX body\n",
    )
    _write(
        tmp_path / ".agents/skills/my-tool/SKILL.md",
        "# my-tool\nUser content\n",
    )

    block = build_skills_block(tmp_path, pypi_dists={"httpx": "0.25.0"})
    assert "HTTPX body" in block
    assert "User content" in block
    assert "Requests body" not in block


def test_build_skills_block_no_filter_without_pypi_dists(tmp_path: Path) -> None:
    """Without pypi_dists, all skills (auto + user) should be included."""
    _write(
        tmp_path / ".agents/skills/requests/SKILL.md",
        f"{_auto_header('requests', '2.31.0')}\nRequests body\n",
    )
    _write(
        tmp_path / ".agents/skills/my-tool/SKILL.md",
        "# my-tool\nUser content\n",
    )

    block = build_skills_block(tmp_path)
    assert "Requests body" in block
    assert "User content" in block


# --- add_skill with description ---


def test_add_skill_with_description(tmp_path: Path) -> None:
    path = add_skill(tmp_path, "my-tool", description="HTTP client wrapper")
    content = path.read_text(encoding="utf-8")
    assert "HTTP client wrapper" in content
    assert "## What it is" in content


# --- add_skill backward compat ---


def test_add_skill_backward_compat(tmp_path: Path) -> None:
    """Existing no-arg behavior unchanged."""
    path = add_skill(tmp_path, "plain")
    content = path.read_text(encoding="utf-8")
    assert "# plain" in content
    assert "<!-- Describe what this tool/library/API does -->" in content


# --- add_skill with libs ---


def test_add_skill_with_libs(tmp_path: Path) -> None:
    from jaunt.lib_inspect import LibRef

    # Create a fake local lib
    lib_dir = tmp_path / "mylib"
    lib_dir.mkdir()
    (lib_dir / "__init__.py").write_text('__version__ = "0.1.0"\n')
    (lib_dir / "core.py").write_text(
        'def greet(name: str) -> str:\n    """Say hello."""\n    return f"Hi {name}"\n'
    )

    ref = LibRef(type="path", name="mylib", path=str(lib_dir), version=None, import_roots=[])
    path = add_skill(tmp_path, "my-tool", description="My tool", libs=[ref])
    content = path.read_text(encoding="utf-8")
    assert "# my-tool" in content
    assert "My tool" in content
    assert "## Libraries" in content
    # META.json should be created
    meta_path = tmp_path / ".agents/skills/my-tool/META.json"
    assert meta_path.exists()
    meta_data = json.loads(meta_path.read_text())
    assert meta_data["description"] == "My tool"
    assert len(meta_data["libs"]) == 1


# --- read/write skill meta ---


def test_read_write_skill_meta(tmp_path: Path) -> None:
    _write(tmp_path / ".agents/skills/s/SKILL.md", "# s\n")
    meta = SkillMeta(
        libs=[{"type": "pypi", "name": "requests", "path": None, "version": "2.31.0"}],
        description="HTTP lib",
    )
    write_skill_meta(tmp_path, "s", meta)

    read_back = read_skill_meta(tmp_path, "s")
    assert read_back is not None
    assert read_back.description == "HTTP lib"
    assert len(read_back.libs) == 1
    assert read_back.libs[0]["name"] == "requests"


def test_read_skill_meta_missing(tmp_path: Path) -> None:
    assert read_skill_meta(tmp_path, "nonexistent") is None


def test_add_skill_stores_relative_path(tmp_path: Path) -> None:
    """Local lib paths should be stored relative to project root in META.json."""
    from jaunt.lib_inspect import LibRef

    lib_dir = tmp_path / "libs" / "mylib"
    lib_dir.mkdir(parents=True)
    (lib_dir / "__init__.py").write_text("")

    ref = LibRef(type="path", name="mylib", path=str(lib_dir), version=None, import_roots=[])
    add_skill(tmp_path, "my-tool", libs=[ref])
    meta_data = json.loads((tmp_path / ".agents/skills/my-tool/META.json").read_text())
    stored_path = meta_data["libs"][0]["path"]
    # Should be relative, not absolute
    assert not Path(stored_path).is_absolute()
    assert stored_path == "libs/mylib"
