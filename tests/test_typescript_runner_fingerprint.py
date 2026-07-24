from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jaunt.typescript.tester import _RUNNER_REQUIRED_FILES, _runner_fingerprint
from jaunt.typescript.worker import TypeScriptWorkerError, WorkerInstallation


def _managed_client(root: Path, *, export: str = "./dist/test/runner.js") -> SimpleNamespace:
    package = root / "node_modules/@usejaunt/ts"
    for index, relative in enumerate(_RUNNER_REQUIRED_FILES):
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


@pytest.mark.parametrize(
    "relative",
    (
        "dist/test/native.node",
        "dist/test/runtime.wasm",
        "dist/test/extensionless-helper",
    ),
)
def test_runner_fingerprint_tracks_every_shipped_runtime_file(
    tmp_path: Path, relative: str
) -> None:
    client = _managed_client(tmp_path)
    runtime = client.installation.package_root / relative
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"runtime-v1")
    before = _runner_fingerprint(tmp_path, client, _initialized())

    runtime.write_bytes(b"runtime-v2")

    assert _runner_fingerprint(tmp_path, client, _initialized()) != before


def test_runner_fingerprint_tracks_full_manifest_semantics_not_formatting(tmp_path: Path) -> None:
    client = _managed_client(tmp_path)
    manifest_path = client.installation.package_root / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    before = _runner_fingerprint(tmp_path, client, _initialized())

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert _runner_fingerprint(tmp_path, client, _initialized()) == before

    manifest["jauntRuntime"] = {"mode": "strict"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert _runner_fingerprint(tmp_path, client, _initialized()) != before


def test_runner_fingerprint_tracks_same_version_compiler_runtime_bytes(tmp_path: Path) -> None:
    base = _managed_client(tmp_path)
    package = base.installation.package_root
    worker = package / "dist/worker.js"
    worker.write_text("export {};\n", encoding="utf-8")
    compiler_package = tmp_path / "node_modules/typescript"
    compiler = compiler_package / "lib/typescript.js"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("export const version = '6.0.2';\n", encoding="utf-8")
    declaration = compiler_package / "lib/lib.es2024.d.ts"
    declaration.write_text("interface Array<T> { readonly length: number; }\n", encoding="utf-8")
    (compiler_package / "package.json").write_text(
        json.dumps(
            {
                "name": "typescript",
                "version": "6.0.2",
                "main": "./lib/typescript.js",
            }
        ),
        encoding="utf-8",
    )
    client = SimpleNamespace(
        installation=WorkerInstallation(
            node="node",
            worker_entry=worker,
            compiler_module_path=compiler,
            package_root=package,
            tool_owner=tmp_path,
            package_managed=True,
        )
    )
    before = _runner_fingerprint(tmp_path, client, _initialized())

    declaration.write_text(
        "interface Array<T> { readonly length: number; at(index: number): T | undefined; }\n",
        encoding="utf-8",
    )

    assert _runner_fingerprint(tmp_path, client, _initialized()) != before


def test_runner_fingerprint_is_portable_for_identical_managed_runtime(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    installed_root = tmp_path / "installed"
    source = _managed_client(source_root)
    installed = _managed_client(installed_root)

    assert _runner_fingerprint(source_root, source, _initialized()) == _runner_fingerprint(
        installed_root, installed, _initialized()
    )


def test_runner_fingerprint_is_portable_through_a_pnpm_virtual_store_link(
    tmp_path: Path,
) -> None:
    npm_root = tmp_path / "npm"
    pnpm_root = tmp_path / "pnpm"
    npm = _managed_client(npm_root)
    pnpm = _managed_client(pnpm_root)

    logical_package = pnpm.installation.package_root
    virtual_package = (
        pnpm_root / "node_modules/.pnpm/@usejaunt+ts@file+..+package/node_modules/@usejaunt/ts"
    )
    virtual_package.parent.mkdir(parents=True)
    logical_package.rename(virtual_package)
    logical_package.symlink_to(virtual_package, target_is_directory=True)

    assert logical_package.resolve() == virtual_package.resolve()
    assert npm.installation.package_root.resolve() != virtual_package.resolve()
    assert _runner_fingerprint(npm_root, npm, _initialized()) == _runner_fingerprint(
        pnpm_root, pnpm, _initialized()
    )

    (virtual_package / "dist/test/runner.js").write_text(
        "export const changedRuntime = true;\n", encoding="utf-8"
    )
    assert _runner_fingerprint(npm_root, npm, _initialized()) != _runner_fingerprint(
        pnpm_root, pnpm, _initialized()
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
