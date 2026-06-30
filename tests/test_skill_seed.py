from __future__ import annotations

from pathlib import Path

from jaunt.skill_seed import seed_skills_into_workspace, skills_fingerprint


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_seeds_builtins_into_workspace(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = seed_skills_into_workspace(ws, project_root=None, builtin_names=["ruff", "pytest"])
    assert warnings == []
    assert (ws / ".agents" / "skills" / "ruff" / "SKILL.md").is_file()
    assert (ws / ".agents" / "skills" / "pytest" / "SKILL.md").is_file()


def test_project_skill_overrides_builtin(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    proj = tmp_path / "proj"
    _write(proj / ".agents" / "skills" / "ruff" / "SKILL.md", "PROJECT RUFF\n")
    seed_skills_into_workspace(ws, project_root=proj, builtin_names=["ruff"])
    seeded = (ws / ".agents" / "skills" / "ruff" / "SKILL.md").read_text(encoding="utf-8")
    assert seeded == "PROJECT RUFF\n"


def test_unknown_builtin_is_skipped(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    warnings = seed_skills_into_workspace(ws, project_root=None, builtin_names=["nope"])
    assert not (ws / ".agents" / "skills" / "nope").exists()
    assert warnings == []  # unknown builtin names are silently skipped (registry-resolved)


def test_fingerprint_changes_with_set(tmp_path):
    a = skills_fingerprint(project_root=None, builtin_names=["ruff"])
    b = skills_fingerprint(project_root=None, builtin_names=["ruff", "pytest"])
    assert a != b
    assert a == skills_fingerprint(project_root=None, builtin_names=["ruff"])
