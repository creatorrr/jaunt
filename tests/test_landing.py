from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jaunt import landing


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-b", "main")
    _git(r, "config", "user.email", "t@example.com")
    _git(r, "config", "user.name", "T")
    (r / "src").mkdir()
    (r / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def _machine_owned(path: str) -> bool:
    return "/__generated__/" in f"/{path}" or path == "JAUNT_LOG"


def test_extract_patch_scoped_by_predicate(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    gen = repo / "src" / "__generated__"
    gen.mkdir()
    (gen / "app.py").write_text("y = 2\n", encoding="utf-8")
    patch = landing.extract_patch(repo, base, is_allowed=_machine_owned)
    assert "src/__generated__/app.py" in patch


def test_extract_patch_rejects_out_of_scope_paths(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "src" / "app.py").write_text("x = 999\n", encoding="utf-8")
    with pytest.raises(landing.LandingError, match="src/app.py"):
        landing.extract_patch(repo, base, is_allowed=_machine_owned)


def test_extract_patch_empty_when_no_changes(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    assert landing.extract_patch(repo, base, is_allowed=_machine_owned) == ""
