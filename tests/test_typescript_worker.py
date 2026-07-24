from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from collections.abc import Mapping
from pathlib import Path

import pytest

from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import PROTOCOL_VERSION, InitializeParams
from jaunt.typescript.worker import (
    _runtime_module_specifiers,
    _runtime_package_resolution_closure,
    REQUIRED_WORKER_CAPABILITIES,
    WorkerClient,
    WorkerCrashedError,
    WorkerInstallation,
    WorkerOutOfMemoryError,
    WorkerProtocolError,
    WorkerRemoteError,
    WorkerToolchainChangedError,
    WorkerTimeoutError,
    TypeScriptWorkerError,
    resolve_worker_installation,
    runtime_package_identity,
    toolchain_session_identity,
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


@pytest.mark.parametrize(
    "source",
    [
        'import "hoisted-runtime-helper";',
        'void import("hoisted-runtime-helper");',
        'module.exports = require("hoisted-runtime-helper");',
        'const helper = require.resolve("hoisted-runtime-helper");',
    ],
)
def test_runtime_package_scanner_captures_static_native_load_forms(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "index.js") == (
        "hoisted-runtime-helper",
    )


def test_runtime_package_scanner_handles_regex_inside_template_expression(
    tmp_path: Path,
) -> None:
    source = (
        'const printed = `"${value.replaceAll(/"|\\\\/g, "\\\\$&")}"`;\n'
        'module.exports = require("hoisted-runtime-helper");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "pretty-format.js") == (
        "hoisted-runtime-helper",
    )


@pytest.mark.parametrize(
    "prefix",
    [
        'const value = new /ignored import("missing-import") '
        'require("missing-require")/.constructor("actual");',
        'export default /ignored import("missing-import") require("missing-require")/;',
        'class Runner extends /ignored import("missing-import") '
        'require("missing-require")/.constructor {}',
        'const rendered = `${new /ignored import("missing-import") '
        'require("missing-require")/.constructor("actual")}`;',
    ],
)
def test_runtime_package_scanner_ignores_regex_after_expression_prefix_keyword(
    tmp_path: Path,
    prefix: str,
) -> None:
    source = f'{prefix}\nimport("real-import");\n'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-import",
    )


@pytest.mark.parametrize("member", ["target.new", "target?.default", "target.extends"])
def test_runtime_package_scanner_keeps_division_after_keyword_named_member(
    tmp_path: Path,
    member: str,
) -> None:
    source = f'const ratio = {member} / require("real-divisor") / divisor;\n'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-divisor",
    )


@pytest.mark.parametrize(
    "control_head",
    [
        "if (ok)",
        "while (ok)",
        "for (; ok; step())",
        "switch (value)",
        "catch (error)",
    ],
)
def test_runtime_package_scanner_ignores_regex_after_control_flow_head(
    tmp_path: Path,
    control_head: str,
) -> None:
    source = (
        f'{control_head}/import("missing-import") require("missing-require")/.test(value);\n'
        'import("real-import");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-import",
    )


def test_runtime_package_scanner_tracks_control_flow_regex_inside_template_expression(
    tmp_path: Path,
) -> None:
    source = (
        'const rendered = `${(() => { if (ok)/} import("missing-template")/.test(value); '
        'return import("real-template"); })()}`;\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-template",
    )


@pytest.mark.parametrize(
    "statement",
    [
        "{ run(); }",
        "label: { run(); }",
        "if (ok) { run(); }",
        "if (ok) { run(); } else { recover(); }",
        "switch (value) { default: run(); }",
        "try { run(); } catch { recover(); }",
        "try { run(); } catch (error) { recover(error); } finally { finish(); }",
        "do { run(); } while (again);",
        "function run() {}",
        "function run({ value } = {}) {}",
        "class Runner {}",
        "class Runner extends mixin({}) {}",
    ],
)
def test_runtime_package_scanner_ignores_regex_after_statement_brace(
    tmp_path: Path,
    statement: str,
) -> None:
    source = (
        f'{statement} /import("missing-import") require("missing-require")/.test(value);\n'
        'import("real-import");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-import",
    )


def test_runtime_package_scanner_tracks_statement_brace_regex_inside_template_expression(
    tmp_path: Path,
) -> None:
    source = (
        'const rendered = `${(() => { if (ok) {} /} import("missing-template")/.test(value); '
        'return import("real-template"); })()}`;\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-template",
    )


@pytest.mark.parametrize(
    "source",
    [
        (
            'function outer() { {} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        (
            'switch (value) { case 1: {} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        (
            'function stopped() { return\n{} /import("missing-import") '
            'require("missing-require")/.test(value); }'
        ),
        'const value = 1\n{} /import("missing-import") require("missing-require")/.test(value);',
    ],
)
def test_runtime_package_scanner_ignores_regex_after_nested_or_asi_block(
    tmp_path: Path,
    source: str,
) -> None:
    source += '\nimport("real-import");\n'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-import",
    )


def test_runtime_package_scanner_tracks_nested_block_regex_inside_template_expression(
    tmp_path: Path,
) -> None:
    source = (
        'const rendered = `${(() => { {} /} import("missing-template")/.test(value); '
        'return import("real-template"); })()}`;\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-template",
    )


@pytest.mark.parametrize(
    "statement",
    ["label: function run() {}", "if (ok) function run() {}"],
)
def test_runtime_package_scanner_ignores_regex_after_annex_b_function_declaration(
    tmp_path: Path,
    statement: str,
) -> None:
    source = (
        f'{statement} /import("missing-import") require("missing-require")/.test(value);\n'
        'require("real-require");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (
        "real-require",
    )


@pytest.mark.parametrize("expression", ["{}", "function () {}", "class {}", "(() => {})"])
def test_runtime_package_scanner_keeps_division_after_braced_expression_executable(
    tmp_path: Path,
    expression: str,
) -> None:
    source = f'const ratio = {expression} / require("real-divisor");\n'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-divisor",
    )


@pytest.mark.parametrize(
    "source",
    [
        'const value = { nested: {} / require("real-divisor") };',
        'const value = class extends (class {}) {} / require("real-divisor");',
        '({ value } = {}) / require("real-divisor");',
        'function ratio() { return {} / require("real-divisor"); }',
    ],
)
def test_runtime_package_scanner_preserves_nested_braced_expression_division(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-divisor",
    )


def test_runtime_package_scanner_keeps_division_adjacent_loads_executable(
    tmp_path: Path,
) -> None:
    source = (
        "const ratio = (left + right) / divisor;\n"
        'import("real-import");\n'
        'const adjusted = ratio / require("real-require");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-import",
        "real-require",
    )


@pytest.mark.parametrize(
    "source",
    [
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        'load("hoisted-runtime-helper");',
        'import { createRequire as makeRequire } from "module";\n'
        "const factory = makeRequire;\n"
        "const load = factory(import.meta.url);\n"
        "const alias = load;\n"
        'alias.resolve("hoisted-runtime-helper");',
        'import * as nodeModule from "node:module";\n'
        'nodeModule.createRequire(import.meta.url)("hoisted-runtime-helper");',
        'import nodeModule from "node:module";\n'
        "const load = nodeModule.createRequire(import.meta.url);\n"
        'load("hoisted-runtime-helper");',
        'const { createRequire: makeRequire } = require("node:module");\n'
        "const load = makeRequire(__filename);\n"
        'load("hoisted-runtime-helper");',
        'const nodeModule = require("module");\n'
        "const load = nodeModule.createRequire(__filename);\n"
        'load.resolve("hoisted-runtime-helper");',
        'import { Module } from "node:module";\n'
        'Module.createRequire(import.meta.url)("hoisted-runtime-helper");',
        'const { Module } = require("node:module");\n'
        'Module.createRequire(__filename)("hoisted-runtime-helper");',
        'const { createRequire } = await import("node:module");\n'
        'createRequire(import.meta.url)("hoisted-runtime-helper");',
        'const nodeModule = await import("node:module");\n'
        "const load = nodeModule.createRequire(import.meta.url);\n"
        'load("hoisted-runtime-helper");',
        'require("node:module").createRequire(__filename)("hoisted-runtime-helper");',
        '(await import("node:module")).createRequire(import.meta.url)("hoisted-runtime-helper");',
    ],
)
def test_runtime_package_scanner_tracks_proven_create_require_forms(
    tmp_path: Path,
    source: str,
) -> None:
    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" in specifiers


@pytest.mark.parametrize(
    "source",
    [
        "function createRequire() { return () => null; }\n"
        "const load = createRequire();\n"
        'load("hoisted-runtime-helper");',
        'import { createRequire } from "not-node-module";\n'
        "const load = createRequire(import.meta.url);\n"
        'load("hoisted-runtime-helper");',
        'import * as nodeModule from "not-node-module";\n'
        "const load = nodeModule.createRequire(import.meta.url);\n"
        'load("hoisted-runtime-helper");',
    ],
)
def test_runtime_package_scanner_does_not_trust_create_require_by_name(
    tmp_path: Path,
    source: str,
) -> None:
    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" not in specifiers


@pytest.mark.parametrize(
    "use",
    [
        "consume(load);",
        "load = replacement;",
        'load["resolve"]("hoisted-runtime-helper");',
        "function leakLoader() { return load; }",
    ],
)
def test_runtime_package_scanner_rejects_ambiguous_proven_loader_uses(
    tmp_path: Path,
    use: str,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "let load = createRequire(import.meta.url);\n"
        f"{use}\n"
    )

    with pytest.raises(TypeScriptWorkerError, match="module-loading|loader alias|specifier"):
        _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")


def test_runtime_package_scanner_allows_opaque_calls_but_keeps_static_siblings(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        "load(packageName);\n"
        "load.resolve(dependency, { paths: [root] });\n"
        'load("hoisted-runtime-helper");\n'
    )

    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" in specifiers


def test_runtime_package_scanner_allows_bundled_loader_export_and_forwarding(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const require = createRequire(import.meta.url);\n"
        "const bundled = ((fallback) => typeof require < 'u' ? require : "
        "typeof Proxy < 'u' ? new Proxy(fallback, { get: (_target, name) => "
        "(typeof require < 'u' ? require : fallback)[name] }) : fallback)"
        "(function (...args) { return require.apply(this, args); });\n"
        'bundled("hoisted-runtime-helper");\n'
        "bundled(runtimeSelectedPackage);\n"
        'require.call(null, "forwarded-runtime-helper");\n'
        "require.apply(null, runtimeArguments);\n"
        "export { require as bundledRequire };\n"
    )

    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" in specifiers
    assert "forwarded-runtime-helper" in specifiers
    assert "runtimeSelectedPackage" not in specifiers


def test_runtime_package_scanner_allows_factory_loader_in_runtime_options(
    tmp_path: Path,
) -> None:
    source = (
        'import * as nodeModule from "node:module";\n'
        "const options = { require: nodeModule.createRequire(runtimeHref) };\n"
        'nodeModule.createRequire(import.meta.url)("hoisted-runtime-helper");\n'
    )

    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" in specifiers


def test_runtime_package_scanner_allows_standard_loader_runtime_plumbing(
    tmp_path: Path,
) -> None:
    source = (
        'import nodeModule, { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        'load.extensions[".css"] = () => {};\n'
        "load.resolve.paths;\n"
        "let cached;\n"
        "cached ??= nodeModule.createRequire(import.meta.url);\n"
        "cache.set(this, cached);\n"
        'cache.get(this)("cached-runtime-helper");\n'
        "class Wrapper {\n"
        "  get require() { return cached; }\n"
        "  createRequire(url) { return this.other(url); }\n"
        "}\n"
    )

    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js")

    assert "node:module" in specifiers
    assert "cached-runtime-helper" in specifiers


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


def test_worker_client_rechecks_runtime_identity_on_clean_session_exit(tmp_path: Path) -> None:
    async def run() -> None:
        source = _echo_worker()
        installation = _installation(tmp_path, source)
        client = WorkerClient(root=tmp_path, installation=installation)

        with pytest.raises(
            WorkerToolchainChangedError,
            match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
        ):
            async with client:
                await client.initialize(_initialize_params(tmp_path))
                installation.worker_entry.write_text(
                    source + "\n# rebuilt while the session was active\n",
                    encoding="utf-8",
                )

    asyncio.run(run())


def test_worker_client_allows_identical_runtime_bytes_on_clean_session_exit(
    tmp_path: Path,
) -> None:
    async def run() -> None:
        source = _echo_worker()
        installation = _installation(tmp_path, source)
        async with WorkerClient(root=tmp_path, installation=installation) as client:
            await client.initialize(_initialize_params(tmp_path))
            installation.worker_entry.write_text(source, encoding="utf-8")

    asyncio.run(run())


def test_worker_client_sealed_identity_skips_late_clean_exit_recheck(tmp_path: Path) -> None:
    async def run() -> None:
        source = _echo_worker()
        installation = _installation(tmp_path, source)
        async with WorkerClient(root=tmp_path, installation=installation) as client:
            await client.initialize(_initialize_params(tmp_path))
            client.seal_runtime_identity()
            installation.worker_entry.write_text(
                source + "\n# rebuilt after the final commit boundary\n",
                encoding="utf-8",
            )

    asyncio.run(run())


def test_worker_client_later_request_reopens_clean_exit_identity_check(tmp_path: Path) -> None:
    async def run() -> None:
        source = _echo_worker()
        installation = _installation(tmp_path, source)
        client = WorkerClient(root=tmp_path, installation=installation)

        with pytest.raises(
            WorkerToolchainChangedError,
            match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
        ):
            async with client:
                await client.initialize(_initialize_params(tmp_path))
                client.seal_runtime_identity()
                await client.request("echo", {"value": 1})
                installation.worker_entry.write_text(
                    source + "\n# rebuilt after a later request\n",
                    encoding="utf-8",
                )

    asyncio.run(run())


def test_worker_client_preserves_body_error_when_runtime_changes_on_exceptional_exit(
    tmp_path: Path,
) -> None:
    class BodyError(RuntimeError):
        pass

    async def run() -> None:
        source = _echo_worker()
        installation = _installation(tmp_path, source)
        client = WorkerClient(root=tmp_path, installation=installation)

        with pytest.raises(BodyError, match="operation failed"):
            async with client:
                await client.initialize(_initialize_params(tmp_path))
                installation.worker_entry.write_text(
                    source + "\n# rebuilt while unwinding the operation\n",
                    encoding="utf-8",
                )
                raise BodyError("operation failed")

    asyncio.run(run())


def test_packaged_worker_identity_is_portable_and_scoped_to_runtime(tmp_path: Path) -> None:
    def installation_at(root: Path) -> WorkerInstallation:
        package = root / "node_modules/@usejaunt/ts"
        files = {
            "dist/worker/main.js": "import '../analyzer/core.js';\n",
            "dist/analyzer/core.js": "export const worker = 1;\n",
            "dist/analyzer/helper": "#!/usr/bin/env node\n",
            "dist/analyzer/kernel.wasm": "wasm-runtime-v1\n",
            "dist/analyzer/native.node": "native-runtime-v1\n",
            "dist/protocol/messages.js": "export const protocol = 1;\n",
            "dist/schema/protocol.json": '{"version": 1}\n',
            "dist/test/runner.js": "export const runner = 1;\n",
            "dist/test/runner-addon.node": "test-native-v1\n",
            "dist/analyzer/core.d.ts": "export declare const worker = 1;\n",
            "dist/spec.js": "export {};\n",
            "dist/spec.d.cts": "export declare function magic(): never;\n",
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
                    "exports": {
                        "./worker": "./dist/worker/main.js",
                        "./spec": {
                            "types": "./dist/spec.d.cts",
                            "default": "./dist/spec.js",
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        compiler_package = root / "node_modules/typescript"
        compiler = compiler_package / "lib/typescript.js"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("export const version = '6.0.2';\n", encoding="utf-8")
        (compiler_package / "lib/lib.es2024.d.ts").write_text(
            "interface Array<T> { readonly length: number; }\n",
            encoding="utf-8",
        )
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

    full_expected = worker_runtime_identity(installed, include_test=True)
    test_native = installed.package_root / "dist/test/runner-addon.node"
    full_session = toolchain_session_identity(installed, include_test=True)
    test_native.write_text("test-native-v2\n", encoding="utf-8")
    assert worker_runtime_identity(installed) == expected
    assert worker_runtime_identity(installed, include_test=True) != full_expected
    assert toolchain_session_identity(installed, include_test=True) != full_session
    test_native.write_text("test-native-v1\n", encoding="utf-8")
    assert worker_runtime_identity(installed, include_test=True) == full_expected

    for relative in (
        "dist/analyzer/helper",
        "dist/analyzer/kernel.wasm",
        "dist/analyzer/native.node",
    ):
        runtime = source.package_root / relative
        original = runtime.read_bytes()
        session_expected = toolchain_session_identity(source, include_test=False)
        runtime.write_bytes(original + b"changed")
        assert worker_runtime_identity(source) != expected
        assert toolchain_session_identity(source, include_test=False) != session_expected
        runtime.write_bytes(original)
        assert worker_runtime_identity(source) == expected

    compiler_declaration = source.compiler_module_path.parent / "lib.es2024.d.ts"
    compiler_declaration.write_text(
        "interface Array<T> { readonly length: number; at(index: number): T | undefined; }\n"
    )
    assert worker_runtime_identity(source) != expected
    compiler_declaration.write_text("interface Array<T> { readonly length: number; }\n")
    assert worker_runtime_identity(source) == expected

    compiler_manifest = source.compiler_module_path.parent.parent / "package.json"
    compiler_payload = json.loads(compiler_manifest.read_text(encoding="utf-8"))
    compiler_payload["main"] = "./lib/typescript.next.js"
    compiler_manifest.write_text(json.dumps(compiler_payload), encoding="utf-8")
    assert worker_runtime_identity(source) != expected
    compiler_payload["main"] = "./lib/typescript.js"
    compiler_manifest.write_text(json.dumps(compiler_payload), encoding="utf-8")
    assert worker_runtime_identity(source) == expected

    (source.package_root / "dist/analyzer/core.js").write_text(
        "export const worker = 2;\n", encoding="utf-8"
    )
    assert worker_runtime_identity(source) != expected
    (source.package_root / "dist/analyzer/core.js").write_text(
        "export const worker = 1;\n", encoding="utf-8"
    )
    assert worker_runtime_identity(source) == expected

    declaration = source.package_root / "dist/spec.d.cts"
    declaration.write_text("export declare function magic(value: string): never;\n")
    assert worker_runtime_identity(source) != expected
    declaration.write_text("export declare function magic(): never;\n")
    assert worker_runtime_identity(source) == expected
    declaration.unlink()
    assert worker_runtime_identity(source) != expected
    declaration.write_text("export declare function magic(): never;\n")
    assert worker_runtime_identity(source) == expected

    manifest_path = source.package_root / "package.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    original_manifest = json.dumps(manifest)
    manifest["exports"]["./spec"]["types"] = "./dist/spec-v2.d.cts"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert worker_runtime_identity(source) != expected
    manifest_path.write_text(original_manifest, encoding="utf-8")
    assert worker_runtime_identity(source) == expected
    manifest_path.write_text(json.dumps(json.loads(original_manifest), indent=2), encoding="utf-8")
    assert worker_runtime_identity(source) == expected
    manifest = json.loads(original_manifest)
    manifest["jauntRuntime"] = {"mode": "strict"}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert worker_runtime_identity(source) != expected
    manifest_path.write_text(original_manifest, encoding="utf-8")
    assert worker_runtime_identity(source) == expected

    client = WorkerClient(root=source.tool_owner, installation=source)
    client.verify_runtime_identity()
    client.pin_full_runtime_identity()
    dist = source.package_root / "dist"
    exact_files = {
        path.relative_to(dist): path.read_bytes() for path in dist.rglob("*") if path.is_file()
    }
    shutil.rmtree(dist)
    for relative, content in exact_files.items():
        path = dist / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()


def test_worker_client_rejects_same_byte_typescript_symlink_aba(tmp_path: Path) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    stores = tmp_path / "stores"

    def compiler_store(name: str) -> Path:
        package = stores / name
        compiler = package / "lib/typescript.js"
        compiler.parent.mkdir(parents=True)
        compiler.write_text("export const version = '6.0.2';\n", encoding="utf-8")
        (package / "lib/lib.es2024.d.ts").write_text(
            "interface Array<T> { readonly length: number; }\n",
            encoding="utf-8",
        )
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "typescript",
                    "version": "6.0.2",
                    "main": "./lib/typescript.js",
                }
            ),
            encoding="utf-8",
        )
        return package

    first = compiler_store("first")
    second = compiler_store("second")
    lexical = tmp_path / "node_modules/typescript"
    lexical.parent.mkdir(exist_ok=True)
    lexical.symlink_to(first, target_is_directory=True)
    installation = WorkerInstallation(
        node=installation.node,
        worker_entry=installation.worker_entry,
        compiler_module_path=lexical / "lib/typescript.js",
        package_root=installation.package_root,
        tool_owner=installation.tool_owner,
    )
    assert worker_runtime_identity(installation) == worker_runtime_identity(
        WorkerInstallation(
            node=installation.node,
            worker_entry=installation.worker_entry,
            compiler_module_path=second / "lib/typescript.js",
            package_root=installation.package_root,
            tool_owner=installation.tool_owner,
        )
    )

    client = WorkerClient(root=tmp_path, installation=installation)
    client.verify_runtime_identity()
    lexical.unlink()
    lexical.symlink_to(second, target_is_directory=True)

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()


@pytest.mark.parametrize("module_path", [False, True])
@pytest.mark.parametrize("remove_nearer", [False, True])
def test_package_resolution_pin_rejects_nearer_package_topology_changes(
    tmp_path: Path,
    *,
    module_path: bool,
    remove_nearer: bool,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    if module_path:
        start = tmp_path / "node_modules/@usejaunt/ts/dist/test/runner.js"
        nearer = tmp_path / "node_modules/@usejaunt/ts/node_modules/vitest"
        boundary = None
    else:
        start = tmp_path / "packages/web"
        nearer = start / "node_modules/vitest"
        boundary = tmp_path
    start.parent.mkdir(parents=True, exist_ok=True)
    if module_path:
        start.write_text("export {};\n", encoding="utf-8")
    else:
        start.mkdir(exist_ok=True)
    fallback = tmp_path / "node_modules/vitest"

    def install(package: Path) -> None:
        (package / "dist").mkdir(parents=True)
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "vitest",
                    "version": "4.1.10",
                    "exports": "./dist/index.js",
                }
            ),
            encoding="utf-8",
        )
        (package / "dist/index.js").write_text(
            "export const runtime = 'same-bytes';\n",
            encoding="utf-8",
        )

    install(fallback)
    if remove_nearer:
        install(nearer)

    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_identity(
        "Vitest test package",
        start,
        "vitest",
        boundary=boundary,
        module_path=module_path,
        expected_name="vitest",
    )

    if remove_nearer:
        shutil.rmtree(nearer)
    else:
        install(nearer)

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()

    client.reset_full_runtime_identity()
    client.verify_runtime_identity()


@pytest.mark.parametrize(
    "relative_runtime",
    [
        "node_modules/vite/dist/index.js",
        "node_modules/rollup/dist/native.node",
        "node_modules/rollup/dist/parser.wasm",
        "node_modules/vite/bin/esbuild",
    ],
)
def test_package_resolution_closure_seal_pins_all_transitive_runtime_files(
    tmp_path: Path,
    relative_runtime: str,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")

    def install(package: str, dependencies: Mapping[str, str] | None = None) -> Path:
        package_root = tmp_path / "node_modules" / package
        runtime = package_root / "dist/index.js"
        runtime.parent.mkdir(parents=True)
        runtime.write_text(f"export const packageName = {package!r};\n", encoding="utf-8")
        (package_root / "package.json").write_text(
            json.dumps(
                {
                    "name": package,
                    "version": "1.0.0",
                    "main": "./dist/index.js",
                    "dependencies": dependencies or {},
                }
            ),
            encoding="utf-8",
        )
        return package_root

    install("vitest", {"vite": "^7.0.0"})
    install("vite", {"rollup": "^4.0.0"})
    install("rollup")
    runtime = tmp_path / relative_runtime
    runtime.parent.mkdir(parents=True, exist_ok=True)
    runtime.write_bytes(b"runtime-v1\n")
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest package",
        tmp_path,
        "vitest",
        boundary=tmp_path,
        expected_name="vitest",
    )

    runtime.write_bytes(b"runtime-v2\n")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.seal_runtime_identity()


def test_package_resolution_closure_pins_undeclared_static_import_from_pnpm_store(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    store = tmp_path / "node_modules/.pnpm/vite-plugin-example@1.0.0/node_modules"
    plugin = store / "vite-plugin-example"
    runtime = plugin / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        'export default load("hoisted-runtime-helper");\n',
        encoding="utf-8",
    )
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    helper = store / "hoisted-runtime-helper"
    helper.mkdir()
    helper_runtime = helper / "index.js"
    helper_runtime.write_text("export const value = 1;\n", encoding="utf-8")
    (helper / "package.json").write_text(
        json.dumps(
            {
                "name": "hoisted-runtime-helper",
                "version": "1.0.0",
                "type": "module",
                "main": "./index.js",
            }
        ),
        encoding="utf-8",
    )
    lexical_plugin = tmp_path / "node_modules/vite-plugin-example"
    lexical_plugin.symlink_to(plugin, target_is_directory=True)
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest config plugin",
        tmp_path,
        "vite-plugin-example",
        boundary=tmp_path,
        expected_name="vite-plugin-example",
    )

    helper_runtime.write_text("export const value = 2;\n", encoding="utf-8")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.seal_runtime_identity()


def test_package_resolution_closure_pins_external_package_import_alias(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    plugin = tmp_path / "node_modules/vite-plugin-example"
    runtime = plugin / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('export { value } from "#helper";\n', encoding="utf-8")
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
                "imports": {"#helper": "hoisted-runtime-helper"},
            }
        ),
        encoding="utf-8",
    )
    helper = tmp_path / "node_modules/hoisted-runtime-helper"
    helper.mkdir()
    helper_runtime = helper / "index.js"
    helper_runtime.write_text("export const value = 1;\n", encoding="utf-8")
    (helper / "package.json").write_text(
        json.dumps(
            {
                "name": "hoisted-runtime-helper",
                "version": "1.0.0",
                "main": "./index.js",
            }
        ),
        encoding="utf-8",
    )
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest config plugin",
        tmp_path,
        "vite-plugin-example",
        boundary=tmp_path,
        expected_name="vite-plugin-example",
    )

    helper_runtime.write_text("export const value = 2;\n", encoding="utf-8")

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.seal_runtime_identity()


def test_package_resolution_closure_handles_bundled_loader_runtime_plumbing(
    tmp_path: Path,
) -> None:
    package = tmp_path / "node_modules/vite-like"
    runtime = package / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        'import * as nodeModule from "node:module";\n'
        "const require = nodeModule.createRequire(import.meta.url);\n"
        "const executor = { require: nodeModule.createRequire(runtimeHref) };\n"
        "const bundled = ((fallback) => typeof require < 'u' ? require : "
        "typeof Proxy < 'u' ? new Proxy(fallback, { get: (_target, name) => "
        "(typeof require < 'u' ? require : fallback)[name] }) : fallback)"
        "(function (...args) { return require.apply(this, args); });\n"
        'bundled("hoisted-runtime-helper");\n'
        "bundled(runtimeSelectedPackage);\n"
        "export { executor, require as i };\n",
        encoding="utf-8",
    )
    (package / "package.json").write_text(
        json.dumps({"name": "vite-like", "version": "1.0.0", "main": "./dist/index.js"}),
        encoding="utf-8",
    )
    helper = tmp_path / "node_modules/hoisted-runtime-helper"
    helper.mkdir()
    (helper / "index.js").write_text("export {};\n", encoding="utf-8")
    (helper / "package.json").write_text(
        json.dumps({"name": "hoisted-runtime-helper", "version": "1.0.0", "main": "./index.js"}),
        encoding="utf-8",
    )

    closure = _runtime_package_resolution_closure(package, root_label="vite-like")

    assert {edge.package for edge in closure} == {"hoisted-runtime-helper"}
    assert all(edge.resolved_root is not None for edge in closure)


def test_package_resolution_closure_resolves_all_package_import_mapping_forms(
    tmp_path: Path,
) -> None:
    plugin = tmp_path / "node_modules/vite-plugin-example"
    runtime = plugin / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "\n".join(
            [
                'import "#exact";',
                'import "#nested";',
                'import "#conditional";',
                'import "#array";',
                'import "#pattern/feature";',
                'import "#internal";',
                'import "#self";',
            ]
        ),
        encoding="utf-8",
    )
    (plugin / "dist/internal.js").write_text("export {};\n", encoding="utf-8")
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
                "imports": {
                    "#exact": "exact-helper/subpath",
                    "#nested": "#exact",
                    "#conditional": {
                        "node": "node-helper",
                        "default": "default-helper",
                    },
                    "#array": ["array-helper", "fallback-helper"],
                    "#pattern/*": "pattern-helper/*",
                    "#internal": "./dist/internal.js",
                    "#self": "vite-plugin-example/internal",
                },
            }
        ),
        encoding="utf-8",
    )
    expected = {
        "array-helper",
        "default-helper",
        "exact-helper",
        "fallback-helper",
        "node-helper",
        "pattern-helper",
    }
    for package in expected:
        package_root = tmp_path / "node_modules" / package
        package_root.mkdir()
        (package_root / "index.js").write_text("export {};\n", encoding="utf-8")
        (package_root / "package.json").write_text(
            json.dumps({"name": package, "version": "1.0.0", "main": "./index.js"}),
            encoding="utf-8",
        )

    closure = _runtime_package_resolution_closure(
        plugin,
        root_label="vite-plugin-example",
    )

    assert {edge.package for edge in closure} == expected


@pytest.mark.parametrize(
    ("imports", "message"),
    [
        ({}, "unresolved alias"),
        ({"#helper": "./../outside.js"}, "unsafe package-relative target"),
    ],
)
def test_package_resolution_closure_rejects_invalid_package_import_alias(
    tmp_path: Path,
    imports: Mapping[str, object],
    message: str,
) -> None:
    plugin = tmp_path / "node_modules/vite-plugin-example"
    runtime = plugin / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text('import "#helper";\n', encoding="utf-8")
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
                "imports": imports,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(TypeScriptWorkerError, match=message):
        _runtime_package_resolution_closure(
            plugin,
            root_label="vite-plugin-example",
        )


def test_package_resolution_closure_pins_absent_undeclared_static_import(
    tmp_path: Path,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    plugin = tmp_path / "node_modules/vite-plugin-example"
    runtime = plugin / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        'export const optional = () => load.resolve("optional-hoisted-helper");\n',
        encoding="utf-8",
    )
    (plugin / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "type": "module",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest config plugin",
        tmp_path,
        "vite-plugin-example",
        boundary=tmp_path,
        expected_name="vite-plugin-example",
    )

    helper = tmp_path / "node_modules/optional-hoisted-helper"
    helper.mkdir()
    (helper / "index.js").write_text("export const value = 1;\n", encoding="utf-8")
    (helper / "package.json").write_text(
        json.dumps(
            {
                "name": "optional-hoisted-helper",
                "version": "1.0.0",
                "main": "./index.js",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()


def test_runtime_package_identity_prunes_nested_node_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "node_modules/vite"
    (package / "dist").mkdir(parents=True)
    (package / "dist/index.js").write_text("export {};\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "vite", "version": "7.0.0", "main": "./dist/index.js"}),
        encoding="utf-8",
    )
    nested_runtime = package / "node_modules/unlisted-runtime/native.node"
    nested_runtime.parent.mkdir(parents=True)
    nested_runtime.write_bytes(b"native-runtime\n")
    original_is_file = Path.is_file

    def reject_nested_walk(path: Path) -> bool:
        if path == nested_runtime:
            raise AssertionError("nested node_modules must be pruned before file inspection")
        return original_is_file(path)

    monkeypatch.setattr(Path, "is_file", reject_nested_walk)

    assert runtime_package_identity(package, expected_name="vite").startswith("sha256:")


def test_runtime_package_identity_tracks_full_manifest_semantics(tmp_path: Path) -> None:
    package = tmp_path / "node_modules/vite-plugin-example"
    (package / "dist").mkdir(parents=True)
    (package / "dist/index.js").write_text("export {};\n", encoding="utf-8")
    manifest = {
        "name": "vite-plugin-example",
        "version": "1.0.0",
        "main": "./dist/index.js",
        "pluginConfig": {"mode": "fast"},
    }
    manifest_path = package / "package.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    before = runtime_package_identity(package, expected_name="vite-plugin-example")

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert runtime_package_identity(package, expected_name="vite-plugin-example") == before

    manifest["pluginConfig"]["mode"] = "strict"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert runtime_package_identity(package, expected_name="vite-plugin-example") != before


def test_runtime_package_identity_tracks_internal_symlink_retargeting_portably(
    tmp_path: Path,
) -> None:
    def install(root: Path) -> tuple[Path, Path]:
        package = root / "node_modules/vite-plugin-example"
        dist = package / "dist"
        dist.mkdir(parents=True)
        for name in ("implementation-a.js", "implementation-b.js"):
            (dist / name).write_text("export const value = 1;\n", encoding="utf-8")
        entry = dist / "index.js"
        entry.symlink_to("implementation-a.js")
        (package / "package.json").write_text(
            json.dumps(
                {
                    "name": "vite-plugin-example",
                    "version": "1.0.0",
                    "main": "./dist/index.js",
                }
            ),
            encoding="utf-8",
        )
        return package, entry

    first, _first_entry = install(tmp_path / "first")
    second, second_entry = install(tmp_path / "second")
    expected = runtime_package_identity(first, expected_name="vite-plugin-example")
    assert runtime_package_identity(second, expected_name="vite-plugin-example") == expected

    second_entry.unlink()
    second_entry.symlink_to("implementation-b.js")

    assert runtime_package_identity(second, expected_name="vite-plugin-example") != expected


def test_runtime_package_identity_tracks_exact_internal_symlink_text(tmp_path: Path) -> None:
    package = tmp_path / "node_modules/vite-plugin-example"
    dist = package / "dist"
    dist.mkdir(parents=True)
    (dist / "implementation.js").write_text("export const value = 1;\n", encoding="utf-8")
    entry = dist / "index.js"
    os.symlink("implementation.js", entry)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    expected = runtime_package_identity(package, expected_name="vite-plugin-example")

    entry.unlink()
    os.symlink("./implementation.js", entry)

    assert entry.resolve() == (dist / "implementation.js").resolve()
    assert runtime_package_identity(package, expected_name="vite-plugin-example") != expected


@pytest.mark.parametrize("target", ["missing.js", "../../../outside.js"])
def test_runtime_package_identity_rejects_invalid_internal_symlinks(
    tmp_path: Path,
    target: str,
) -> None:
    package = tmp_path / "node_modules/vite-plugin-example"
    dist = package / "dist"
    dist.mkdir(parents=True)
    (tmp_path / "outside.js").write_text("export {};\n", encoding="utf-8")
    (dist / "index.js").symlink_to(target)
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        TypeScriptWorkerError,
        match="runtime package symlink|Runtime package symlink",
    ):
        runtime_package_identity(package, expected_name="vite-plugin-example")


def test_runtime_package_identity_rejects_symlink_mutation_during_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = tmp_path / "node_modules/vite-plugin-example"
    dist = package / "dist"
    dist.mkdir(parents=True)
    for name in ("implementation-a.js", "implementation-b.js"):
        (dist / name).write_text("export const value = 1;\n", encoding="utf-8")
    entry = dist / "index.js"
    entry.symlink_to("implementation-a.js")
    (package / "package.json").write_text(
        json.dumps(
            {
                "name": "vite-plugin-example",
                "version": "1.0.0",
                "main": "./dist/index.js",
            }
        ),
        encoding="utf-8",
    )
    original_readlink = os.readlink
    mutated = False

    def mutate_after_read(
        path: os.PathLike[str] | str,
        *,
        dir_fd: int | None = None,
    ) -> str:
        nonlocal mutated
        target = original_readlink(path, dir_fd=dir_fd)
        if Path(path) == entry and not mutated:
            mutated = True
            entry.unlink()
            entry.symlink_to("implementation-b.js")
        return target

    monkeypatch.setattr(os, "readlink", mutate_after_read)

    with pytest.raises(TypeScriptWorkerError, match="changed while its freshness identity"):
        runtime_package_identity(package, expected_name="vite-plugin-example")
    assert mutated is True


def test_runtime_seal_rechecks_packages_changed_during_its_first_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    package = tmp_path / "node_modules/vitest"
    runtime = package / "dist/index.js"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("export const value = 1;\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "vitest", "version": "4.1.10", "main": "./dist/index.js"}),
        encoding="utf-8",
    )
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_runtime_identity(
        "Vitest package",
        package,
        expected_name="vitest",
    )
    original_verify = client.verify_runtime_identity
    verification_count = 0

    def mutate_after_first_verification() -> str:
        nonlocal verification_count
        result = original_verify()
        verification_count += 1
        if verification_count == 1:
            runtime.write_text("export const value = 2;\n", encoding="utf-8")
        return result

    monkeypatch.setattr(client, "verify_runtime_identity", mutate_after_first_verification)

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.seal_runtime_identity()

    assert verification_count == 1


def test_package_resolution_closure_rejects_dependency_symlink_aba(tmp_path: Path) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    vitest = tmp_path / "node_modules/vitest"
    (vitest / "dist").mkdir(parents=True)
    (vitest / "dist/index.js").write_text("export {};\n", encoding="utf-8")
    (vitest / "package.json").write_text(
        json.dumps(
            {
                "name": "vitest",
                "version": "4.1.10",
                "dependencies": {"vite": "^7.0.0"},
            }
        ),
        encoding="utf-8",
    )
    stores = tmp_path / ".package-store"

    def vite_store(name: str) -> Path:
        package = stores / name
        (package / "dist").mkdir(parents=True)
        (package / "dist/index.js").write_text("export const same = true;\n", encoding="utf-8")
        (package / "package.json").write_text(
            json.dumps({"name": "vite", "version": "7.0.0"}),
            encoding="utf-8",
        )
        return package

    first = vite_store("vite-a")
    second = vite_store("vite-b")
    lexical_vite = tmp_path / "node_modules/vite"
    lexical_vite.symlink_to(first, target_is_directory=True)
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest package",
        tmp_path,
        "vitest",
        boundary=tmp_path,
        expected_name="vitest",
    )

    lexical_vite.unlink()
    lexical_vite.symlink_to(second, target_is_directory=True)

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()


def test_package_resolution_closure_pins_missing_optional_dependency(tmp_path: Path) -> None:
    installation = _installation(tmp_path, "console.log('worker');\n")
    vitest = tmp_path / "node_modules/vitest"
    (vitest / "dist").mkdir(parents=True)
    (vitest / "dist/index.js").write_text("export {};\n", encoding="utf-8")
    (vitest / "package.json").write_text(
        json.dumps(
            {
                "name": "vitest",
                "version": "4.1.10",
                "optionalDependencies": {"optional-runtime": "^1.0.0"},
            }
        ),
        encoding="utf-8",
    )
    client = WorkerClient(root=tmp_path, installation=installation)
    client.pin_package_resolution_closure(
        "Vitest package",
        tmp_path,
        "vitest",
        boundary=tmp_path,
        expected_name="vitest",
    )

    optional_runtime = tmp_path / "node_modules/optional-runtime"
    optional_runtime.mkdir()
    (optional_runtime / "package.json").write_text(
        json.dumps({"name": "optional-runtime", "version": "1.0.0"}),
        encoding="utf-8",
    )

    with pytest.raises(
        WorkerToolchainChangedError,
        match="JAUNT_TS_TOOLCHAIN_CHANGED_DURING_BUILD",
    ):
        client.verify_runtime_identity()


def test_packaged_worker_identity_fails_closed_without_runtime_tree(tmp_path: Path) -> None:
    package = tmp_path / "node_modules/@usejaunt/ts"
    package.mkdir(parents=True)
    worker = package / "worker.js"
    worker.write_text("export {};\n", encoding="utf-8")
    (package / "package.json").write_text(
        json.dumps({"name": "@usejaunt/ts", "version": "0.1.0-alpha.0"}),
        encoding="utf-8",
    )
    compiler = tmp_path / "typescript.js"
    compiler.write_text("export {};\n", encoding="utf-8")
    installation = WorkerInstallation(
        node=sys.executable,
        worker_entry=worker,
        compiler_module_path=compiler,
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


def test_worker_client_does_not_replay_deterministic_heap_oom(tmp_path: Path) -> None:
    marker = tmp_path / "validate-count"
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
    elif method == "validateOverlay":
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        sys.stderr.write(
            "FATAL ERROR: Reached heap limit Allocation failed - "
            "JavaScript heap out of memory\\n"
        )
        sys.stderr.flush()
        os._exit(134)
    else:
        result = {{"method": method, **stamp}}
    print(json.dumps({{
        "protocol": "{PROTOCOL_VERSION}",
        "id": request["id"],
        "ok": True,
        "result": result,
    }}), flush=True)
''',
    )

    async def run() -> None:
        client = WorkerClient(root=tmp_path, installation=installation)
        await client.initialize(_initialize_params(tmp_path))
        with pytest.raises(WorkerOutOfMemoryError, match="was not replayed"):
            await client.request("validateOverlay", {})
        assert marker.read_text() == "1"
        await client.close()

    asyncio.run(run())


def test_worker_client_waits_for_delayed_stderr_before_classifying_oom(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "validate-count"
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
    elif method == "validateOverlay":
        count = int(marker.read_text()) + 1 if marker.exists() else 1
        marker.write_text(str(count))
        sys.stderr.write("FATAL ERROR: JavaScript heap out of memory\\n")
        sys.stderr.flush()
        os._exit(134)
    print(json.dumps({{
        "protocol": "{PROTOCOL_VERSION}",
        "id": request["id"],
        "ok": True,
        "result": result,
    }}), flush=True)
''',
    )

    class DelayedStderrClient(WorkerClient):
        async def _read_stderr(self) -> None:
            await asyncio.sleep(0.05)
            await super()._read_stderr()

    async def run() -> None:
        client = DelayedStderrClient(root=tmp_path, installation=installation)
        await client.initialize(_initialize_params(tmp_path))
        with pytest.raises(WorkerOutOfMemoryError, match="was not replayed"):
            await client.request("validateOverlay", {})
        assert marker.read_text() == "1"
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
