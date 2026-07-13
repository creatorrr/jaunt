"""Deterministic checks for the Claude Code plugin artifacts (jaunt-claude-plugin/)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "jaunt-claude-plugin"
GUARD = PLUGIN / "scripts" / "guard.sh"


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
    assert manifest["version"] == "1.2.0"
    assert "TypeScript" in manifest["description"]


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
        assert "${CLAUDE_PLUGIN_ROOT}/scripts/" in command, (
            f"hook command must reference ${{CLAUDE_PLUGIN_ROOT}}/scripts/: {command}"
        )
        refs = re.findall(r"\$\{CLAUDE_PLUGIN_ROOT\}(/scripts/[^\"' ]+)", command)
        assert refs, f"no script reference found in: {command}"
        for ref in refs:
            script = PLUGIN / ref.lstrip("/")
            assert script.is_file(), f"missing {ref}"
            assert script.stat().st_mode & 0o111, f"not executable: {ref}"


def test_all_plugin_files_are_tracked_by_git():
    # .gitignore's `build/` pattern once swallowed skills/build/SKILL.md; a
    # re-include keeps the dir tracked. Fails if any plugin file goes untracked.
    if shutil.which("git") is None or not (REPO / ".git").exists():  # pragma: no cover
        pytest.skip("not a git checkout")
    proc = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", "jaunt-claude-plugin"],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = sorted(proc.stdout.splitlines())
    on_disk = sorted(str(p.relative_to(REPO)) for p in PLUGIN.rglob("*") if p.is_file())
    assert tracked == on_disk


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


_needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash unavailable")


def _fake_jaunt_bin(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    jaunt = bin_dir / "jaunt"
    # Consumes the replayed stdin payload and prints a marker so the test can
    # assert the guard both found jaunt AND piped the payload through.
    jaunt.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$1" = "--version" ]; then echo "jaunt 1.7.0"; exit 0; fi\n'
        "cat >/dev/null\necho GUARD_RAN\n"
    )
    jaunt.chmod(0o755)
    return bin_dir


def _run_guard(payload, *, env=None):
    return subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
    )


@_needs_bash
def test_guard_fails_open_on_empty_stdin():
    result = _run_guard("")
    assert result.returncode == 0
    assert result.stdout == ""


@_needs_bash
def test_guard_fails_open_on_garbage_stdin():
    result = _run_guard("this is not json {[")
    assert result.returncode == 0


@_needs_bash
def test_guard_runs_jaunt_for_owned_generated_path(tmp_path):
    # Root dir name contains a space to exercise the word-splitting fix.
    root = tmp_path / "repo with space"
    (root / "__generated__").mkdir(parents=True)
    (root / "jaunt.toml").write_text("version = 1\n")
    bin_dir = _fake_jaunt_bin(tmp_path)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps({"tool_input": {"file_path": str(root / "__generated__" / "billing.py")}})
    result = _run_guard(payload, env=env)
    assert result.returncode == 0
    assert "GUARD_RAN" in result.stdout


@_needs_bash
def test_guard_rewrites_payload_cwd_to_owning_project(tmp_path):
    # `jaunt guard` resolves generated_dir from the payload's `cwd` (Claude
    # Code sets it to the session cwd); the wrapper must rewrite it to the
    # owning project or a nested custom generated_dir is checked against the
    # wrong config (codex review P2 on PR #71).
    root = tmp_path / "repo"
    nested = root / "packages" / "billing"
    (nested / "__generated__").mkdir(parents=True)
    (nested / "jaunt.toml").write_text("version = 1\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    echo_jaunt = bin_dir / "jaunt"
    echo_jaunt.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = "--version" ]; then echo "jaunt 1.7.0"; exit 0; fi\ncat\n'
    )  # echo payload back
    echo_jaunt.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps(
        {
            "cwd": str(root),  # session cwd = repo root, NOT the owning project
            "tool_input": {"file_path": str(nested / "__generated__" / "mod.py")},
        }
    )
    result = _run_guard(payload, env=env)
    assert result.returncode == 0
    assert json.loads(result.stdout)["cwd"] == str(nested)


@_needs_bash
def test_guard_falls_back_to_uv_when_path_jaunt_is_stale(tmp_path):
    # A version-manager shim for a pre-1.3 jaunt has no `guard` subcommand and
    # exits nonzero; the wrapper must fall back to `uv run --no-sync jaunt`.
    root = tmp_path / "repo"
    (root / "__generated__").mkdir(parents=True)
    (root / "jaunt.toml").write_text("version = 1\n")
    (root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    stale = bin_dir / "jaunt"
    stale.write_text("#!/usr/bin/env bash\necho 'invalid choice: guard' >&2\nexit 2\n")
    stale.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'if [ "$*" = "run --no-sync jaunt --version" ]; then '
        'echo "jaunt 1.7.0"; exit 0; fi\n'
        "cat >/dev/null\necho UV_FALLBACK_RAN\n"
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps({"tool_input": {"file_path": str(root / "__generated__" / "mod.py")}})
    result = _run_guard(payload, env=env)
    assert result.returncode == 0
    assert "UV_FALLBACK_RAN" in result.stdout


@_needs_bash
def test_guard_uses_uvx_for_javascript_only_workspace(tmp_path):
    root = tmp_path / "repo"
    (root / "__generated__").mkdir(parents=True)
    (root / "jaunt.toml").write_text("version = 2\n")
    (root / "package.json").write_text('{"name":"sample"}\n')
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text("#!/usr/bin/env bash\ncat >/dev/null\necho UVX_FALLBACK_RAN\n")
    uvx.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps({"tool_input": {"file_path": str(root / "__generated__" / "mod.ts")}})
    result = _run_guard(payload, env=env)
    assert result.returncode == 0
    assert "UVX_FALLBACK_RAN" in result.stdout


@_needs_bash
def test_guard_skips_stale_zero_exit_jaunt_for_v2(tmp_path):
    root = tmp_path / "repo"
    (root / "gen").mkdir(parents=True)
    (root / "jaunt.toml").write_text("version = 2\n[target.ts]\ngenerated_dir='gen'\n")
    (root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    stale = bin_dir / "jaunt"
    stale.write_text(
        '#!/usr/bin/env bash\nif [ "$1" = "--version" ]; then echo "jaunt 1.6.3"; fi\n'
    )
    stale.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\necho 'jaunt 1.6.3'\n")
    uv.chmod(0o755)
    uvx = bin_dir / "uvx"
    uvx.write_text("#!/usr/bin/env bash\ncat >/dev/null\necho UVX_FALLBACK_RAN\n")
    uvx.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps({"tool_input": {"file_path": str(root / "gen" / "mod.ts")}})
    result = _run_guard(payload, env=env)
    assert "UVX_FALLBACK_RAN" in result.stdout


@_needs_bash
def test_guard_uv_probe_uses_owner_and_plugin_cache(tmp_path):
    root = tmp_path / "session-root"
    owner = root / "packages" / "app"
    (owner / "gen").mkdir(parents=True)
    (owner / "jaunt.toml").write_text("version = 2\n[target.ts]\ngenerated_dir='gen'\n")
    (owner / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    unwritable_home = tmp_path / "unwritable-home"
    unwritable_home.mkdir()
    unwritable_home.chmod(0o500)
    stale = bin_dir / "jaunt"
    stale.write_text("#!/usr/bin/env bash\necho 'jaunt 1.6.3'\n")
    stale.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text(
        """#!/usr/bin/env bash
if [ "$PWD" != "$EXPECTED_OWNER" ]; then exit 81; fi
if [ "$UV_CACHE_DIR" != "$EXPECTED_CACHE" ]; then exit 82; fi
if [ "$*" = "run --no-sync jaunt --version" ]; then echo "jaunt 1.7.0"; exit 0; fi
python3 -c '
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": p["tool_input"]["file_path"] + " is generated",
}}))
'
"""
    )
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": str(unwritable_home),
        "PLUGIN_DATA": str(plugin_data),
        "EXPECTED_OWNER": str(owner),
        "EXPECTED_CACHE": str(plugin_data),
        "CLAUDE_PROJECT_DIR": str(root),
    }
    env.pop("UV_CACHE_DIR", None)
    payload = json.dumps({"tool_input": {"file_path": str(owner / "gen" / "mod.ts")}})
    result = _run_guard(payload, env=env)
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "ask"


@_needs_bash
def test_guard_fails_open_when_no_owning_jaunt_toml(tmp_path):
    root = tmp_path / "no config here"
    (root / "src").mkdir(parents=True)
    bin_dir = _fake_jaunt_bin(tmp_path)  # jaunt present but must never be invoked
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "CLAUDE_PROJECT_DIR": str(root),
    }
    payload = json.dumps({"tool_input": {"file_path": str(root / "src" / "foo.py")}})
    result = _run_guard(payload, env=env)
    assert result.returncode == 0
    assert "GUARD_RAN" not in result.stdout


@_needs_bash
def test_guard_asks_before_editing_generated_pyi(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "jaunt.toml").write_text("version = 1\n")
    stub = root / "src" / "pkg" / "mod.pyi"
    stub.parent.mkdir(parents=True)
    stub.write_text("# This .pyi stub was generated by jaunt. DO NOT EDIT.\n")
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    payload = json.dumps({"tool_input": {"file_path": str(stub)}})
    result = _run_guard(payload, env=env)
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "ask"
    assert str(stub.with_suffix(".py")) in specific["permissionDecisionReason"]
