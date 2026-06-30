import json
from pathlib import Path

import jaunt.repo_context.search as search


def test_available_false_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: None)
    assert search.available() is False


def test_query_parses_json(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    class _CP:
        returncode = 0
        stdout = json.dumps(
            [{"unit": {"file": "src/a.py", "snippet": "def f(): ..."}, "score": 0.9}]
        )
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _CP())
    hits = search.query("auth token", root=tmp_path, max_hits=8)
    assert len(hits) == 1 and hits[0].file == "src/a.py"


def test_query_timeout_returns_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    def _boom(*a, **k):
        raise search.subprocess.TimeoutExpired(cmd="colgrep", timeout=5)

    monkeypatch.setattr(search.subprocess, "run", _boom)
    assert search.query("x", root=tmp_path) == []


def test_query_malformed_json_returns_empty(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(search.shutil, "which", lambda _: "/usr/bin/colgrep")

    class _CP:
        returncode = 0
        stdout = "not json"
        stderr = ""

    monkeypatch.setattr(search.subprocess, "run", lambda *a, **k: _CP())
    assert search.query("x", root=tmp_path) == []
