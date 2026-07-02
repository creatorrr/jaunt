"""End-to-end: adopt an async function via the CLI; fixture failure at check time."""

from __future__ import annotations

import json
from pathlib import Path

import jaunt.cli


def _project(tmp_path: Path, module_src: str) -> None:
    (tmp_path / "src").mkdir(parents=True, exist_ok=True)
    (tmp_path / "jaunt.toml").write_text(
        'version = 1\n\n[paths]\nsource_roots = ["src"]\n\n[contract]\nstrength = false\n',
        encoding="utf-8",
    )
    (tmp_path / "src" / "amod.py").write_text(module_src, encoding="utf-8")


ASYNC_SRC = '''
async def double(x: int) -> int:
    """Double.

    Examples:
        - double(2) == 4
    """
    return x * 2
'''


def test_adopt_async_function_via_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, ASYNC_SRC)

    code = jaunt.cli.main(["adopt", "amod:double", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["ok"] is True

    marked = (tmp_path / "src" / "amod.py").read_text(encoding="utf-8")
    assert "@jaunt.contract" in marked

    battery = tmp_path / "tests" / "contract" / "amod" / "test_double.py"
    text = battery.read_text(encoding="utf-8")
    assert "async def test_examples():" in text

    code = jaunt.cli.main(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out


FIXTURE_SRC = '''
import jaunt


@jaunt.contract
def lookup(db, key: str) -> str:
    """Look up.

    Examples:
        - lookup(db, 'a') == 'A'

    Fixtures: db
    """
    return db[key]
'''


def test_missing_fixture_at_check_is_behavior_drift(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.chdir(tmp_path)
    _project(tmp_path, FIXTURE_SRC)
    # Battery written with a working conftest...
    conftest = tmp_path / "tests" / "contract" / "conftest.py"
    conftest.parent.mkdir(parents=True)
    conftest.write_text(
        "import pytest\n\n@pytest.fixture\ndef db():\n    return {'a': 'A'}\n",
        encoding="utf-8",
    )
    code = jaunt.cli.main(["reconcile", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out

    # ...then the conftest disappears: check must block with behavior drift.
    conftest.unlink()
    code = jaunt.cli.main(["check", "--root", str(tmp_path), "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 4
    assert any("behavior-drift" in json.dumps(row) for row in out.get("contracts", [out]))
