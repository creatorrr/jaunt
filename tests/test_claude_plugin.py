"""Deterministic checks for the Claude Code plugin artifacts (jaunt-claude-plugin/)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "jaunt-claude-plugin"


def _frontmatter(text: str) -> dict[str, str]:
    m = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML frontmatter"
    fields: dict[str, str] = {}
    key = None
    for line in m.group(1).splitlines():
        if line[:1] in (" ", "\t") and key:
            fields[key] += " " + line.strip()
        elif ":" in line:
            key, _, value = line.partition(":")
            key = key.strip()
            fields[key] = value.strip()
    return fields


def test_plugin_manifest_shape():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "jaunt"
    assert re.fullmatch(r"\d+\.\d+\.\d+", manifest["version"])


def test_marketplace_at_repo_root_points_at_plugin():
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    (entry,) = market["plugins"]
    assert entry["name"] == manifest["name"]
    assert entry["version"] == manifest["version"] == market["metadata"]["version"]
    assert (REPO / entry["source"]).resolve() == PLUGIN.resolve()
    assert not (PLUGIN / ".claude-plugin" / "marketplace.json").exists()


def test_only_manifest_inside_dot_claude_plugin():
    assert [p.name for p in (PLUGIN / ".claude-plugin").iterdir()] == ["plugin.json"]


def test_hooks_reference_existing_executable_scripts():
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    commands = [
        h["command"]
        for groups in hooks["hooks"].values()
        for group in groups
        for h in group["hooks"]
    ]
    assert commands, "hooks.json defines no commands"
    for command in commands:
        for ref in re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}([^\"' ]+)", command):
            script = PLUGIN / ref.lstrip("/")
            assert script.is_file(), f"missing {ref}"
            assert script.stat().st_mode & 0o111, f"not executable: {ref}"


def test_scripts_are_valid_bash():
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("bash unavailable")
    for script in sorted((PLUGIN / "scripts").glob("*.sh")):
        subprocess.run(["bash", "-n", str(script)], check=True)


def test_skills_and_agents_have_frontmatter():
    docs = sorted(PLUGIN.glob("skills/*/SKILL.md")) + sorted(PLUGIN.glob("agents/*.md"))
    names = set()
    for doc in docs:
        fields = _frontmatter(doc.read_text())
        assert fields.get("name"), f"{doc}: missing name"
        assert fields["name"] not in names, f"duplicate skill/agent name {fields['name']}"
        names.add(fields["name"])
        assert 0 < len(fields.get("description", "")) <= 1024, f"{doc}: bad description"
    assert {"build", "working-with-jaunt", "doctor", "convert", "first-build-reviewer"} <= names


def test_convert_skill_is_user_invoked_only():
    fields = _frontmatter((PLUGIN / "skills" / "convert" / "SKILL.md").read_text())
    assert fields.get("disable-model-invocation") == "true"
