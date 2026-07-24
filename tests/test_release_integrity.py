from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[1]
VERIFY_TAGS = ROOT / "scripts" / "verify_release_tags.py"
VERIFY_PYPI = ROOT / "scripts" / "verify_pypi_candidates.py"
VERIFY_GITHUB_ASSETS = ROOT / "scripts" / "verify_github_release_assets.py"


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


def test_github_release_assets_are_component_scoped_and_resumable(tmp_path: Path) -> None:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    expected.mkdir()
    downloaded.mkdir()
    wheel = expected / "jaunt-2.0.0-py3-none-any.whl"
    sdist = expected / "jaunt-2.0.0.tar.gz"
    wheel.write_bytes(b"wheel")
    sdist.write_bytes(b"sdist")
    manifest = expected / "SHA256SUMS"
    manifest.write_text(
        "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in (wheel, sdist)
        ),
        encoding="utf-8",
    )
    command = [
        "python",
        str(VERIFY_GITHUB_ASSETS),
        "--expected-dir",
        str(expected),
        "--downloaded-dir",
        str(downloaded),
    ]

    # An interrupted release can be checked before missing assets are uploaded.
    (downloaded / wheel.name).write_bytes(wheel.read_bytes())
    subprocess.run([*command, "--allow-missing"], check=True)
    incomplete = subprocess.run(command, check=False, capture_output=True, text=True)
    assert incomplete.returncode != 0
    assert "missing assets" in incomplete.stderr

    (downloaded / sdist.name).write_bytes(sdist.read_bytes())
    (downloaded / manifest.name).write_bytes(manifest.read_bytes())
    subprocess.run(command, check=True)

    (downloaded / wheel.name).write_bytes(b"different")
    mismatch = subprocess.run(command, check=False, capture_output=True, text=True)
    assert mismatch.returncode != 0
    assert "differs from the candidate" in mismatch.stderr

    (downloaded / wheel.name).write_bytes(wheel.read_bytes())
    (downloaded / "pack.json").write_text("{}\n", encoding="utf-8")
    unexpected = subprocess.run(command, check=False, capture_output=True, text=True)
    assert unexpected.returncode != 0
    assert "unexpected assets" in unexpected.stderr


def test_github_release_assets_reject_unsafe_or_cross_component_manifests(
    tmp_path: Path,
) -> None:
    expected = tmp_path / "expected"
    downloaded = tmp_path / "downloaded"
    expected.mkdir()
    downloaded.mkdir()
    candidate = expected / "package.tgz"
    candidate.write_bytes(b"npm")
    (expected / "SHA256SUMS").write_text(
        f"{hashlib.sha256(candidate.read_bytes()).hexdigest()}  release/npm/package.tgz\n",
        encoding="utf-8",
    )
    command = [
        "python",
        str(VERIFY_GITHUB_ASSETS),
        "--expected-dir",
        str(expected),
        "--downloaded-dir",
        str(downloaded),
        "--allow-missing",
    ]

    invalid = subprocess.run(command, check=False, capture_output=True, text=True)
    assert invalid.returncode != 0
    assert "invalid SHA256SUMS line" in invalid.stderr


def test_workflows_gate_release_integrity_and_typescript_fixture_freshness() -> None:
    root = Path(__file__).parents[1]
    release = (root / ".github/workflows/release.yml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert release.count("scripts/verify_release_tags.py") >= 4
    assert release.count("scripts/verify_pypi_candidates.py") >= 3
    assert "git fetch --force --tags origin" in release
    assert "jaunt check --language ts --root examples/typescript-jwt" in release
    assert "jaunt check --language ts --root examples/typescript-jwt" in ci
    candidate_refreeze = (
        '"$jaunt_bin" test --language ts --no-build --no-run --root "$project" --json'
    )
    candidate_check = '"$jaunt_bin" check --language ts --magic-only --root "$project"'
    candidate_typecheck = 'npm --prefix "$project" run typecheck'
    candidate_vitest = 'npm --prefix "$project" test'
    registry_refreeze = (
        '"$venv/bin/jaunt" test --language ts --no-build --no-run --root "$project" --json'
    )
    registry_check = '"$venv/bin/jaunt" check --language ts --magic-only --root "$project"'
    assert release.count(candidate_refreeze) == 1
    assert release.count(registry_refreeze) == 1
    assert candidate_check in release
    assert registry_check in release
    candidate_offset = release.index(candidate_refreeze)
    candidate_check_offset = release.index(candidate_check, candidate_offset)
    candidate_guard = release[candidate_offset:candidate_check_offset]
    registry_offset = release.index(registry_refreeze)
    registry_check_offset = release.index(registry_check, registry_offset)
    registry_guard = release[registry_offset:registry_check_offset]
    for guard, failure_message in (
        (candidate_guard, "Unexpected exact-candidate refreeze report"),
        (registry_guard, "Unexpected registry refreeze report"),
    ):
        assert 'payload.get("generated") == []' in guard
        assert 'payload.get("skipped") == []' in guard
        assert 'payload.get("failed") == {}' in guard
        assert 'payload.get("refrozen") == expected' in guard
        assert failure_message in guard
    assert (
        candidate_offset
        < candidate_check_offset
        < release.index(candidate_typecheck)
        < release.index(candidate_vitest)
    )
    assert (
        registry_offset
        < registry_check_offset
        < release.index(candidate_typecheck, registry_offset)
        < release.index(candidate_vitest, registry_offset)
    )
    assert 'if [[ -n "$published_commit" && "$published_commit" != "$GITHUB_SHA" ]]' in release
    assert "verify_pypi_candidates.py" in release
    assert 'published_integrity="$(npm view' in release
    assert "environment: npm" in release
    assert "environment: pypi" in release
    assert "(inputs.component == 'both' && needs.publish_npm.result == 'success')" in release
    assert release.count("needs.candidates.result == 'success'") == 2
    assert "always()" not in release
    assert release.count("!cancelled()") == 2
    assert release.count("id-token: write") == 2
    assert release.count("node-version: 24") == 7
    assert release.count("npm install --global npm@11.18.0") == 7
    for validation_job in (
        "validate_python",
        "validate_typescript",
        "validate_typescript_benchmark",
        "validate_typescript_examples",
        "validate_docs",
    ):
        assert f"  {validation_job}:" in release
        assert f"      - {validation_job}" in release
    assert "name: jaunt-release-benchmark" in release
    assert 'npm publish "$tarball" --access public --tag "$candidate_tag"' in release
    assert 'npm view "@usejaunt/ts@${candidate_tag}" version' in release
    assert 'test "$(npm view "@usejaunt/ts@${candidate_tag}" version)" = "$version"' in release
    assert "packages-dir: release/pypi-upload" in release
    assert "cp release/python/*.whl release/python/*.tar.gz release/pypi-upload/" in release
    assert "scripts/verify_github_release_assets.py" in release
    assert "release/python && sha256sum --check SHA256SUMS" in release
    assert "release/npm && sha256sum --check SHA256SUMS" in release
    assert "npm run benchmark:watch:ci" in release
    assert "JAUNT_TS_STRICT_BENCHMARK_ENABLED" in ci
    assert "runs-on: [self-hosted, linux, x64, jaunt-ts-performance]" in ci
    assert "node-version: 24.14.0" in ci
    assert "npm run benchmark:watch --" in ci
    assert 'if [[ "${{ inputs.component }}" == "python" ]]' in release
    assert "Refusing to finalize" not in release
    assert 'git config user.name "github-actions[bot]"' in release
    assert "NODE_AUTH_TOKEN" not in release
    assert "NPM_TOKEN" not in release
    assert "npm dist-tag add" not in release
    assert "promote_npm_latest" not in release
    stable_tags = "          - latest\n          - next\n          - beta\n        default: latest"
    assert stable_tags in release
    assert "--legacy-peer-deps" in release
    assert 'test "$typescript_before" =' in release
    assert 'local candidate_prefix="${project}.candidate"' in release
    assert 'mv "$candidate_prefix/node_modules/@usejaunt/ts"' in release
    assert "FIXME(exact-wheel-eject)" in release
    assert '"$jaunt_bin" eject ' not in release
    assert "FIXME(registry-runner)" in release
    assert '"$venv/bin/jaunt" eject ' not in release
