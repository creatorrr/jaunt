"""Landing: extract job diffs and commit them onto the developer's branch."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path


class LandingError(Exception):
    pass


def git_out(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise LandingError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def changed_paths(worktree: Path, base_commit: str) -> list[str]:
    git_out(worktree, "add", "-A")
    out = git_out(worktree, "diff", "--cached", "--name-only", base_commit)
    return [path for path in out.splitlines() if path.strip()]


def extract_patch(worktree: Path, base_commit: str, is_allowed: Callable[[str], bool]) -> str:
    paths = changed_paths(worktree, base_commit)
    if not paths:
        return ""
    violations = [path for path in paths if not is_allowed(path)]
    if violations:
        raise LandingError(f"job touched paths outside allowlist: {', '.join(sorted(violations))}")
    return git_out(worktree, "diff", "--cached", "--binary", base_commit, "--", *paths)
