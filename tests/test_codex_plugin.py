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
    assert manifest["version"] == "1.1.1"
    assert "TypeScript" in manifest["description"]
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
    assert "version" not in marketplace
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
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'sample'\nversion = '0'\n")
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


def test_doctor_skips_nested_tool_worktrees_and_scopes_hooks_to_its_host(
    tmp_path: Path,
) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 1\n")
    for host in (".claude", ".codex"):
        nested = tmp_path / host / "worktrees" / "unrelated"
        nested.mkdir(parents=True)
        (nested / "jaunt.toml").write_text("version = 1\n")
    (tmp_path / ".claude" / "settings.json").write_text(
        '{"hooks":{"PreToolUse":[{"command":"jaunt guard"}]}}\n'
    )
    (tmp_path / ".codex" / "config.toml").write_text('command = "scripts/codex-guard.sh"\n')
    _write_executable(
        bin_dir / "codex",
        'if [ "$1" = "--version" ]; then echo "codex-cli 9"; else echo "Logged in"; fi\n',
    )
    _write_executable(
        bin_dir / "jaunt",
        """if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; exit 0; fi
echo '{"command":"status","ok":true,"fresh":[],"stale":[],"orphans":[]}'
""",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "JAUNT_WORKSPACE_ROOT": str(tmp_path),
    }

    codex = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "- .: 0 fresh" in codex.stdout
    assert ".claude/worktrees" not in codex.stdout
    assert ".codex/worktrees" not in codex.stdout
    assert "== duplicate Codex hooks" in codex.stdout
    assert str(tmp_path / ".codex" / "config.toml") in codex.stdout
    assert str(tmp_path / ".claude" / "settings.json") not in codex.stdout

    claude = subprocess.run(
        ["bash", str(CLAUDE_PLUGIN / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "- .: 0 fresh" in claude.stdout
    assert ".claude/worktrees" not in claude.stdout
    assert ".codex/worktrees" not in claude.stdout
    assert "== duplicate Claude hooks" in claude.stdout
    assert str(tmp_path / ".claude" / "settings.json") in claude.stdout
    assert str(tmp_path / ".codex" / "config.toml") not in claude.stdout


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\n{body}")
    path.chmod(0o755)


def test_workspace_runner_prefers_installed_then_uv_project_then_uvx(tmp_path: Path) -> None:
    resolver = PLUGIN / "scripts" / "resolve-workspace.sh"

    installed_root = tmp_path / "installed"
    installed_bin = installed_root / "bin"
    installed_bin.mkdir(parents=True)
    (installed_root / "jaunt.toml").write_text("version = 1\n")
    installed_log = installed_root / "runner.log"
    _write_executable(
        installed_bin / "jaunt",
        'if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; '
        f'else printf "jaunt:%s\\n" "$*" > "{installed_log}"; fi\n',
    )
    _write_executable(installed_bin / "uv", f'printf "uv:%s\\n" "$*" > "{installed_log}"\n')
    _write_executable(installed_bin / "uvx", f'printf "uvx:%s\\n" "$*" > "{installed_log}"\n')
    env = {**os.environ, "PATH": f"{installed_bin}{os.pathsep}/usr/bin:/bin"}
    subprocess.run(
        ["bash", str(resolver), "--run", str(installed_root), "status", "--json"],
        env=env,
        check=True,
    )
    assert installed_log.read_text() == "jaunt:status --json\n"

    uv_root = tmp_path / "uv-project"
    uv_bin = uv_root / "bin"
    uv_bin.mkdir(parents=True)
    (uv_root / "jaunt.toml").write_text("version = 1\n")
    (uv_root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    uv_log = uv_root / "runner.log"
    _write_executable(uv_bin / "jaunt", 'echo "jaunt 0.4.3"\n')
    _write_executable(
        uv_bin / "uv",
        'if [ "$*" = "run --no-sync jaunt --version" ]; then echo "jaunt 1.7.1"; '
        f'else printf "uv:%s\\n" "$*" > "{uv_log}"; fi\n',
    )
    _write_executable(uv_bin / "uvx", f'printf "uvx:%s\\n" "$*" > "{uv_log}"\n')
    env = {**os.environ, "PATH": f"{uv_bin}{os.pathsep}/usr/bin:/bin"}
    subprocess.run(["bash", str(resolver), "--run", str(uv_root), "check"], env=env, check=True)
    assert uv_log.read_text() == "uv:run --no-sync jaunt check\n"

    js_root = tmp_path / "js-only"
    js_bin = js_root / "bin"
    js_bin.mkdir(parents=True)
    (js_root / "jaunt.toml").write_text("version = 2\n[target.ts]\n")
    (js_root / "package.json").write_text('{"name":"sample"}\n')
    js_log = js_root / "runner.log"
    _write_executable(js_bin / "jaunt", 'echo "jaunt 1.6.3"\n')
    _write_executable(js_bin / "uv", f'printf "uv:%s\\n" "$*" > "{js_log}"\n')
    _write_executable(js_bin / "uvx", f'printf "uvx:%s\\n" "$*" > "{js_log}"\n')
    env = {**os.environ, "PATH": f"{js_bin}{os.pathsep}/usr/bin:/bin"}
    subprocess.run(
        ["bash", str(resolver), "--run", str(js_root), "status", "--language", "ts"],
        env=env,
        check=True,
    )
    assert js_log.read_text() == "uvx:jaunt status --language ts\n"

    inline_root = tmp_path / "v2-inline"
    inline_bin = inline_root / "bin"
    inline_bin.mkdir(parents=True)
    (inline_root / "jaunt.toml").write_text(
        'version = 2\ntarget = { py = { source_roots = ["src"] } }\n'
    )
    (inline_root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    inline_log = inline_root / "runner.log"
    _write_executable(inline_bin / "jaunt", 'echo "jaunt 1.6.3"\n')
    _write_executable(inline_bin / "uv", 'echo "jaunt 1.6.3"\n')
    _write_executable(inline_bin / "uvx", f'printf "uvx:%s\\n" "$*" > "{inline_log}"\n')
    env = {**os.environ, "PATH": f"{inline_bin}{os.pathsep}/usr/bin:/bin"}
    subprocess.run(
        ["bash", str(resolver), "--run", str(inline_root), "status"], env=env, check=True
    )
    assert inline_log.read_text() == "uvx:jaunt status\n"


def test_workspace_runner_exports_plugin_cache_for_uv_and_uvx(tmp_path: Path) -> None:
    resolver = PLUGIN / "scripts" / "resolve-workspace.sh"
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    unwritable_home = tmp_path / "unwritable-home"
    unwritable_home.mkdir()
    unwritable_home.chmod(0o500)

    uv_root = tmp_path / "uv-project"
    uv_bin = uv_root / "bin"
    uv_bin.mkdir(parents=True)
    (uv_root / "jaunt.toml").write_text("version = 2\n[target.py]\n")
    (uv_root / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    uv_log = uv_root / "runner.log"
    _write_executable(
        uv_bin / "uv",
        """if [ "$UV_CACHE_DIR" != "$EXPECTED_CACHE" ]; then exit 82; fi
if [ "$*" = "run --no-sync jaunt --version" ]; then echo "jaunt 1.7.1"; exit 0; fi
printf "uv:%s\n" "$*" > "$RUNNER_LOG"
""",
    )
    env = {
        **os.environ,
        "PATH": f"{uv_bin}{os.pathsep}/usr/bin:/bin",
        "HOME": str(unwritable_home),
        "PLUGIN_DATA": str(plugin_data),
        "EXPECTED_CACHE": str(plugin_data),
        "RUNNER_LOG": str(uv_log),
    }
    env.pop("UV_CACHE_DIR", None)
    subprocess.run(["bash", str(resolver), "--run", str(uv_root), "status"], env=env, check=True)
    assert uv_log.read_text() == "uv:run --no-sync jaunt status\n"

    js_root = tmp_path / "js-only"
    js_bin = js_root / "bin"
    js_bin.mkdir(parents=True)
    (js_root / "jaunt.toml").write_text("version = 2\n[target.ts]\n")
    js_log = js_root / "runner.log"
    _write_executable(
        js_bin / "uvx",
        """if [ "$UV_CACHE_DIR" != "$EXPECTED_CACHE" ]; then exit 82; fi
printf "uvx:%s\n" "$*" > "$RUNNER_LOG"
""",
    )
    env.update(
        {
            "PATH": f"{js_bin}{os.pathsep}/usr/bin:/bin",
            "RUNNER_LOG": str(js_log),
        }
    )
    subprocess.run(
        ["bash", str(resolver), "--run", str(js_root), "status", "--language", "ts"],
        env=env,
        check=True,
    )
    assert js_log.read_text() == "uvx:jaunt status --language ts\n"


def test_session_status_reports_typescript_unbuilt_invalid_and_diagnostics(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "version = 2\n[target.ts]\nsource_roots=['src']\nprojects=['tsconfig.json']\n"
    )
    _write_executable(
        bin_dir / "jaunt",
        """if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; exit 0; fi
cat <<'JSON'
{
  "command": "status",
  "ok": true,
  "fresh": [],
  "stale": ["ts:src/slug/index"],
  "stale_changes": {"ts:src/slug/index": "structural"},
  "unbuilt": ["ts:src/slug/index"],
  "invalid": {
    "ts:src/bad/index": [{"code": "JAUNT_TS_INVALID", "message": "invalid artifact"}]
  },
  "orphans": [],
  "diagnostics": [
    {"code": "JAUNT_TS_WARNING", "message": "compiler warning", "severity": "warning"}
  ],
  "targets": {
    "ts": {
      "fresh": [],
      "stale": {"src/slug/index": "structural"},
      "unbuilt": ["src/slug/index"],
      "invalid": {
        "src/bad/index": [{"code": "JAUNT_TS_INVALID", "message": "invalid artifact"}]
      },
      "orphans": []
    }
  }
}
JSON
""",
    )
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "session-status.sh")],
        input=json.dumps({"cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "TS: 1 unbuilt, 1 invalid, 2 diagnostics" in result.stdout
    assert "JAUNT_TS_WARNING" in result.stdout
    assert "JAUNT_TS_INVALID" in result.stdout


def test_doctor_checks_node_npm_and_typescript_tooling_without_building(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (tmp_path / "jaunt.toml").write_text(
        "version = 2\n[target.ts]\nsource_roots=['src']\nprojects=['tsconfig.json']\n"
    )
    _write_executable(
        bin_dir / "codex",
        'if [ "$1" = "--version" ]; then echo "codex-cli 9"; else echo "Logged in"; fi\n',
    )
    _write_executable(bin_dir / "node", 'echo "v22.14.0"\n')
    _write_executable(bin_dir / "npm", 'echo "11.5.1"\n')
    _write_executable(
        bin_dir / "jaunt",
        """if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; exit 0; fi
cat <<'JSON'
{
  "command": "status",
  "ok": true,
  "fresh": [],
  "stale": [],
  "unbuilt": ["ts:src/slug/index"],
  "invalid": {},
  "orphans": [],
  "diagnostics": [
    {"code": "JAUNT_TS_NOTE", "message": "review this warning", "severity": "warning"}
  ],
  "targets": {
    "ts": {
      "fresh": [],
      "stale": {},
      "unbuilt": ["src/slug/index"],
      "invalid": {},
      "orphans": []
    }
  }
}
JSON
""",
    )
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
    assert "- node: v22.14.0" in result.stdout
    assert "- npm: 11.5.1" in result.stdout
    assert "TypeScript: worker/compiler ready; 1 unbuilt, 0 invalid, 1 diagnostics" in result.stdout
    assert "JAUNT_TS_NOTE: review this warning" in result.stdout


def test_status_hooks_do_not_report_error_payload_as_healthy(tmp_path: Path) -> None:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (tmp_path / "jaunt.toml").write_text("version = 2\n[target.ts]\n")
    _write_executable(
        bin_dir / "jaunt",
        """if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; exit 0; fi
cat <<'JSON'
{
  "command": "status",
  "ok": false,
  "error": {
    "message": "compiler unavailable",
    "diagnostics": [{"code": "JAUNT_TS_COMPILER", "message": "install TypeScript"}]
  }
}
JSON
""",
    )
    _write_executable(bin_dir / "codex", 'echo "Logged in"\n')
    _write_executable(bin_dir / "node", 'echo "v22.14.0"\n')
    _write_executable(bin_dir / "npm", 'echo "11.5.1"\n')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "JAUNT_WORKSPACE_ROOT": str(tmp_path),
    }
    session = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "session-status.sh")],
        input=json.dumps({"cwd": str(tmp_path)}),
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "TS status unavailable: compiler unavailable [JAUNT_TS_COMPILER]" in session.stdout
    assert "0 fresh" not in session.stdout
    doctor = subprocess.run(
        ["bash", str(PLUGIN / "scripts" / "doctor.sh")],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "worker/compiler unavailable: compiler unavailable" in doctor.stdout
    assert "JAUNT_TS_COMPILER: install TypeScript" in doctor.stdout
    assert "worker/compiler ready" not in doctor.stdout


def _fake_guard_bin(tmp_path: Path, *, fail: bool = False) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    jaunt = bin_dir / "jaunt"
    if fail:
        jaunt.write_text(
            '#!/usr/bin/env bash\nif [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; '
            "else exit 2; fi\n"
        )
    else:
        jaunt.write_text(
            """#!/usr/bin/env bash
if [ "$1" = "--version" ]; then echo "jaunt 1.7.1"; exit 0; fi
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


def test_codex_guard_uses_uvx_in_javascript_only_workspace(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 2\n")
    (tmp_path / "package.json").write_text('{"name":"sample"}\n')
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text(
        """#!/usr/bin/env bash
python3 -c '
import json, sys
payload = json.load(sys.stdin)
path = payload["tool_input"]["file_path"]
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": f"{path} is generated; edit the spec",
}}))
'
"""
    )
    uvx.chmod(0o755)
    patch = "*** Begin Patch\n*** Update File: __generated__/mod.ts\n*** End Patch"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
    }
    result = _run_guard(_payload(tmp_path, patch), env=env)
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"


def test_codex_guard_skips_stale_zero_exit_jaunt_for_v2(tmp_path: Path) -> None:
    (tmp_path / "jaunt.toml").write_text("version = 2\n[target.ts]\ngenerated_dir = 'gen'\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname='sample'\nversion='0'\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "jaunt", 'if [ "$1" = "--version" ]; then echo "jaunt 1.6.3"; fi\n')
    _write_executable(bin_dir / "uv", 'echo "jaunt 1.6.3"\n')
    _write_executable(
        bin_dir / "uvx",
        """python3 -c '
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": p["tool_input"]["file_path"] + " is generated",
}}))
'
""",
    )
    patch = "*** Begin Patch\n*** Update File: gen/mod.ts\n*** End Patch"
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"}
    result = _run_guard(_payload(tmp_path, patch), env=env)
    assert json.loads(result.stdout)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_codex_guard_uv_probe_uses_owner_and_plugin_cache(tmp_path: Path) -> None:
    root = tmp_path / "session-root"
    owner = root / "packages" / "app"
    (owner / "gen").mkdir(parents=True)
    (owner / "jaunt.toml").write_text("version = 2\n[target.ts]\ngenerated_dir = 'gen'\n")
    (owner / "pyproject.toml").write_text("[project]\nname='app'\nversion='0'\n")
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    plugin_data = tmp_path / "plugin-data"
    plugin_data.mkdir()
    unwritable_home = tmp_path / "unwritable-home"
    unwritable_home.mkdir()
    unwritable_home.chmod(0o500)
    _write_executable(bin_dir / "jaunt", 'echo "jaunt 1.6.3"\n')
    _write_executable(
        bin_dir / "uv",
        """if [ "$PWD" != "$EXPECTED_OWNER" ]; then exit 81; fi
if [ "$UV_CACHE_DIR" != "$EXPECTED_CACHE" ]; then exit 82; fi
if [ "$*" = "run --no-sync jaunt --version" ]; then echo "jaunt 1.7.1"; exit 0; fi
python3 -c '
import json, sys
p = json.load(sys.stdin)
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "ask",
    "permissionDecisionReason": p["tool_input"]["file_path"] + " is generated",
}}))
'
""",
    )
    patch = "*** Begin Patch\n*** Update File: packages/app/gen/mod.ts\n*** End Patch"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": str(unwritable_home),
        "PLUGIN_DATA": str(plugin_data),
        "EXPECTED_OWNER": str(owner),
        "EXPECTED_CACHE": str(plugin_data),
    }
    env.pop("UV_CACHE_DIR", None)
    result = _run_guard(_payload(root, patch), env=env)
    specific = json.loads(result.stdout)["hookSpecificOutput"]
    assert specific["permissionDecision"] == "deny"
