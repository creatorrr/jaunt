"""Deterministic checks for the Codex Jaunt plugin bundle."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
PLUGIN = REPO / "plugins" / "jaunt"
GUARD = PLUGIN / "scripts" / "codex-guard.sh"
CLAUDE_PLUGIN = REPO / "jaunt-claude-plugin"


def test_manifest_and_marketplace_shape() -> None:
    manifest = json.loads((PLUGIN / ".codex-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "jaunt"
    assert manifest["version"] == "1.0.0"
    assert manifest["skills"] == "./skills/"
    assert "hooks" not in manifest
    assert "apps" not in manifest
    assert "mcpServers" not in manifest
    interface = manifest["interface"]
    assert interface["category"] == "Developer Tools"
    assert interface["defaultPrompt"]
    assert not any(key in interface for key in ("composerIcon", "logo", "screenshots"))

    marketplace = json.loads((REPO / ".agents" / "plugins" / "marketplace.json").read_text())
    assert marketplace["name"] == "jaunt-codex-plugins"
    (entry,) = marketplace["plugins"]
    assert entry == {
        "name": "jaunt",
        "source": {"source": "local", "path": "./plugins/jaunt"},
        "policy": {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        "category": "Developer Tools",
    }


def test_skills_have_frontmatter_and_interface_metadata() -> None:
    expected = {
        "working-with-jaunt",
        "build",
        "doctor",
        "convert",
        "first-build-reviewer",
    }
    assert {path.parent.name for path in PLUGIN.glob("skills/*/SKILL.md")} == expected
    for skill_path in sorted(PLUGIN.glob("skills/*/SKILL.md")):
        text = skill_path.read_text()
        match = re.match(r"\A---\n(.*?)\n---\n", text, re.DOTALL)
        assert match, f"{skill_path}: missing frontmatter"
        frontmatter = yaml.safe_load(match.group(1))
        assert frontmatter["name"] == skill_path.parent.name
        assert frontmatter["description"]
        assert "${PLUGIN_ROOT" not in text, (
            "PLUGIN_ROOT is hook-only; skill shell commands must resolve from SKILL.md"
        )
        agent = yaml.safe_load((skill_path.parent / "agents" / "openai.yaml").read_text())
        assert agent["interface"]["display_name"]
        assert agent["interface"]["short_description"]
        assert agent["interface"]["default_prompt"]
    for name in ("convert", "first-build-reviewer"):
        agent = yaml.safe_load((PLUGIN / "skills" / name / "agents" / "openai.yaml").read_text())
        assert agent["policy"]["allow_implicit_invocation"] is False


def test_hooks_reference_existing_executable_scripts() -> None:
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    commands = [
        hook["command"]
        for groups in hooks["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands
    for command in commands:
        refs = re.findall(r"\$\{PLUGIN_ROOT\}(/scripts/[^\"' ]+)", command)
        assert refs
        for ref in refs:
            script = PLUGIN / ref.lstrip("/")
            assert script.is_file()
            assert script.stat().st_mode & 0o111


def test_shared_scripts_are_byte_identical() -> None:
    for name in ("doctor.sh", "resolve-workspace.sh", "session-status.sh"):
        assert (PLUGIN / "scripts" / name).read_bytes() == (
            CLAUDE_PLUGIN / "scripts" / name
        ).read_bytes()


def test_plugin_files_are_tracked() -> None:
    if shutil.which("git") is None:  # pragma: no cover
        pytest.skip("git unavailable")
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(REPO),
            "ls-files",
            "--",
            "plugins/jaunt",
            ".agents/plugins/marketplace.json",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    tracked = set(proc.stdout.splitlines())
    on_disk = {
        str(path.relative_to(REPO))
        for root in (PLUGIN, REPO / ".agents" / "plugins")
        for path in root.rglob("*")
        if path.is_file()
    }
    assert tracked == on_disk


def test_all_plugin_scripts_are_valid_bash() -> None:
    if shutil.which("bash") is None:  # pragma: no cover
        pytest.skip("bash unavailable")
    for plugin in (PLUGIN, CLAUDE_PLUGIN):
        for script in sorted((plugin / "scripts").glob("*.sh")):
            subprocess.run(["bash", "-n", str(script)], check=True)


def test_doctor_does_not_accept_failed_probe_output(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text("#!/usr/bin/env bash\necho probe-failed\nexit 1\n")
    codex.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\necho sync-failed\nexit 1\n")
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "JAUNT_WORKSPACE_ROOT": str(tmp_path),
    }
    result = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "- codex: unavailable" in result.stdout
    assert "- codex auth: not authenticated" in result.stdout
    assert "- jaunt: unavailable" in result.stdout
    assert "- codex: probe-failed" not in result.stdout
    assert "- jaunt: sync-failed" not in result.stdout


def test_doctor_skips_codex_warning_preambles(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    codex = bin_dir / "codex"
    codex.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'WARNING: helper aliases unavailable'\n"
        'if [ "$1" = "--version" ]; then\n'
        "  echo 'codex-cli 9.9.9'\n"
        "else\n"
        "  echo 'Logged in using an API key'\n"
        "fi\n"
    )
    codex.chmod(0o755)
    uv = bin_dir / "uv"
    uv.write_text("#!/usr/bin/env bash\necho 'jaunt 1.6.2'\n")
    uv.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "JAUNT_WORKSPACE_ROOT": str(tmp_path),
    }
    result = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "- codex: codex-cli 9.9.9" in result.stdout
    assert "- codex auth: Logged in using an API key" in result.stdout
    assert "- jaunt: jaunt 1.6.2" in result.stdout
    assert "WARNING:" not in result.stdout


def _fake_guard_bin(tmp_path: Path, *, fail: bool = False) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    jaunt = bin_dir / "jaunt"
    if fail:
        jaunt.write_text("#!/usr/bin/env bash\nexit 2\n")
    else:
        jaunt.write_text(
            """#!/usr/bin/env bash
python3 -c '
import json, os, sys
payload = json.load(sys.stdin)
path = payload["tool_input"]["file_path"]
generated = os.environ.get("FAKE_GENERATED_DIR", "__generated__")
if f"/{generated}/" in f"/{path}":
    spec = path.replace(f"/{generated}/", "/")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": f"{path} is generated; edit {spec}",
    }}))
'
"""
        )
    jaunt.chmod(0o755)
    return bin_dir


def _payload(cwd: Path, patch: str) -> str:
    return json.dumps(
        {"cwd": str(cwd), "tool_name": "apply_patch", "tool_input": {"command": patch}}
    )


def _run_guard(payload: str, *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(GUARD)],
        input=payload,
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )


def _env(bin_dir: Path, *, generated_dir: str = "__generated__") -> dict[str, str]:
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "FAKE_GENERATED_DIR": generated_dir,
    }


@pytest.mark.parametrize(
    "patch",
    [
        "*** Begin Patch\n*** Update File: src/pkg/__generated__/mod.py\n*** End Patch",
        "*** Begin Patch\n   *** Update File: src/pkg/__generated__/mod.py   \n*** End Patch",
        "*** Begin Patch\n"
        "*** Update File: src/pkg/old.py\n"
        "*** Move to: src/pkg/__generated__/mod.py\n"
        "*** End Patch",
        "*** Begin Patch\n"
        "*** Update File: src/pkg/plain.py\n"
        "*** Add File: src/pkg/__generated__/mod.py\n"
        "*** End Patch",
    ],
)
def test_codex_guard_denies_generated_patch_shapes(tmp_path: Path, patch: str) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    bin_dir = _fake_guard_bin(tmp_path)
    result = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir))
    assert result.returncode == 0
    output = json.loads(result.stdout)
    specific = output["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert "edit" in specific["permissionDecisionReason"]


def test_codex_guard_uses_custom_generated_dir(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    bin_dir = _fake_guard_bin(tmp_path)
    patch = "*** Begin Patch\n*** Update File: src/pkg/gen_out/mod.py\n*** End Patch"
    result = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir, generated_dir="gen_out"))
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_guard_denies_existing_generated_pyi(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    pyi = tmp_path / "src" / "pkg" / "mod.pyi"
    pyi.parent.mkdir(parents=True)
    pyi.write_text("# This .pyi stub was generated by jaunt. DO NOT EDIT.\n# jaunt:kind=stub\n")
    bin_dir = _fake_guard_bin(tmp_path, fail=True)
    patch = "*** Begin Patch\n*** Delete File: src/pkg/mod.pyi\n*** End Patch"
    result = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir))
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
    assert str(pyi.with_suffix(".py")) in specific["permissionDecisionReason"]


@pytest.mark.parametrize(
    "payload",
    [
        "",
        "not json",
        json.dumps({"cwd": "/", "tool_name": "apply_patch", "tool_input": {}}),
        json.dumps({"cwd": "/", "tool_name": "Bash", "tool_input": {"command": "x"}}),
    ],
)
def test_codex_guard_malformed_inputs_fail_open(tmp_path: Path, payload: str) -> None:
    bin_dir = _fake_guard_bin(tmp_path, fail=True)
    result = _run_guard(payload, env=_env(bin_dir))
    assert result.returncode == 0
    assert result.stdout == ""


def test_codex_guard_no_config_and_tool_failure_fail_open(tmp_path: Path) -> None:
    bin_dir = _fake_guard_bin(tmp_path, fail=True)
    patch = "*** Begin Patch\n*** Update File: __generated__/mod.py\n*** End Patch"
    no_config = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir))
    assert no_config.stdout == ""

    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    tool_failure = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir))
    assert tool_failure.returncode == 0
    assert tool_failure.stdout == ""


def test_codex_guard_allows_normal_file(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    bin_dir = _fake_guard_bin(tmp_path)
    patch = "*** Begin Patch\n*** Update File: src/pkg/mod.py\n*** End Patch"
    result = _run_guard(_payload(tmp_path, patch), env=_env(bin_dir))
    assert result.returncode == 0
    assert result.stdout == ""
