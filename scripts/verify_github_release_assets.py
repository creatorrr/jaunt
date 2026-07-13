#!/usr/bin/env python3
"""Verify GitHub release assets against a component-specific checksum manifest."""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path

_CHECKSUM_RE = re.compile(r"^(?P<digest>[0-9a-f]{64}) [ *](?P<name>[^/\\]+)$")
_IGNORED_BUILD_FILES = frozenset({"pack.json"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _release_files(directory: Path, *, ignored: frozenset[str] = frozenset()) -> dict[str, Path]:
    return {
        path.name: path
        for path in directory.iterdir()
        if path.is_file() and path.name not in ignored
    }


def verify_assets(*, expected_dir: Path, downloaded_dir: Path, allow_missing: bool) -> None:
    manifest = expected_dir / "SHA256SUMS"
    if not manifest.is_file():
        raise ValueError(f"missing component checksum manifest: {manifest}")

    expected = _release_files(expected_dir, ignored=_IGNORED_BUILD_FILES)
    candidate_names = set(expected) - {manifest.name}
    checksums: dict[str, str] = {}
    for line_number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = _CHECKSUM_RE.fullmatch(line)
        if match is None:
            raise ValueError(f"invalid SHA256SUMS line {line_number}: {line!r}")
        name = match.group("name")
        if name in checksums:
            raise ValueError(f"duplicate SHA256SUMS entry: {name}")
        checksums[name] = match.group("digest")

    if set(checksums) != candidate_names:
        raise ValueError(
            "checksum manifest file set differs from candidates: "
            f"manifest={sorted(checksums)}, candidates={sorted(candidate_names)}"
        )
    for name, digest in checksums.items():
        actual = _sha256(expected[name])
        if actual != digest:
            raise ValueError(
                f"candidate {name} does not match SHA256SUMS: expected {digest}, got {actual}"
            )

    downloaded = _release_files(downloaded_dir)
    unexpected = set(downloaded) - set(expected)
    if unexpected:
        raise ValueError(f"GitHub release has unexpected assets: {sorted(unexpected)}")

    for name, downloaded_path in downloaded.items():
        expected_digest = _sha256(expected[name])
        actual_digest = _sha256(downloaded_path)
        if actual_digest != expected_digest:
            raise ValueError(
                f"GitHub release asset {name} differs from the candidate: "
                f"expected {expected_digest}, got {actual_digest}"
            )

    missing = set(expected) - set(downloaded)
    if missing and not allow_missing:
        raise ValueError(f"GitHub release is missing assets: {sorted(missing)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expected-dir", type=Path, required=True)
    parser.add_argument("--downloaded-dir", type=Path, required=True)
    parser.add_argument(
        "--allow-missing",
        action="store_true",
        help="verify all present assets but permit a resumable release to omit candidates",
    )
    args = parser.parse_args()
    try:
        verify_assets(
            expected_dir=args.expected_dir,
            downloaded_dir=args.downloaded_dir,
            allow_missing=args.allow_missing,
        )
    except (OSError, ValueError) as error:
        parser.exit(1, f"error: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
