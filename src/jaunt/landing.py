"""Landing: extract job diffs and commit them onto the developer's branch."""

from __future__ import annotations

import subprocess
import tempfile
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

HEAD_MOVED = "HEAD_MOVED"  # sentinel: caller defers landing to the next daemon iteration


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


def extract_patch(
    worktree: Path,
    base_commit: str,
    is_allowed: Callable[[str], bool],
    *,
    is_ignored: Callable[[str], bool] | None = None,
) -> str:
    paths = changed_paths(worktree, base_commit)
    if is_ignored is not None:
        paths = [path for path in paths if not is_ignored(path)]
    if not paths:
        return ""
    violations = [path for path in paths if not is_allowed(path)]
    if violations:
        raise LandingError(f"job touched paths outside allowlist: {', '.join(sorted(violations))}")
    return git_out(worktree, "diff", "--cached", "--binary", base_commit, "--", *paths)


def validate_patch(
    repo: Path,
    patch: str,
    *,
    patch_paths: Sequence[str],
    validator: Callable[[Path], tuple[bool, str]],
) -> tuple[bool, str]:
    """Apply a proposal in a disposable worktree and run a scoped gate.

    Proposal landing can happen long after generation.  Revalidating away from
    the developer's checkout keeps failure atomic and prevents a force/retry
    path from bypassing target conformance.
    """

    if not patch or not patch_paths:
        return False, "proposal has no patch"
    parent = Path(tempfile.mkdtemp(prefix="jaunt-land-"))
    worktree = parent / "worktree"
    patch_file = parent / "proposal.patch"
    try:
        git_out(repo, "worktree", "add", "--detach", str(worktree), "HEAD")
        patch_file.write_text(patch, encoding="utf-8")
        applied = subprocess.run(
            [
                "git",
                "-C",
                str(worktree),
                "apply",
                "--3way",
                *_apply_include_args(patch_paths),
                str(patch_file),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if applied.returncode != 0:
            return False, "proposal no longer applies cleanly"
        return validator(worktree)
    finally:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)],
            capture_output=True,
            text=True,
            check=False,
        )
        shutil.rmtree(parent, ignore_errors=True)


def build_commit_message(module: str, cause: str, job_id: str, spec_digest: str) -> str:
    return f"regen({module}): {cause}\n\nJaunt-Job: {job_id}\nJaunt-Spec: {spec_digest[:8]}\n"


def _current_branch(repo: Path) -> str:
    return git_out(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


def _rollback_paths(repo: Path, patch_paths: Sequence[str]) -> None:
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "restore",
            "--staged",
            "--worktree",
            "--source=HEAD",
            "--",
            *patch_paths,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(repo), "clean", "-fd", "--", *patch_paths],
        capture_output=True,
        text=True,
        check=False,
    )


def _unstage_paths(repo: Path, paths: Sequence[str]) -> None:
    if not paths:
        return
    subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "restore",
            "--staged",
            "--source=HEAD",
            "--",
            *paths,
        ],
        capture_output=True,
        text=True,
        check=False,
    )


def _apply_include_args(patch_paths: Sequence[str]) -> list[str]:
    return [f"--include={path}" for path in patch_paths]


def land(
    repo: Path,
    patch: str,
    *,
    patch_paths: Sequence[str],
    message: str,
    expected_branch: str,
    expected_head: str,
    extra_commit_paths: Sequence[str] = (),
) -> str | None:
    if not patch or not patch_paths:
        return None
    if _current_branch(repo) != expected_branch:
        return None
    if git_out(repo, "rev-parse", "HEAD").strip() != expected_head:
        # A commit landed after the daemon probed this HEAD. Defer to the next
        # iteration, which re-probes and either supersedes this job or lands it.
        return HEAD_MOVED
    dirty = git_out(repo, "status", "--porcelain", "--", *patch_paths).strip()
    if dirty:
        return None

    patch_file = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as f:
            f.write(patch)
            patch_file = f.name
        apply_proc = subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "apply",
                "--3way",
                *_apply_include_args(patch_paths),
                patch_file,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if apply_proc.returncode != 0:
            _rollback_paths(repo, patch_paths)
            return None
        # This only narrows the race; git has no compare-and-swap commit primitive.
        if git_out(repo, "rev-parse", "HEAD").strip() != expected_head:
            _rollback_paths(repo, patch_paths)
            return HEAD_MOVED
        commit_paths = [*patch_paths, *extra_commit_paths]
        try:
            git_out(repo, "add", "--", *commit_paths)
            git_out(repo, "commit", "-m", message, "--", *commit_paths)
            return git_out(repo, "rev-parse", "HEAD").strip()
        except LandingError:
            _rollback_paths(repo, patch_paths)
            _unstage_paths(repo, extra_commit_paths)
            return None
    finally:
        if patch_file:
            Path(patch_file).unlink(missing_ok=True)
