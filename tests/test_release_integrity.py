from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
VERIFY_TAGS = ROOT / "scripts" / "verify_release_tags.py"
VERIFY_PYPI = ROOT / "scripts" / "verify_pypi_candidates.py"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_release_tag_may_be_absent_or_target_the_expected_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Release Test")
    _git(tmp_path, "config", "user.email", "release@example.test")
    (tmp_path / "tracked").write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked")
    _git(tmp_path, "commit", "--quiet", "-m", "first")
    expected = _git(tmp_path, "rev-parse", "HEAD")

    subprocess.run(
        [
            "python",
            str(VERIFY_TAGS),
            "--expected-commit",
            expected,
            "v1.0.0",
        ],
        cwd=tmp_path,
        check=True,
    )
    _git(tmp_path, "tag", "-a", "v1.0.0", "-m", "release")
    subprocess.run(
        ["python", str(VERIFY_TAGS), "--expected-commit", expected, "v1.0.0"],
        cwd=tmp_path,
        check=True,
    )


def test_release_tag_rejects_a_different_commit(tmp_path: Path) -> None:
    _git(tmp_path, "init", "--quiet")
    _git(tmp_path, "config", "user.name", "Release Test")
    _git(tmp_path, "config", "user.email", "release@example.test")
    (tmp_path / "tracked").write_text("first\n", encoding="utf-8")
    _git(tmp_path, "add", "tracked")
    _git(tmp_path, "commit", "--quiet", "-m", "first")
    _git(tmp_path, "tag", "v1.0.0")
    (tmp_path / "tracked").write_text("second\n", encoding="utf-8")
    _git(tmp_path, "commit", "--quiet", "-am", "second")
    expected = _git(tmp_path, "rev-parse", "HEAD")

    result = subprocess.run(
        ["python", str(VERIFY_TAGS), "--expected-commit", expected, "v1.0.0"],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "points to" in result.stderr
    assert "expected" in result.stderr


def test_pypi_candidate_digests_require_the_exact_file_set_and_bytes(tmp_path: Path) -> None:
    wheel = tmp_path / "jaunt-2.0.0-py3-none-any.whl"
    sdist = tmp_path / "jaunt-2.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    metadata: dict[str, Any] = {
        "urls": [
            {
                "filename": path.name,
                "digests": {"sha256": hashlib.sha256(path.read_bytes()).hexdigest()},
            }
            for path in (wheel, sdist)
        ]
    }
    metadata_file = tmp_path / "metadata.json"
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    command = [
        "python",
        str(VERIFY_PYPI),
        "--project",
        "jaunt",
        "--version",
        "2.0.0",
        "--dist",
        str(tmp_path),
        "--metadata-file",
        str(metadata_file),
    ]

    subprocess.run(command, check=True)
    metadata["urls"] = [
        {
            "filename": wheel.name,
            "digests": {"sha256": "0" * 64},
        },
        {
            "filename": sdist.name,
            "digests": {"sha256": hashlib.sha256(sdist.read_bytes()).hexdigest()},
        },
    ]
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    mismatch = subprocess.run(command, check=False, capture_output=True, text=True)
    assert mismatch.returncode != 0
    assert "PyPI bytes differ" in mismatch.stderr

    metadata["urls"] = metadata["urls"][:1]
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")
    missing = subprocess.run(command, check=False, capture_output=True, text=True)
    assert missing.returncode != 0
    assert "candidate set differs" in missing.stderr


def test_workflows_gate_release_integrity_and_typescript_fixture_freshness() -> None:
    root = Path(__file__).parents[1]
    release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert release.count("scripts/verify_release_tags.py") >= 4
    assert release.count("scripts/verify_pypi_candidates.py") >= 3
    assert "git fetch --force --tags origin" in release
    assert "jaunt check --language ts --root examples/typescript-jwt" in release
    assert "jaunt check --language ts --root examples/typescript-jwt" in ci
    assert 'if [[ -n "$published_commit" && "$published_commit" != "$GITHUB_SHA" ]]' in release
    assert "verify_pypi_candidates.py" in release
    assert 'published_integrity="$(npm view' in release
