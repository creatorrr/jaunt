from __future__ import annotations

import os
from pathlib import Path

from jaunt.dotenv import load_dotenv, load_dotenv_into_environ


def test_load_dotenv_parses_simple_lines(tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text(
        "\n".join(
            [
                "# comment",
                "OPENAI_API_KEY=sk-test",
                "export FOO=bar",
                "QUOTED='hello world'",
                'DQUOTED="hi there"',
                "EMPTY=",
                "NOEQUALS",
                "",
            ]
        ),
        encoding="utf-8",
    )

    vals = load_dotenv(p)
    assert vals["OPENAI_API_KEY"] == "sk-test"
    assert vals["FOO"] == "bar"
    assert vals["QUOTED"] == "hello world"
    assert vals["DQUOTED"] == "hi there"
    assert vals["EMPTY"] == ""
    assert "NOEQUALS" not in vals


def test_load_dotenv_into_environ_does_not_override_existing(monkeypatch, tmp_path: Path) -> None:
    p = tmp_path / ".env"
    p.write_text("A=1\nB=2\n", encoding="utf-8")

    monkeypatch.setenv("A", "existing")
    monkeypatch.delenv("B", raising=False)

    ok = load_dotenv_into_environ(p)
    assert ok is True
    assert os.environ["A"] == "existing"
    assert os.environ["B"] == "2"


def test_load_dotenv_into_environ_returns_false_when_missing(tmp_path: Path) -> None:
    p = tmp_path / "missing.env"
    assert load_dotenv_into_environ(p) is False

