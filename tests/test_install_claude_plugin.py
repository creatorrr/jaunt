from __future__ import annotations

import json
import subprocess

import jaunt.cli
from jaunt import claude_plugin


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _record_run(results: list[object], calls: list[dict]):
    """Return a subprocess.run stand-in that records argv/kwargs and pops results."""

    def _run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    return _run


# --- pure logic (claude_plugin.py) -----------------------------------------


def test_marketplace_add_command_default() -> None:
    assert claude_plugin.marketplace_add_command(local_path=None) == [
        "claude",
        "plugin",
        "marketplace",
        "add",
        "creatorrr/jaunt",
    ]


def test_marketplace_add_command_local() -> None:
    assert claude_plugin.marketplace_add_command(local_path="/repo") == [
        "claude",
        "plugin",
        "marketplace",
        "add",
        "/repo",
    ]


def test_plugin_install_command() -> None:
    assert claude_plugin.plugin_install_command() == [
        "claude",
        "plugin",
        "install",
        "jaunt@jaunt-plugins",
    ]


def test_refresh_commands() -> None:
    assert claude_plugin.marketplace_update_command() == [
        "claude",
        "plugin",
        "marketplace",
        "update",
        "jaunt-plugins",
    ]
    assert claude_plugin.plugin_update_command() == [
        "claude",
        "plugin",
        "update",
        "jaunt@jaunt-plugins",
    ]


def test_classify_result() -> None:
    assert claude_plugin.classify_result(0, "done", "") == "ok"
    assert claude_plugin.classify_result(1, "", "marketplace already exists") == "already"
    assert claude_plugin.classify_result(1, "Already installed", "") == "already"
    assert claude_plugin.classify_result(0, "Already installed", "") == "already"
    assert (
        claude_plugin.classify_result(
            1, "", "marketplace is already added from a different source; remove it before adding"
        )
        == "error"
    )
    assert claude_plugin.classify_result(1, "", "network unreachable") == "error"


# --- CLI orchestration ------------------------------------------------------


def test_missing_cli_human(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: None)
    rc = jaunt.cli.main(["install-claude-plugin"])
    assert rc == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Claude Code CLI not found" in captured.err
    assert "claude plugin marketplace add creatorrr/jaunt" in captured.err
    assert "claude plugin install jaunt@jaunt-plugins" in captured.err
    assert "https://jaunt.ing/docs/guides/claude-code-plugin" in captured.err


def test_missing_cli_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: None)
    rc = jaunt.cli.main(["install-claude-plugin", "--json"])
    assert rc == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["command"] == "install-claude-plugin"
    assert payload["ok"] is False
    assert "Claude Code CLI not found" in payload["error"]


def test_happy_path_invokes_both_in_order(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    calls: list[dict] = []
    results = [_FakeResult(0, "added"), _FakeResult(0, "installed")]
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, calls))

    rc = jaunt.cli.main(["install-claude-plugin"])
    assert rc == 0
    assert [c["argv"] for c in calls] == [
        ["claude", "plugin", "marketplace", "add", "creatorrr/jaunt"],
        ["claude", "plugin", "install", "jaunt@jaunt-plugins"],
    ]
    # subprocess calls are non-shell, capture output, and detach stdin.
    for c in calls:
        assert c["kwargs"]["capture_output"] is True
        assert c["kwargs"]["text"] is True
        assert c["kwargs"]["stdin"] is subprocess.DEVNULL
        assert c["kwargs"]["timeout"] == 120
        assert "shell" not in c["kwargs"]
    output = capsys.readouterr().out
    assert "Start a new Claude Code session" in output
    assert "/hooks" in output


def test_happy_path_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    results = [_FakeResult(0, "added"), _FakeResult(0, "installed")]
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, []))

    rc = jaunt.cli.main(["install-claude-plugin", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "command": "install-claude-plugin",
        "ok": True,
        "marketplace": "added",
        "plugin": "installed",
        "local": False,
    }


def test_idempotent_rerun_already(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    results = [
        _FakeResult(1, "", "marketplace 'jaunt-plugins' already exists"),
        _FakeResult(0, "marketplace updated"),
        _FakeResult(1, "", "plugin jaunt is already installed"),
        _FakeResult(0, "plugin updated"),
    ]
    calls: list[dict] = []
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, calls))

    rc = jaunt.cli.main(["install-claude-plugin", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["marketplace"] == "updated"
    assert payload["plugin"] == "updated"
    assert [call["argv"] for call in calls] == [
        ["claude", "plugin", "marketplace", "add", "creatorrr/jaunt"],
        ["claude", "plugin", "marketplace", "update", "jaunt-plugins"],
        ["claude", "plugin", "install", "jaunt@jaunt-plugins"],
        ["claude", "plugin", "update", "jaunt@jaunt-plugins"],
    ]


def test_real_failure_propagates_stderr(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    results = [_FakeResult(1, "", "fatal: network unreachable")]
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, []))

    rc = jaunt.cli.main(["install-claude-plugin"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "network unreachable" in captured.err


def test_real_failure_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    results = [_FakeResult(1, "", "fatal: network unreachable")]
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, []))

    rc = jaunt.cli.main(["install-claude-plugin", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "install-claude-plugin"
    assert payload["ok"] is False
    assert "network unreachable" in payload["error"]


def test_local_requires_manifest(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    called: list[dict] = []
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run([], called))

    rc = jaunt.cli.main(["install-claude-plugin", "--local", "--root", str(tmp_path)])
    assert rc == 2
    assert called == []
    captured = capsys.readouterr()
    assert ".claude-plugin/marketplace.json" in captured.err


def test_local_with_manifest(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    manifest = tmp_path / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    calls: list[dict] = []
    results = [_FakeResult(0, "added"), _FakeResult(0, "installed")]
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run(results, calls))

    rc = jaunt.cli.main(["install-claude-plugin", "--local", "--root", str(tmp_path), "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["local"] is True
    assert calls[0]["argv"] == [
        "claude",
        "plugin",
        "marketplace",
        "add",
        str(tmp_path),
    ]


def test_local_existing_install_updates_in_order(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    manifest = tmp_path / ".claude-plugin" / "marketplace.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}", encoding="utf-8")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run(
            [
                _FakeResult(0, "marketplace already exists"),
                _FakeResult(0, "marketplace updated"),
                _FakeResult(0, "plugin already installed"),
                _FakeResult(0, "plugin updated"),
            ],
            calls,
        ),
    )
    assert (
        jaunt.cli.main(["install-claude-plugin", "--local", "--root", str(tmp_path), "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["plugin"] == "updated"
    assert [call["argv"] for call in calls] == [
        ["claude", "plugin", "marketplace", "add", str(tmp_path)],
        ["claude", "plugin", "marketplace", "update", "jaunt-plugins"],
        ["claude", "plugin", "install", "jaunt@jaunt-plugins"],
        ["claude", "plugin", "update", "jaunt@jaunt-plugins"],
    ]


def test_update_failure_stops_after_exact_refresh_steps(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run(
            [
                _FakeResult(1, "", "marketplace already exists"),
                _FakeResult(0, "marketplace updated"),
                _FakeResult(1, "plugin already installed"),
                _FakeResult(1, "", "update failed"),
            ],
            calls,
        ),
    )
    assert jaunt.cli.main(["install-claude-plugin", "--json"]) == 1
    assert "update failed" in json.loads(capsys.readouterr().out)["error"]
    assert calls[-1]["argv"] == [
        "claude",
        "plugin",
        "update",
        "jaunt@jaunt-plugins",
    ]


def test_timeout_is_reported_in_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/claude")
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run([subprocess.TimeoutExpired(["claude"], 120)], []),
    )
    assert jaunt.cli.main(["install-claude-plugin", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "timed out" in payload["error"].lower()
