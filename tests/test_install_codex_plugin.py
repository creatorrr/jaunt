from __future__ import annotations

import json
import subprocess

import jaunt.cli
from jaunt import codex_plugin


class _FakeResult:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _record_run(results: list[object], calls: list[dict]):
    def _run(argv, **kwargs):
        calls.append({"argv": list(argv), "kwargs": kwargs})
        result = results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    return _run


def test_codex_plugin_commands_and_classification() -> None:
    assert codex_plugin.marketplace_add_command(local_path=None) == [
        "codex",
        "plugin",
        "marketplace",
        "add",
        "creatorrr/jaunt",
    ]
    assert codex_plugin.marketplace_add_command(local_path="/repo") == [
        "codex",
        "plugin",
        "marketplace",
        "add",
        "/repo",
    ]
    assert codex_plugin.plugin_install_command() == [
        "codex",
        "plugin",
        "add",
        "jaunt@jaunt-codex-plugins",
    ]
    assert codex_plugin.marketplace_upgrade_command() == [
        "codex",
        "plugin",
        "marketplace",
        "upgrade",
        "jaunt-codex-plugins",
    ]
    assert codex_plugin.plugin_remove_command() == [
        "codex",
        "plugin",
        "remove",
        "jaunt@jaunt-codex-plugins",
    ]
    assert codex_plugin.classify_result(0, "", "") == "ok"
    assert codex_plugin.classify_result(1, "", "already installed") == "already"
    assert codex_plugin.classify_result(0, "already installed", "") == "already"
    assert (
        codex_plugin.classify_result(
            1, "", "marketplace is already added from a different source; remove it before adding"
        )
        == "error"
    )
    assert codex_plugin.classify_result(1, "", "network error") == "error"


def test_missing_codex_cli_human_and_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: None)
    assert jaunt.cli.main(["install-codex-plugin"]) == 2
    captured = capsys.readouterr()
    assert "Codex CLI not found" in captured.err
    assert "codex plugin marketplace add creatorrr/jaunt" in captured.err
    assert "codex plugin add jaunt@jaunt-codex-plugins" in captured.err

    assert jaunt.cli.main(["install-codex-plugin", "--json"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "install-codex-plugin"
    assert payload["ok"] is False


def test_default_install_order_and_human_handoff(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run([_FakeResult(0), _FakeResult(0)], calls),
    )
    assert jaunt.cli.main(["install-codex-plugin"]) == 0
    assert [call["argv"] for call in calls] == [
        ["codex", "plugin", "marketplace", "add", "creatorrr/jaunt"],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
    ]
    for call in calls:
        assert call["kwargs"]["capture_output"] is True
        assert call["kwargs"]["text"] is True
        assert call["kwargs"]["stdin"] is subprocess.DEVNULL
        assert call["kwargs"]["timeout"] == 120
        assert "shell" not in call["kwargs"]
    output = capsys.readouterr().out
    assert "Start a new Codex session" in output
    assert "/hooks" in output


def test_default_install_json_and_already_results(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run(
            [
                _FakeResult(1, "", "marketplace already exists"),
                _FakeResult(0, "marketplace upgraded"),
                _FakeResult(1, "plugin already installed"),
                _FakeResult(0, "plugin removed"),
                _FakeResult(0, "plugin installed"),
            ],
            calls,
        ),
    )
    assert jaunt.cli.main(["install-codex-plugin", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "command": "install-codex-plugin",
        "ok": True,
        "marketplace": "updated",
        "plugin": "refreshed",
        "local": False,
    }
    assert [call["argv"] for call in calls] == [
        ["codex", "plugin", "marketplace", "add", "creatorrr/jaunt"],
        ["codex", "plugin", "marketplace", "upgrade", "jaunt-codex-plugins"],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
        ["codex", "plugin", "remove", "jaunt@jaunt-codex-plugins"],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
    ]


def test_local_install_requires_marketplace(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    calls: list[dict] = []
    monkeypatch.setattr(jaunt.cli.subprocess, "run", _record_run([], calls))
    assert jaunt.cli.main(["install-codex-plugin", "--local", "--root", str(tmp_path)]) == 2
    assert calls == []
    assert ".agents/plugins/marketplace.json" in capsys.readouterr().err


def test_local_install_uses_root_and_json(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text("{}")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run([_FakeResult(0), _FakeResult(0)], calls),
    )
    assert (
        jaunt.cli.main(["install-codex-plugin", "--local", "--root", str(tmp_path), "--json"]) == 0
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload["local"] is True
    assert calls[0]["argv"] == ["codex", "plugin", "marketplace", "add", str(tmp_path)]


def test_local_existing_install_refreshes_without_git_upgrade(
    monkeypatch, tmp_path, capsys
) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    marketplace = tmp_path / ".agents" / "plugins" / "marketplace.json"
    marketplace.parent.mkdir(parents=True)
    marketplace.write_text("{}")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run(
            [
                _FakeResult(0, "marketplace already exists"),
                _FakeResult(0, "plugin already installed"),
                _FakeResult(0, "plugin removed"),
                _FakeResult(0, "plugin installed"),
            ],
            calls,
        ),
    )
    assert (
        jaunt.cli.main(["install-codex-plugin", "--local", "--root", str(tmp_path), "--json"]) == 0
    )
    assert json.loads(capsys.readouterr().out)["plugin"] == "refreshed"
    assert [call["argv"] for call in calls] == [
        ["codex", "plugin", "marketplace", "add", str(tmp_path)],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
        ["codex", "plugin", "remove", "jaunt@jaunt-codex-plugins"],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
    ]


def test_subprocess_failure_and_timeout(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run([_FakeResult(1, "", "network unreachable")], []),
    )
    assert jaunt.cli.main(["install-codex-plugin", "--json"]) == 1
    assert "network unreachable" in json.loads(capsys.readouterr().out)["error"]

    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run([subprocess.TimeoutExpired(["codex"], 120)], []),
    )
    assert jaunt.cli.main(["install-codex-plugin", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert "timed out" in payload["error"].lower()


def test_refresh_reinstall_failure_is_actionable_and_preserves_order(monkeypatch, capsys) -> None:
    monkeypatch.setattr(jaunt.cli.shutil, "which", lambda _name: "/usr/bin/codex")
    calls: list[dict] = []
    monkeypatch.setattr(
        jaunt.cli.subprocess,
        "run",
        _record_run(
            [
                _FakeResult(1, "", "marketplace already exists"),
                _FakeResult(0, "marketplace upgraded"),
                _FakeResult(1, "plugin already installed"),
                _FakeResult(0, "plugin removed"),
                _FakeResult(1, "", "network unavailable"),
            ],
            calls,
        ),
    )
    assert jaunt.cli.main(["install-codex-plugin", "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert "removed the cached Jaunt plugin" in payload["error"]
    assert "codex plugin add jaunt@jaunt-codex-plugins" in payload["error"]
    assert [call["argv"] for call in calls][-2:] == [
        ["codex", "plugin", "remove", "jaunt@jaunt-codex-plugins"],
        ["codex", "plugin", "add", "jaunt@jaunt-codex-plugins"],
    ]
