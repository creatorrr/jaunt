from __future__ import annotations

import json
from pathlib import Path

from jaunt import cli

SRC = (
    "import jaunt\n\n\n"
    "@jaunt.contract\n"
    "def shout(text: str) -> str:\n"
    '    """Uppercase. Examples:\n    - "hi" -> "HI"\n    """\n'
    "    return text.upper()\n"
)


def _project(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(SRC, encoding="utf-8")
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n[paths]\nsource_roots = ["src"]\ntest_roots = ["tests"]\n',
        encoding="utf-8",
    )
    return tmp_path


def test_status_json_includes_contracts(tmp_path: Path, capsys) -> None:
    root = _project(tmp_path)
    assert cli.cmd_reconcile(cli.parse_args(["reconcile", "--root", str(root)])) == cli.EXIT_OK
    capsys.readouterr()
    rc = cli.cmd_status(cli.parse_args(["status", "--root", str(root), "--json"]))
    assert rc == cli.EXIT_OK
    data = json.loads(capsys.readouterr().out)
    contracts = {c["ref"]: c for c in data["contracts"]}
    assert "demo:shout" in contracts
    assert contracts["demo:shout"]["state"] == "in-sync"
    assert "/" in contracts["demo:shout"]["strength"]
