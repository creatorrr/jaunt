from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jaunt.typescript.tester import _RUNNER_RUNTIME_FILES, _runner_fingerprint
from jaunt.typescript.worker import TypeScriptWorkerError


def _managed_client(root: Path, *, export: str = "./dist/test/runner.js") -> SimpleNamespace:
    package = root / "node_modules/@usejaunt/ts"
    for index, relative in enumerate(_RUNNER_RUNTIME_FILES):
        path = package / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"export const runtime{index} = {index};\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@usejaunt/ts",
                "version": "0.1.0-alpha.0",
                "exports": {"./test-runner": {"import": export, "default": export}},
            }
        ),
        encoding="utf-8",
    )
    return SimpleNamespace(
        installation=SimpleNamespace(
            package_root=package,
            package_managed=True,
            node=None,
            tool_owner=root,
        )
    )


def _initialized() -> SimpleNamespace:
    return SimpleNamespace(
        worker_version="0.1.0-alpha.0",
        typescript_version="6.0.2",
    )


def test_runner_fingerprint_tracks_exact_held_out_guard_bytes(tmp_path: Path) -> None:
    client = _managed_client(tmp_path)
    before = _runner_fingerprint(tmp_path, client, _initialized())

    heldout = client.installation.package_root / "dist/test/heldout.js"
    heldout.write_text("export const hardenedGuard = true;\n", encoding="utf-8")

    assert _runner_fingerprint(tmp_path, client, _initialized()) != before


def test_runner_fingerprint_is_portable_for_identical_managed_runtime(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    installed_root = tmp_path / "installed"
    source = _managed_client(source_root)
    installed = _managed_client(installed_root)

    assert _runner_fingerprint(source_root, source, _initialized()) == _runner_fingerprint(
        installed_root, installed, _initialized()
    )


def test_runner_fingerprint_fails_closed_when_held_out_guard_is_missing(tmp_path: Path) -> None:
    client = _managed_client(tmp_path)
    (client.installation.package_root / "dist/test/heldout.js").unlink()

    with pytest.raises(TypeScriptWorkerError, match="heldout\\.js"):
        _runner_fingerprint(tmp_path, client, _initialized())


def test_runner_fingerprint_fails_closed_on_inconsistent_runner_export(tmp_path: Path) -> None:
    client = _managed_client(tmp_path, export="./dist/test/not-the-runner.js")

    with pytest.raises(TypeScriptWorkerError, match="inconsistent './test-runner' export"):
        _runner_fingerprint(tmp_path, client, _initialized())
