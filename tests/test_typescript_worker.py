from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

import pytest

from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import PROTOCOL_VERSION, InitializeParams
from jaunt.typescript.worker import (
    REQUIRED_WORKER_CAPABILITIES,
    WorkerClient,
    WorkerCrashedError,
    WorkerInstallation,
    WorkerProtocolError,
    WorkerRemoteError,
    WorkerTimeoutError,
    TypeScriptWorkerError,
    resolve_worker_installation,
    worker_generation_fingerprint,
    worker_environment,
    worker_runtime_identity,
)


def _installation(tmp_path: Path, source: str) -> WorkerInstallation:
    script = tmp_path / "worker.py"
    script.write_text(source, encoding="utf-8")
    compiler = tmp_path / "typescript.js"
    compiler.write_text("", encoding="utf-8")
    return WorkerInstallation(
        node=sys.executable,
        worker_entry=script,
        compiler_module_path=compiler,
        package_root=tmp_path,
        tool_owner=tmp_path,
    )


def _echo_worker() -> str:
    return f'''\
import json
import sys

STAMP = {{"sessionId": "s", "epoch": 1, "snapshot": "snap", "inputHashes": {{}}}}
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        result = {{
            "workerVersion": "0.1",
            "protocol": "{PROTOCOL_VERSION}",
            "typescriptVersion": "5.9.0",
            "capabilities": {list(REQUIRED_WORKER_CAPABILITIES)!r},
            **STAMP,
            "snapshot": request["params"].get("generationFingerprint", "missing"),
        }}
    elif method == "fail":
        response = {{
            "protocol": "{PROTOCOL_VERSION}",
            "id": request["id"],
            "ok": False,
            "error": {{
                "code": "FAIL",
                "message": "requested failure",
                "retryable": False,
                "diagnostics": [],
            }},
        }}
        print(json.dumps(response), flush=True)
        continue
    else:
        result = {{"method": method, "value": request["params"].get("value")}}
    response = {{
        "protocol": "{PROTOCOL_VERSION}",
        "id": request["id"],
        "ok": True,
        "result": result,
    }}
    print(json.dumps(response), flush=True)
    if method == "shutdown":
        break
'''


def _initialize_params(tmp_path: Path) -> InitializeParams:
    return InitializeParams(
        root=str(tmp_path),
        projects=("tsconfig.json",),
        test_projects=(),
        source_roots=("src",),
        test_roots=(),
        generated_dir="__generated__",
        tool_owner=".",
        compiler_module_path=str(tmp_path / "typescript.js"),
        client_version="1",
        tool_version="1",
        generation_fingerprint="sha256:base-generation",
    )


def test_worker_client_handshake_concurrent_requests_and_remote_error(tmp_path: Path) -> None:
    async def run() -> None:
        installation = _installation(tmp_path, _echo_worker())
        expected = worker_generation_fingerprint(
            "sha256:base-generation",
            worker_runtime_identity(installation),
        )
        async with WorkerClient(
            root=tmp_path,
            installation=installation,
        ) as client:
            initialized = await client.initialize(_initialize_params(tmp_path))
            assert initialized.stamp.session_id == "s"
            assert initialized.stamp.snapshot == expected
            first, second = await asyncio.gather(
                client.request("echo", {"value": 1}),
                client.request("echo", {"value": 2}),
            )
            assert {first["value"], second["value"]} == {1, 2}
            with pytest.raises(WorkerRemoteError, match="requested failure"):
                await client.request("fail", {})

    asyncio.run(run())


def test_arbitrary_worker_override_bytes_change_generation_identity(tmp_path: Path) -> None:
    installation = _installation(tmp_path, "console.log('first');\n")
    first_identity = worker_runtime_identity(installation)
    first_generation = worker_generation_fingerprint("sha256:base", first_identity)

    installation.worker_entry.write_text("console.log('second');\n", encoding="utf-8")
    second_identity = worker_runtime_identity(installation)
    second_generation = worker_generation_fingerprint("sha256:base", second_identity)

    assert second_identity != first_identity
    assert second_generation != first_generation


def test_packaged_worker_identity_is_portable_and_scoped_to_runtime(tmp_path: Path) -> None:
    def installation_at(root: Path) -> WorkerInstallation:
        package = root / "node_modules/@usejaunt/ts"
        files = {
            "dist/worker/main.js": "import '../analyzer/core.js';\n",
            "dist/analyzer/core.js": "export const worker = 1;\n",
            "dist/protocol/messages.js": "export const protocol = 1;\n",
            "dist/schema/protocol.json": '{"version": 1}\n',
            "dist/test/runner.js": "export const runner = 1;\n",
            "dist/analyzer/core.d.ts": "export declare const worker = 1;\n",
        }
        for relative, content in files.items():
            path = package / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@usejaunt/ts",
                    "version": "0.1.0-alpha.0",
                    "exports": {"./worker": "./dist/worker/main.js"},
                }
            ),
            encoding="utf-8",
        )
        compiler = root / "typescript.js"
        compiler.write_text("", encoding="utf-8")
        return WorkerInstallation(
            node=sys.executable,
            worker_entry=package / "dist/worker/main.js",
            compiler_module_path=compiler,
            package_root=package,
            tool_owner=root,
            package_managed=True,
        )

    source = installation_at(tmp_path / "source")
    installed = installation_at(tmp_path / "installed")

    expected = worker_runtime_identity(source)
    assert worker_runtime_identity(installed) == expected

    (source.package_root / "dist/test/runner.js").write_text(
        "export const runner = 2;\n", encoding="utf-8"
    )
    assert worker_runtime_identity(source) == expected

    (source.package_root / "dist/analyzer/core.js").write_text(
        "export const worker = 2;\n", encoding="utf-8"
    )
    assert worker_runtime_identity(source) != expected


def test_packaged_worker_identity_fails_closed_without_runtime_tree(tmp_path: Path) -> None:
    package = tmp_path / "node_modules/@usejaunt/ts"
    package.mkdir(parents=True)
    worker = package / "worker.js"
    worker.write_text("export {};\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@usejaunt/ts", "version": "0.1.0-alpha.0"}),
        encoding="utf-8",
    )
    installation = WorkerInstallation(
        node=sys.executable,
        worker_entry=worker,
        compiler_module_path=tmp_path / "typescript.js",
        package_root=package,
        tool_owner=tmp_path,
        package_managed=True,
    )

    with pytest.raises(TypeScriptWorkerError, match="no runtime directory"):
        worker_runtime_identity(installation)


def test_worker_client_rejects_missing_required_capabilities_at_handshake(
    tmp_path: Path,
) -> None:
    source = _echo_worker().replace(
        repr(list(REQUIRED_WORKER_CAPABILITIES)), repr(["analyze", "overlay"])
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=_installation(tmp_path, source))
        with pytest.raises(WorkerProtocolError, match="contract-projection"):
            await client.initialize(_initialize_params(tmp_path))
        await client.close()

    asyncio.run(run())


def test_worker_client_rejects_malformed_stdout(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys\nfor _line in sys.stdin:\n print('not json', flush=True)\n break\n",
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        with pytest.raises(WorkerProtocolError, match="Malformed"):
            await client.request("echo", {})
        await client.close()

    asyncio.run(run())


def test_worker_client_rejects_protocol_mismatch(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        """\
import json
import sys
for line in sys.stdin:
 request = json.loads(line)
 response = {"protocol": "jaunt-ts/1-draft.1", "id": request["id"], "ok": True, "result": {}}
 print(json.dumps(response), flush=True)
 break
""",
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        with pytest.raises(WorkerProtocolError, match="protocol mismatch"):
            await client.request("analyzeWorkspace", {})
        await client.close()

    asyncio.run(run())


def test_worker_client_rejects_oversized_stdout(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys\nfor _line in sys.stdin:\n print('x' * 512, flush=True)\n break\n",
    )

    async def run() -> None:
        client = WorkerClient(
            root=tmp_path,
            installation=installation,
            max_message_bytes=128,
        )
        with pytest.raises(WorkerProtocolError, match="exceeds 128 bytes"):
            await client.request("analyzeWorkspace", {})
        await client.close()

    asyncio.run(run())


def test_worker_client_times_out_and_kills_process_group(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys, time\n"
        "for _line in sys.stdin:\n"
        " phase = '[jaunt:phase] method=validateOverlay '\n"
        " sys.stderr.write(phase + 'phase=module-overlays state=start elapsed_ms=7\\n')\n"
        " sys.stderr.flush()\n"
        " time.sleep(60)\n",
    )

    async def run() -> None:
        client = WorkerClient(
            root=tmp_path,
            installation=installation,
            request_timeout=0.05,
        )
        with pytest.raises(WorkerTimeoutError, match="worker_timeout_seconds") as raised:
            await client.request("hang", {})
        assert "phase=module-overlays" in str(raised.value)
        await client.close()

    asyncio.run(run())


def test_worker_client_initialization_timeout_names_startup_setting(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys, time\nfor _line in sys.stdin:\n time.sleep(60)\n",
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        with pytest.raises(WorkerTimeoutError, match="worker_startup_timeout_seconds"):
            await client.request("initialize", {}, timeout=0.05)
        await client.close()

    asyncio.run(run())


def test_worker_client_caller_cancellation_terminates_worker(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys, time\nfor _line in sys.stdin:\n time.sleep(60)\n",
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        task = asyncio.create_task(client.request("analyzeWorkspace", {}))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await client.close()

    asyncio.run(run())


def test_worker_client_reports_crash_with_bounded_stderr(tmp_path: Path) -> None:
    installation = _installation(
        tmp_path,
        "import sys\nsys.stderr.write('x' * 10000)\nsys.stderr.flush()\nsys.exit(7)\n",
    )

    async def run() -> None:
        client = WorkerClient(
            root=tmp_path,
            installation=installation,
            stderr_limit=128,
        )
        with pytest.raises(WorkerCrashedError, match="exit code 7"):
            await client.request("crash", {})
        assert 0 < len(client.stderr) <= 128
        await client.close()

    asyncio.run(run())


def test_worker_client_restarts_and_replays_one_read_only_request_after_crash(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "crashed-once"
    installation = _installation(
        tmp_path,
        f'''\
import json
import os
import pathlib
import sys

marker = pathlib.Path({str(marker)!r})
stamp = {{"sessionId": "s", "epoch": 0, "snapshot": "same", "inputHashes": {{}}}}
for line in sys.stdin:
    request = json.loads(line)
    method = request["method"]
    if method == "initialize":
        result = {{
            "workerVersion": "0.1",
            "protocol": "{PROTOCOL_VERSION}",
            "typescriptVersion": "6.0.2",
            "capabilities": {list(REQUIRED_WORKER_CAPABILITIES)!r},
            **stamp,
        }}
    elif method == "analyzeWorkspace" and not marker.exists():
        marker.write_text("crashed")
        os._exit(17)
    else:
        result = {{"method": method, **stamp}}
    print(json.dumps({{
        "protocol": "{PROTOCOL_VERSION}",
        "id": request["id"],
        "ok": True,
        "result": result,
    }}), flush=True)
    if method == "shutdown":
        break
''',
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        await client.initialize(_initialize_params(tmp_path))
        result = await client.request("analyzeWorkspace", {})
        assert result["method"] == "analyzeWorkspace"
        assert marker.read_text() == "crashed"
        await client.close()

    asyncio.run(run())


def test_project_local_installation_resolution_and_direct_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner = tmp_path / "tools"
    owner.mkdir()
    (owner / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "@usejaunt/ts": "0.1.0",
                    "typescript": "5.9.0",
                }
            }
        ),
        encoding="utf-8",
    )
    package = tmp_path / "node_modules/@usejaunt/ts"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "@usejaunt/ts",
                "version": "0.1.0",
                "exports": {"./worker": {"import": "./dist/worker.js"}},
            }
        ),
        encoding="utf-8",
    )
    (package / "dist").mkdir()
    (package / "dist/worker.js").write_text("", encoding="utf-8")
    compiler = tmp_path / "node_modules/typescript/lib/typescript.js"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("", encoding="utf-8")
    (compiler.parent.parent / "package.json").write_text(
        json.dumps({"name": "typescript", "version": "5.9.0"}), encoding="utf-8"
    )
    monkeypatch.setattr(
        "jaunt.typescript.worker.shutil.which", lambda *_args, **_kwargs: "/bin/node"
    )

    target = TypeScriptTargetConfig(
        source_roots=["src"],
        test_roots=[],
        projects=["tsconfig.json"],
        tool_owner="tools",
    )
    installation = resolve_worker_installation(tmp_path, target, environ={"PATH": "/bin"})

    assert installation.worker_entry == (package / "dist/worker.js").resolve()
    assert installation.compiler_module_path == compiler.resolve()
    assert installation.tool_owner == owner.resolve()
    assert installation.package_managed is True


def test_pnpm_style_tooling_symlinks_may_resolve_to_an_external_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = tmp_path.parent / f"{tmp_path.name}-pnpm-store"
    try:
        compiler_package = store / "typescript"
        compiler = compiler_package / "lib/typescript.js"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("", encoding="utf-8")
        (compiler_package / "package.json").write_text(
            json.dumps({"name": "typescript", "version": "6.0.2"}), encoding="utf-8"
        )
        worker_package = store / "jaunt-ts"
        (worker_package / "dist").mkdir(parents=True)
        (worker_package / "dist/worker.js").write_text("", encoding="utf-8")
        (worker_package / "package.json").write_text(
            json.dumps(
                {
                    "name": "@usejaunt/ts",
                    "version": "0.1.0-alpha.0",
                    "exports": {"./worker": {"import": "./dist/worker.js"}},
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "package.json").write_text(
            json.dumps(
                {
                    "devDependencies": {
                        "@usejaunt/ts": "0.1.0-alpha.0",
                        "typescript": "6.0.2",
                    }
                }
            ),
            encoding="utf-8",
        )
        (tmp_path / "node_modules/@usejaunt").mkdir(parents=True)
        (tmp_path / "node_modules/typescript").symlink_to(
            compiler_package, target_is_directory=True
        )
        (tmp_path / "node_modules/@usejaunt/ts").symlink_to(
            worker_package, target_is_directory=True
        )
        monkeypatch.setattr(
            "jaunt.typescript.worker.shutil.which", lambda *_args, **_kwargs: "/bin/node"
        )

        installation = resolve_worker_installation(
            tmp_path,
            TypeScriptTargetConfig(
                source_roots=["src"],
                test_roots=[],
                projects=["tsconfig.json"],
            ),
            environ={"PATH": "/bin"},
        )

        assert installation.compiler_module_path == (
            tmp_path / "node_modules/typescript/lib/typescript.js"
        )
        assert installation.worker_entry == (tmp_path / "node_modules/@usejaunt/ts/dist/worker.js")
        assert installation.compiler_module_path.resolve() == compiler.resolve()
        assert installation.worker_entry.resolve() == (worker_package / "dist/worker.js").resolve()
    finally:
        shutil.rmtree(store, ignore_errors=True)


def test_worker_override_retains_the_owning_package_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "devDependencies": {
                    "@usejaunt/ts": "0.1.0-alpha.0",
                    "typescript": "5.9.0",
                }
            }
        ),
        encoding="utf-8",
    )
    compiler = tmp_path / "node_modules/typescript/lib/typescript.js"
    compiler.parent.mkdir(parents=True)
    compiler.write_text("", encoding="utf-8")
    (compiler.parent.parent / "package.json").write_text(
        json.dumps({"name": "typescript", "version": "5.9.0"}), encoding="utf-8"
    )
    package = tmp_path / "tooling/jaunt-ts"
    worker = package / "dist/worker/main.js"
    worker.parent.mkdir(parents=True)
    worker.write_text("", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@usejaunt/ts", "version": "0.1.0-alpha.0"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "jaunt.typescript.worker.shutil.which", lambda *_args, **_kwargs: "/bin/node"
    )

    installation = resolve_worker_installation(
        tmp_path,
        TypeScriptTargetConfig(
            source_roots=["src"],
            test_roots=[],
            projects=["tsconfig.json"],
        ),
        environ={"PATH": "/bin", "JAUNT_TS_WORKER": str(worker)},
    )

    assert installation.worker_entry == worker.resolve()
    assert installation.package_root == package.resolve()
    assert installation.package_managed is True


def test_worker_tooling_must_be_direct_dev_dependencies(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "dependencies": {
                    "@usejaunt/ts": "0.1.0",
                    "typescript": "5.9.0",
                }
            }
        ),
        encoding="utf-8",
    )
    target = TypeScriptTargetConfig(
        source_roots=["src"],
        test_roots=[],
        projects=["tsconfig.json"],
    )

    with pytest.raises(TypeScriptWorkerError, match="directly declare devDependencies"):
        resolve_worker_installation(tmp_path, target, environ={"PATH": "/bin"})


def test_worker_tool_owner_cannot_escape_project(tmp_path: Path) -> None:
    target = TypeScriptTargetConfig(
        source_roots=["src"],
        test_roots=[],
        projects=["tsconfig.json"],
        tool_owner="..",
    )
    with pytest.raises(TypeScriptWorkerError, match="escapes the project root"):
        resolve_worker_installation(tmp_path, target, environ={"PATH": "/bin"})


def test_worker_environment_drops_node_injection_variables() -> None:
    env = worker_environment(
        {
            "PATH": "/bin",
            "HOME": "/home/user",
            "NODE_OPTIONS": "--require evil.js",
            "NODE_PATH": "/evil",
            "TS_NODE_PROJECT": "/evil/tsconfig.json",
            "SECRET": "do-not-forward",
        }
    )
    assert env["PATH"] == "/bin"
    assert env["JAUNT_TS_PROTOCOL"] == PROTOCOL_VERSION
    assert env["JAUNT_TS_PHASE_TELEMETRY"] == "1"
    assert "NODE_OPTIONS" not in env
    assert "NODE_PATH" not in env
    assert "TS_NODE_PROJECT" not in env
    assert "SECRET" not in env
