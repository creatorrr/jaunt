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


def test_extract_patch_drops_ignored_paths_before_allowlist(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    gen = repo / "src" / "__generated__"
    gen.mkdir()
    (gen / "app.py").write_text("y = 2\n", encoding="utf-8")
    (repo / "JAUNT_LOG").write_text("worker journal line\n", encoding="utf-8")

    patch = landing.extract_patch(
        repo,
        base,
        is_allowed=lambda path: "/__generated__/" in f"/{path}",
        is_ignored=lambda path: path == "JAUNT_LOG",
    )

    assert "src/__generated__/app.py" in patch
    assert "JAUNT_LOG" not in patch


def test_extract_patch_ignored_only_change_is_empty(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "JAUNT_LOG").write_text("worker journal line\n", encoding="utf-8")

    patch = landing.extract_patch(
        repo,
        base,
        is_allowed=lambda path: False,
        is_ignored=lambda path: path == "JAUNT_LOG",
    )

    assert patch == ""


def test_extract_patch_still_rejects_nonignored_disallowed_path(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "JAUNT_LOG").write_text("worker journal line\n", encoding="utf-8")
    (repo / "src" / "app.py").write_text("x = 999\n", encoding="utf-8")

    with pytest.raises(landing.LandingError, match="src/app.py"):
        landing.extract_patch(
            repo,
            base,
            is_allowed=lambda path: False,
            is_ignored=lambda path: path == "JAUNT_LOG",
        )


def test_extract_patch_empty_when_no_changes(repo: Path) -> None:
    base = _git(repo, "rev-parse", "HEAD")
    assert landing.extract_patch(repo, base, is_allowed=_machine_owned) == ""


def _patch_for(repo: Path, relpath: str, content: str) -> tuple[str, str, list[str]]:
    """Produce (patch, base, paths) for a single-file change without committing it."""
    base = _git(repo, "rev-parse", "HEAD")
    target = repo / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    _git(repo, "add", "--", relpath)
    patch = subprocess.run(
        ["git", "-C", str(repo), "diff", "--cached", "--binary", base],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    _git(repo, "reset", "--hard", base)  # rewind; the patch is the artifact
    return patch, base, [relpath]


def _land(
    repo: Path, patch: str, paths: list[str], msg: str = "m", branch: str = "main"
) -> str | None:
    head = _git(repo, "rev-parse", "HEAD")
    return landing.land(
        repo, patch, patch_paths=paths, message=msg, expected_branch=branch, expected_head=head
    )


def _install_failing_pre_commit(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)


def test_land_commits_with_trailers(repo: Path) -> None:
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 2\n")
    msg = landing.build_commit_message("app", "prose change", "a1b2c3d4", "abcd1234")
    sha = _land(repo, patch, paths, msg=msg)
    assert sha and sha != landing.HEAD_MOVED
    body = _git(repo, "log", "-1", "--format=%B")
    assert "Jaunt-Job: a1b2c3d4" in body and "Jaunt-Spec: abcd1234" in body
    assert (repo / "src/__generated__/app.py").read_text(encoding="utf-8") == "y = 2\n"


def test_land_is_pathspec_limited(repo: Path) -> None:
    (repo / "notes.txt").write_text("dev work in progress\n", encoding="utf-8")
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 3\n")
    sha = _land(repo, patch, paths, msg="regen(app): x")
    assert sha and sha != landing.HEAD_MOVED
    committed = _git(repo, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert committed == ["src/__generated__/app.py"]
    assert (repo / "notes.txt").exists()  # untouched, uncommitted
    assert (repo / "notes.txt").read_text(encoding="utf-8") == "dev work in progress\n"
    assert _git(repo, "status", "--porcelain", "--", "notes.txt") == "?? notes.txt"


def test_land_defers_when_head_moved(repo: Path) -> None:
    stale_head = _git(repo, "rev-parse", "HEAD")
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 9\n")
    (repo / "other.txt").write_text("x\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "user commit moves HEAD")
    result = landing.land(
        repo,
        patch,
        patch_paths=paths,
        message="m",
        expected_branch="main",
        expected_head=stale_head,
    )
    assert result == landing.HEAD_MOVED
    assert _git(repo, "status", "--porcelain") == ""  # nothing applied


def test_land_parks_on_wrong_branch(repo: Path) -> None:
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 4\n")
    _git(repo, "checkout", "-b", "other")
    assert _land(repo, patch, paths) is None


def test_land_parks_on_locally_modified_generated_path(repo: Path) -> None:
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 5\n")
    (repo / "src/__generated__").mkdir(exist_ok=True)
    (repo / "src/__generated__/app.py").write_text("hand edit\n", encoding="utf-8")
    assert _land(repo, patch, paths) is None


def test_land_parks_on_conflict(repo: Path) -> None:
    # Patch built against a file state that no longer exists after a conflicting commit.
    patch, base, paths = _patch_for(repo, "src/__generated__/app.py", "y = 6\n")
    (repo / "src/__generated__").mkdir(exist_ok=True)
    (repo / "src/__generated__/app.py").write_text(
        "conflicting committed content\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "conflicting")
    result = _land(repo, patch, paths)
    assert result is None
    status = _git(repo, "status", "--porcelain")
    assert status == ""  # no half-applied state left behind


def test_land_rolls_back_when_commit_hook_fails(repo: Path) -> None:
    patch, _, paths = _patch_for(repo, "src/__generated__/app.py", "y = 7\n")
    _install_failing_pre_commit(repo)

    assert _land(repo, patch, paths) is None
    assert _git(repo, "status", "--porcelain") == ""
