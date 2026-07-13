#!/usr/bin/env python3
"""Reject release tags that already point at a different commit."""

from __future__ import annotations

import argparse
import subprocess


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def verify_release_tags(*, expected_commit: str, tags: list[str]) -> None:
    expected = _git("rev-parse", "--verify", f"{expected_commit}^{{commit}}")
    if expected.returncode != 0:
        raise ValueError(f"expected commit does not exist: {expected_commit}")
    expected_sha = expected.stdout.strip()

    for tag in tags:
        ref = f"refs/tags/{tag}"
        existing = _git("rev-parse", "--verify", "--quiet", ref)
        if existing.returncode != 0:
            continue
        target = _git("rev-parse", "--verify", f"{ref}^{{commit}}")
        if target.returncode != 0:
            raise ValueError(f"release tag {tag} does not resolve to a commit")
        target_sha = target.stdout.strip()
        if target_sha != expected_sha:
            raise ValueError(f"release tag {tag} points to {target_sha}; expected {expected_sha}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("tags", nargs="+")
    args = parser.parse_args()
    try:
        verify_release_tags(expected_commit=args.expected_commit, tags=args.tags)
    except ValueError as error:
        raise SystemExit(str(error)) from error


if __name__ == "__main__":
    main()
