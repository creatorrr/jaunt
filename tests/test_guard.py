from __future__ import annotations

import io
import json
import sys

import jaunt.cli
from jaunt import guard


def _payload(tool: str, path: str) -> dict:
    return {"tool_name": tool, "tool_input": {"file_path": path}}


def test_warns_on_generated_path_edit() -> None:
    out = guard.evaluate(
        _payload("Edit", "src/pkg/__generated__/mod.py"),
        generated_dir="__generated__",
    )
    assert out is not None
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "src/pkg/mod.py" in decision["permissionDecisionReason"]


def test_allows_normal_paths_and_non_file_tools() -> None:
    assert guard.evaluate(_payload("Edit", "src/pkg/mod.py"), generated_dir="__generated__") is None
    bash_payload = {"tool_name": "Bash", "tool_input": {"file_path": "src/__generated__/mod.py"}}
    assert guard.evaluate(bash_payload, generated_dir="__generated__") is None
    assert (
        guard.evaluate(
            {"tool_name": "Bash", "tool_input": {"command": "ls"}},
            generated_dir="__generated__",
        )
        is None
    )


def test_never_raises_on_malformed_payload() -> None:
    assert guard.evaluate({}, generated_dir="__generated__") is None
    assert guard.evaluate({"tool_input": None}, generated_dir="__generated__") is None


def test_cli_guard_loads_generated_dir_from_payload_cwd(
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\ngenerated_dir = "gen_out"\n',
        encoding="utf-8",
    )
    payload = _payload("Edit", "src/pkg/gen_out/mod.py")
    payload["cwd"] = str(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))

    assert jaunt.cli.main(["guard"]) == 0

    captured = capsys.readouterr()
    assert captured.err == ""
    out = json.loads(captured.out)
    decision = out["hookSpecificOutput"]
    assert decision["permissionDecision"] == "ask"
    assert "gen_out" in decision["permissionDecisionReason"]


def test_cli_guard_ignores_garbage_stdin(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("{not json"))

    assert jaunt.cli.main(["guard"]) == 0

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""
