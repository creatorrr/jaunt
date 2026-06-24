from __future__ import annotations

import json

import jaunt.cli


def test_cmd_eval_deferred_text(capsys) -> None:
    args = jaunt.cli.parse_args(["eval"])
    rc = jaunt.cli.cmd_eval(args)

    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    assert "not supported under the Codex engine" in capsys.readouterr().err


def test_cmd_eval_deferred_json(capsys) -> None:
    args = jaunt.cli.parse_args(["eval", "--json"])
    rc = jaunt.cli.cmd_eval(args)

    assert rc == jaunt.cli.EXIT_CONFIG_OR_DISCOVERY
    payload = json.loads(capsys.readouterr().out)
    assert payload["command"] == "eval"
    assert payload["ok"] is False
    assert "not supported under the Codex engine" in payload["error"]
