#!/usr/bin/env python3
"""Verify that PyPI serves the exact wheel and sdist release candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import quote
from urllib.request import urlopen


def _candidate_files(directory: Path) -> list[Path]:
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and (path.suffix == ".whl" or path.name.endswith(".tar.gz"))
    )


def verify_candidate_digests(*, directory: Path, metadata: Mapping[str, Any]) -> None:
    candidates = _candidate_files(directory)
    if not candidates:
        raise ValueError(f"no wheel or source distribution found in {directory}")

    urls = metadata.get("urls")
    if not isinstance(urls, list):
        raise ValueError("PyPI metadata has no urls list")
    published: dict[str, str] = {}
    for item in urls:
        if not isinstance(item, Mapping):
            continue
        filename = item.get("filename")
        digests = item.get("digests")
        digest = digests.get("sha256") if isinstance(digests, Mapping) else None
        if isinstance(filename, str) and isinstance(digest, str):
            published[filename] = digest

    candidate_names = {path.name for path in candidates}
    published_names = set(published)
    if candidate_names != published_names:
        missing = sorted(candidate_names - published_names)
        unexpected = sorted(published_names - candidate_names)
        details = []
        if missing:
            details.append(f"missing from PyPI: {', '.join(missing)}")
        if unexpected:
            details.append(f"unexpected on PyPI: {', '.join(unexpected)}")
        raise ValueError("PyPI candidate set differs: " + "; ".join(details))

    mismatched = []
    for path in candidates:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != published[path.name]:
            mismatched.append(path.name)
    if mismatched:
        raise ValueError(f"PyPI bytes differ for: {', '.join(mismatched)}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--dist", required=True, type=Path)
    parser.add_argument("--metadata-file", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()

    project = quote(args.project, safe="")
    version = quote(args.version, safe="")
    url = f"https://pypi.org/pypi/{project}/{version}/json"
    try:
        if args.metadata_file is not None:
            metadata = json.loads(args.metadata_file.read_text(encoding="utf-8"))
        else:
            with urlopen(url, timeout=30) as response:  # noqa: S310 - fixed trusted host
                metadata = json.load(response)
        if not isinstance(metadata, Mapping):
            raise ValueError("PyPI metadata is not an object")
        verify_candidate_digests(directory=args.dist, metadata=metadata)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(str(error)) from error
    print(f"verified exact PyPI bytes for {args.project} {args.version}")


if __name__ == "__main__":
    main()
