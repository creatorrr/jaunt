from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
import tracemalloc
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import SupportsIndex, overload

import pytest

import jaunt.typescript.worker as typescript_worker
from jaunt.typescript.config import TypeScriptTargetConfig
from jaunt.typescript.protocol import PROTOCOL_VERSION, InitializeParams
from jaunt.typescript.worker import (
    _annotation_initializer_starts,
    _create_require_module_specifiers,
    _runtime_module_specifiers,
    _runtime_package_resolution_closure,
    _ScopeCapabilityIndex,
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


class _AccessCountingTokens(tuple[tuple[str, str], ...]):
    """Count token reads and copies so scanner tests can guard linear setup."""

    indexed_items: int
    line_breaks_before: tuple[bool, ...]
    sliced_items: int

    def __new__(cls, values: list[tuple[str, str]]) -> _AccessCountingTokens:
        instance = super().__new__(cls, values)
        instance.indexed_items = 0
        instance.sliced_items = 0
        return instance

    @overload
    def __getitem__(self, key: SupportsIndex, /) -> tuple[str, str]: ...

    @overload
    def __getitem__(
        self,
        key: slice[SupportsIndex | None],
        /,
    ) -> tuple[tuple[str, str], ...]: ...

    def __getitem__(
        self,
        key: SupportsIndex | slice[SupportsIndex | None],
        /,
    ) -> tuple[str, str] | tuple[tuple[str, str], ...]:
        if isinstance(key, slice):
            start, stop, step = key.indices(len(self))
            self.sliced_items += len(range(start, stop, step))
        else:
            self.indexed_items += 1
        return super().__getitem__(key)


def test_scope_capability_index_resolves_nearest_containing_interval() -> None:
    index = _ScopeCapabilityIndex(
        {
            0: (0, "root"),
            3: (3, "sibling"),
        },
        scope_open=[-1, 0, 1, 20],
        scope_end_exclusive=[100, 20, 10, 40],
    )

    index.add(start=0, end=20, capability="outer")
    index.add(start=1, end=10, capability="inner")

    assert index.capability_at(5) == "inner"
    assert index.capability_at(15) == "outer"
    assert index.capability_at(30) == "sibling"
    assert index.capability_at(50) == "root"


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


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('import(("grouped-import"))', "grouped-import"),
        ('require(("grouped-require"))', "grouped-require"),
        ('module.require(("grouped-module-require"))', "grouped-module-require"),
        ('require.call(null, ("grouped-call"))', "grouped-call"),
        ('require.apply(null, [("grouped-apply")])', "grouped-apply"),
        ('const load = require; load(("grouped-alias"));', "grouped-alias"),
        (
            'const load = module.require; load.call(null, ("grouped-alias-call"));',
            "grouped-alias-call",
        ),
        (
            'const { require: load } = module; load.apply(null, [("grouped-alias-apply")]);',
            "grouped-alias-apply",
        ),
    ],
)
def test_runtime_package_scanner_unwraps_grouped_static_specifiers(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (expected,)


@pytest.mark.parametrize(
    "load",
    [
        'import(("partial-import") + suffix)',
        'require("partial-ungrouped" + suffix)',
        'require(("partial-require") + suffix)',
        'require(("partial-comma", suffix))',
        'module.require(("partial-module") || fallback)',
        'require.call(null, ("partial-call") + suffix)',
        'require.apply(null, ["partial-array", fallback])',
        'require.apply(null, [("partial-apply"), fallback])',
        'const load = require; load(("partial-alias") + suffix)',
        'const load = require; load.apply(null, [("partial-alias-apply"), fallback])',
    ],
)
def test_runtime_package_scanner_does_not_extract_from_composed_specifiers(
    tmp_path: Path,
    load: str,
) -> None:
    source = f'{load}; require("real-static-sibling");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-static-sibling",
    )


@pytest.mark.parametrize("prefix", ["", "\ufeff"])
@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
def test_runtime_package_scanner_ignores_hashbang_text(
    tmp_path: Path,
    prefix: str,
    terminator: str,
) -> None:
    source = f'{prefix}#! require("inert-hashbang"){terminator}require("real-after-hashbang");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (
        "real-after-hashbang",
    )


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('require?.("optional-require")', "optional-require"),
        ('require?.resolve("optional-resolve")', "optional-resolve"),
        ('require.resolve?.("optional-resolve-call")', "optional-resolve-call"),
        ('module.require?.("optional-module-require")', "optional-module-require"),
        ('module?.require?.("optional-module-chain")', "optional-module-chain"),
    ],
)
def test_runtime_package_scanner_captures_optional_native_loader_calls(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (expected,)


@pytest.mark.parametrize(
    "source",
    [
        'const load = require; load("indirect-commonjs");',
        'const first = require; const second = first; second("indirect-commonjs");',
        'const load = module.require; load("indirect-commonjs");',
        'const load = module["require"]; load("indirect-commonjs");',
        'const { require: load } = module; load("indirect-commonjs");',
        'const { require } = module; require("indirect-commonjs");',
        'module["require"]("indirect-commonjs");',
        'require.call(null, "indirect-commonjs");',
        'require.apply(null, ["indirect-commonjs"]);',
        'require["resolve"]("indirect-commonjs");',
    ],
)
def test_runtime_package_scanner_tracks_indirect_ambient_commonjs_loaders(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (
        "indirect-commonjs",
    )


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('require.call(null, "arrow-default-call")', "arrow-default-call"),
        ('require.apply(null, ["arrow-default-apply"])', "arrow-default-apply"),
    ],
)
def test_runtime_package_scanner_captures_ambient_loader_forwarding_in_arrow_defaults(
    tmp_path: Path,
    expression: str,
    expected: str,
) -> None:
    source = f"const run = (value = {expression}) => value;"

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (expected,)


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ('load("arrow-default-bound")', "arrow-default-bound"),
        ('load.call(null, "arrow-default-bound-call")', "arrow-default-bound-call"),
        ('load.apply(null, ["arrow-default-bound-apply"])', "arrow-default-bound-apply"),
    ],
)
def test_runtime_package_scanner_captures_bound_loader_calls_in_arrow_defaults(
    tmp_path: Path,
    expression: str,
    expected: str,
) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        "const load = createRequire(import.meta.url); "
        f"const run = (value = {expression}) => value;"
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        expected,
    }


@pytest.mark.parametrize(
    "source",
    [
        "consume(require);",
        "const wrapped = require.bind(null);",
        'module[property]("hidden-commonjs");',
        'require[method]("hidden-commonjs");',
        'module["require"].bind(null)("hidden-commonjs");',
        'const { [property]: load } = module; load("hidden-commonjs");',
        'const { ...rest } = module; rest.require("hidden-commonjs");',
    ],
)
def test_runtime_package_scanner_rejects_ambiguous_ambient_commonjs_loader_flow(
    tmp_path: Path,
    source: str,
) -> None:
    with pytest.raises(TypeScriptWorkerError, match="ambient CommonJS|module-loading capability"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs")


@pytest.mark.parametrize(
    "source",
    [
        'function run(require) { consume(require); require[method]("inert"); }',
        'function run(module) { module[property]("inert"); }',
        'module["exports"] = {};',
    ],
)
def test_runtime_package_scanner_limits_ambient_commonjs_tracking_to_proven_globals(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == ()


@pytest.mark.parametrize(
    "source",
    [
        'class C { #require(value) {} method() { this.#require("inert-private"); } }',
        'class C { #import(value) {} method() { this.#import("inert-private"); } }',
        'class C { static #require(value) {} method() { C.#require("inert-private"); } }',
        'class C { #module; method() { this.#module.require("inert-private"); } }',
        'object.module.require("inert-nested-member");',
    ],
)
def test_runtime_package_scanner_ignores_private_loader_names(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == ()


@pytest.mark.parametrize(
    "source",
    [
        'function run(require) { require("inert-parameter"); }',
        'declare function require(id: string): unknown; require("inert-declaration");',
        'function run(module) { module.require("inert-module-parameter"); }',
        'const run = require => require("inert-arrow-parameter");',
        'const run = (module) => module.require("inert-arrow-parameter");',
        'const run = (require): void => require("inert-typed-arrow-parameter");',
        'const run = (require, value = require.call(null, "inert-arrow-default")) => value;',
        '{ const require = localLoader; require("inert-const"); }',
        '{ let module = localModule; module.require("inert-let"); }',
        'function run({loader: require}) { require("inert-destructured"); }',
        'class require { static value = require("inert-class-name"); }',
        'class module { static value = module.require("inert-class-name"); }',
        'enum require { value } require("inert-enum-name");',
        'module require { export const value = 1; } require("inert-module-name");',
        'namespace require { export const value = 1; } require("inert-namespace-name");',
        'import require from "runtime-loader-shim"; require("inert-import");',
        'import {value as module} from "runtime-module-shim"; module.require("inert-import");',
    ],
)
def test_runtime_package_scanner_ignores_shadowed_native_loaders(
    tmp_path: Path,
    source: str,
) -> None:
    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")

    assert not any(specifier.startswith("inert-") for specifier in specifiers)


@pytest.mark.parametrize(
    "declaration",
    [
        "type require = { value: string };",
        "interface require { value: string }",
        "enum require { value }",
        "module require { export const value = 1; }",
        "namespace require { export const value = 1; }",
    ],
)
def test_runtime_package_scanner_allows_declarations_named_require(
    tmp_path: Path,
    declaration: str,
) -> None:
    assert _runtime_module_specifiers(declaration, source_path=tmp_path / "runtime.ts") == ()


@pytest.mark.parametrize(
    "declaration",
    [
        "type require = { value: string };",
        "interface require { value: string }",
    ],
)
def test_runtime_package_scanner_keeps_ambient_loader_after_type_only_require_declaration(
    tmp_path: Path,
    declaration: str,
) -> None:
    source = f'{declaration} require.call(null, "runtime-after-type-declaration");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "runtime-after-type-declaration",
    )


@pytest.mark.parametrize(
    "source",
    [
        'const load = require; type load = string; load("real");',
        'const load = require; interface load {} load("real");',
        'const load = require; function f<load>() {} load("real");',
        'function f<require>() {} require("real");',
    ],
)
def test_runtime_package_scanner_keeps_value_loaders_across_type_namespace_collisions(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ("real",)


def test_runtime_package_scanner_restores_ambient_loader_after_nested_shadow(
    tmp_path: Path,
) -> None:
    source = (
        '{ const require = localLoader; require("inert-shadow"); } '
        'require("real-ambient"); '
        'function local(module) { module.require("inert-module-shadow"); } '
        'module.require("real-ambient-module");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (
        "real-ambient",
        "real-ambient-module",
    )


def test_runtime_package_scanner_keeps_require_import_assignment_runtime_edge(
    tmp_path: Path,
) -> None:
    source = 'import require = require("runtime-import-assignment"); require("inert-local");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "runtime-import-assignment",
    )


def test_runtime_package_scanner_leaves_create_require_alias_tracking_authoritative(
    tmp_path: Path,
) -> None:
    source = (
        'import {createRequire} from "node:module"; '
        "const require = createRequire(import.meta.url); "
        'require("real-created-loader");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.mjs") == (
        "node:module",
        "real-created-loader",
    )


@pytest.mark.parametrize(
    "source",
    [
        'requ\\u0069re("hidden-require");',
        'module.requ\\u0069re("hidden-module-require");',
        'require.res\\u006flve("hidden-resolve");',
        'const value = `${requ\\u0069re("hidden-template")}`;',
        'const value = <A dep={requ\\u0069re("hidden-jsx")} />;',
    ],
)
def test_runtime_package_scanner_rejects_escaped_executable_identifiers(
    tmp_path: Path,
    source: str,
) -> None:
    suffix = ".tsx" if "<A" in source else ".cjs"
    with pytest.raises(TypeScriptWorkerError, match="escaped executable identifier"):
        _runtime_module_specifiers(source, source_path=tmp_path / f"runtime{suffix}")


def test_runtime_package_scanner_keeps_inert_unicode_escapes_opaque(
    tmp_path: Path,
) -> None:
    source = (
        'const text = "requ\\u0069re(\\"inert-string\\")"; '
        'const pattern = /requ\\u0069re\\("inert-regex"\\)/; '
        'const template = `requ\\u0069re("inert-template")`; '
        'require("real-after-inert-escapes");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.cjs") == (
        "real-after-inert-escapes",
    )


@pytest.mark.parametrize(
    "source",
    [
        'object?.require("fake-require");',
        'object?.require?.("fake-optional-call");',
        'object?.import("fake-import");',
        'object?.import?.("fake-optional-import-call");',
    ],
)
def test_runtime_package_scanner_ignores_optional_member_loader_names(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == ()


def test_runtime_package_scanner_allows_optional_node_module_require(
    tmp_path: Path,
) -> None:
    source = 'module?.require("real-optional-module-require");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-optional-module-require",
    )


@pytest.mark.parametrize(
    "source",
    [
        'import type from "runtime-default-type";',
        'import type, { value } from "runtime-default-and-named";',
        'import type = require("runtime-import-assignment");',
        'import { type } from "runtime-named-type";',
        'export { type } from "runtime-export-named-type";',
        'import { type Foo, value } from "runtime-mixed-import";',
        'export { type Foo, value } from "runtime-mixed-export";',
    ],
)
def test_runtime_package_scanner_disambiguates_runtime_type_bindings(
    tmp_path: Path,
    source: str,
) -> None:
    assert len(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == 1


@pytest.mark.parametrize(
    "source",
    [
        'import type Foo from "inert-default-type";',
        'import type Foo = require("inert-import-assignment");',
        'import type { Foo } from "inert-named-type";',
        'import type * as Foo from "inert-namespace-type";',
        'import { type Foo, type Bar as Baz } from "inert-inline-types";',
        'export type { Foo } from "inert-export-type";',
        'export { type Foo, type Bar as Baz } from "inert-inline-export-types";',
    ],
)
def test_runtime_package_scanner_ignores_type_only_static_clauses(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()


@pytest.mark.parametrize(
    "type_import",
    [
        'import type Foo from "inert-types"',
        'import type Foo = require("inert-types")',
    ],
)
def test_type_only_import_does_not_capture_later_semicolonless_require(
    tmp_path: Path,
    type_import: str,
) -> None:
    source = f'{type_import}\nconst runtime = require("real-runtime")'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-runtime",
    )


def test_type_only_import_assignment_detection_reads_constant_local_context() -> None:
    tokens_list: list[tuple[str, str]] = []
    require_indices: list[tuple[int, bool]] = []
    for index in range(2_000):
        if index % 2:
            prefix = [
                ("identifier", "import"),
                ("identifier", "type"),
                ("identifier", f"Type{index}"),
                ("punctuation", "="),
            ]
            type_only = True
        else:
            prefix = [
                ("identifier", "const"),
                ("identifier", f"runtime{index}"),
                ("punctuation", "="),
            ]
            type_only = False
        tokens_list.extend(prefix)
        require_indices.append((len(tokens_list), type_only))
        tokens_list.extend(
            [
                ("identifier", "require"),
                ("punctuation", "("),
                ("string", f"package-{index}"),
                ("punctuation", ")"),
            ]
        )
    tokens = _AccessCountingTokens(tokens_list)

    assert [
        typescript_worker._type_only_import_assignment_require(tokens, require_index)
        for require_index, _type_only in require_indices
    ] == [type_only for _require_index, type_only in require_indices]
    assert tokens.indexed_items <= len(require_indices) * 5


@pytest.mark.parametrize("declaration", ["export", "import-equals"])
def test_semicolonless_static_clause_scans_read_linear_tokens(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declaration: str,
) -> None:
    tokens_list: list[tuple[str, str]] = []
    for index in range(2_000):
        if declaration == "export":
            tokens_list.extend(
                [
                    ("identifier", "export"),
                    ("identifier", "const"),
                    ("identifier", f"value{index}"),
                    ("punctuation", "="),
                    ("number", str(index)),
                ]
            )
        else:
            tokens_list.extend(
                [
                    ("identifier", "import"),
                    ("identifier", f"Alias{index}"),
                    ("punctuation", "="),
                    ("identifier", f"Namespace{index}"),
                ]
            )
    tokens = _AccessCountingTokens(tokens_list)
    monkeypatch.setattr(
        typescript_worker,
        "_runtime_javascript_tokens",
        lambda *_args, **_kwargs: tokens,
    )

    assert _runtime_module_specifiers("ignored", source_path=tmp_path / "runtime.ts") == ()
    assert tokens.indexed_items < len(tokens) * 40


@pytest.mark.parametrize(
    "source",
    [
        'type Alias = import("inert-alias").Value;',
        'type Query = typeof import("inert-query");',
        'interface Shape { property: import("inert-interface-property").Value; }',
        'type Shape = { property: typeof import("inert-object-property"); };',
        'declare const declared: typeof import("inert-declaration");',
        'const annotated: import("inert-annotation").Value = value;',
        'class Container { property!: import("inert-class-property").Value; }',
        'const asserted = value as import("inert-assertion").Value;',
        'const checked = value satisfies import("inert-satisfies").Value;',
    ],
)
def test_runtime_package_scanner_ignores_erased_import_type_expressions(
    tmp_path: Path,
    source: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()


@pytest.mark.parametrize("assertion", ["as", "satisfies"])
@pytest.mark.parametrize("asserted_type", ["Type", 'import("inert-type").Value'])
@pytest.mark.parametrize(
    ("operator", "package"),
    [("&&", "runtime-and"), ("||", "runtime-or"), ("??", "runtime-nullish")],
)
def test_runtime_package_scanner_ends_type_assertions_at_logical_operators(
    tmp_path: Path,
    assertion: str,
    asserted_type: str,
    operator: str,
    package: str,
) -> None:
    source = f'const result = value {assertion} {asserted_type} {operator} import("{package}");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (package,)


@pytest.mark.parametrize("assertion", ["as", "satisfies"])
@pytest.mark.parametrize("asserted_type", ["Type", 'import("inert-type").Value'])
@pytest.mark.parametrize(
    ("operator", "package"),
    [
        ("+", "runtime-add"),
        ("*", "runtime-multiply"),
        ("==", "runtime-equal"),
        ("===", "runtime-strict-equal"),
        (">", "runtime-greater-than"),
        ("<=", "runtime-less-equal"),
        ("instanceof", "runtime-instanceof"),
        ("in", "runtime-in"),
        ("^", "runtime-xor"),
        ("<<", "runtime-left-shift"),
    ],
)
def test_runtime_package_scanner_ends_type_assertions_at_other_binary_operators(
    tmp_path: Path,
    assertion: str,
    asserted_type: str,
    operator: str,
    package: str,
) -> None:
    source = f'const result = value {assertion} {asserted_type} {operator} import("{package}");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (package,)


@pytest.mark.parametrize("assertion", ["as", "satisfies"])
def test_runtime_package_scanner_keeps_nested_assertion_types_erased(
    tmp_path: Path,
    assertion: str,
) -> None:
    source = (
        f"const generic = value {assertion} "
        'Outer<import("inert-first").First, Inner<import("inert-second").Second>> '
        '+ import("runtime-after-generic"); '
        f"const callable = value {assertion} "
        '<T extends import("inert-constraint").Constraint = '
        'import("inert-default").Default>(argument: T) => '
        'T extends import("inert-check").Check '
        '? import("inert-true").True : import("inert-false").False '
        '^ import("runtime-after-function");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "runtime-after-function",
        "runtime-after-generic",
    )


def test_runtime_package_scanner_keeps_dynamic_import_expressions_executable(
    tmp_path: Path,
) -> None:
    source = (
        'type Inert = import("inert-type").Value; '
        'const direct = import("runtime-direct"); '
        'const nested = { load: import("runtime-property") };'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "runtime-direct",
        "runtime-property",
    )


def test_runtime_package_scanner_scales_across_erased_import_type_expressions(
    tmp_path: Path,
) -> None:
    source = "\n".join(
        f'type Alias{index} = import("inert-{index}").Value;' for index in range(2_000)
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()


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


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
@pytest.mark.parametrize("statement", ["break", "continue"])
def test_runtime_package_scanner_uses_regex_after_restricted_control_statement(
    tmp_path: Path,
    statement: str,
    terminator: str,
) -> None:
    source = (
        f"while (ok) {{ {statement}{terminator}"
        '/ require("inert-restricted-regex") /; } '
        'require("real-after-restricted-regex");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-after-restricted-regex",
    )


def test_runtime_package_scanner_uses_regex_after_debugger_statement(
    tmp_path: Path,
) -> None:
    source = 'debugger\n/ require("inert-debugger-regex") /; require("real-after-debugger-regex");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-after-debugger-regex",
    )


def test_runtime_package_scanner_tracks_restricted_statement_regex_in_template(
    tmp_path: Path,
) -> None:
    source = (
        "const rendered = `${(() => { while (ok) { break\n"
        '/ require("inert-template-regex") /; } '
        'return require("real-template"); })()}`;'
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


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
def test_runtime_package_scanner_ends_line_comments_at_ecmascript_terminators(
    tmp_path: Path,
    terminator: str,
) -> None:
    source = f'// comment{terminator}require("real-after-comment");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-after-comment",
    )


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
def test_runtime_package_scanner_ends_template_expression_comments_at_terminators(
    tmp_path: Path,
    terminator: str,
) -> None:
    source = (
        "const rendered = `${1 // comment" + terminator + '+ require("real-template-comment")}`;'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-template-comment",
    )


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
@pytest.mark.parametrize("in_comment", [False, True])
def test_runtime_package_scanner_tracks_line_breaks_inside_whitespace_and_block_comments(
    tmp_path: Path,
    terminator: str,
    in_comment: bool,
) -> None:
    separator = f"/*{terminator}*/" if in_comment else terminator
    source = (
        f"const async = 1; async{separator}"
        'label: {} / require("inert-regex-text") /; require("real-after-label");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "real-after-label",
    )


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (
            '`value${x}`\nlabel: {} / require("inert-label") /; require("real-label");',
            ("real-label",),
        ),
        (
            '`value${x}`\n{} / require("inert-block") /; require("real-block");',
            ("real-block",),
        ),
        (
            '`value${x}` / require("real-division") / 2;',
            ("real-division",),
        ),
    ],
)
def test_runtime_package_scanner_treats_computed_templates_as_expression_values(
    tmp_path: Path,
    suffix: str,
    expected: tuple[str, ...],
) -> None:
    source = f"const x = 1; {suffix}"

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == expected


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


@pytest.mark.parametrize(
    "decorators",
    [
        "@dec",
        "@abstract",
        "@async",
        "@declare",
        "@interface",
        "@module",
        "@namespace",
        "@type",
        "@declare export default",
        "@async export",
        "@dec()",
        "@ns.dec",
        "@ns.class",
        "@ns.class()",
        "@ns.class.dec",
        "@ns.enum<T>()",
        "@ns.function()?.type",
        "@ns.interface.module.namespace",
        "@ns.dec<T>(arg)",
        "@factory().decorate<T>()",
        "@dec(a > b)",
        "@dec(a >= b)",
        "@dec(a >> b)",
        "@dec({x: a > b}, [c >= d], (e > f))",
        "@factory(a > b).decorate<T>({x: c > d})",
        '@dec(")")',
        '@dec("]")',
        '@dec("}")',
        '@dec("(")',
        '@dec("[")',
        '@dec("{")',
        "@dec(`)`)",
        "@dec<')'>()",
        "@dec<'>'>()",
        "@registry[key]",
        "@(factory())",
        "@[]",
        "@{}",
        "@foo!",
        "@foo!.bar",
        "@foo()!",
        "@foo[key]!",
        "@foo<T>!",
        "@foo?.[key]!.bar",
        "@foo<T>()!.bar",
        "@foo?.()",
        "@a()\n@b.c<T>(x)\nexport default",
        "export @dec()",
    ],
)
def test_runtime_package_scanner_ignores_regex_after_decorated_class_declaration(
    tmp_path: Path,
    decorators: str,
) -> None:
    source = (
        f'{decorators} class C {{}} / require("inert-decorator-regex") /; '
        'require("real-after-declaration");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-declaration",
    )


@pytest.mark.parametrize("name", ["interface", "module", "namespace", "type"])
@pytest.mark.parametrize(
    ("head", "decorator"),
    [("class", ""), ("class", "@dec "), ("function", "")],
)
def test_runtime_package_scanner_allows_contextual_declaration_names(
    tmp_path: Path,
    name: str,
    head: str,
    decorator: str,
) -> None:
    signature = f"{name}()" if head == "function" else name
    source = (
        f'{decorator}{head} {signature} {{}} / require("inert-after-declaration") /; '
        'require("real-after-declaration");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-declaration",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        "class C extends ns.type {}",
        "class C extends type {}",
        "class C extends ns.interface {}",
        "class C implements ns.interface {}",
        "interface C extends ns.type {}",
        "function f(): ns.type {}",
        "@dec class C extends ns.type {}",
        "abstract class C implements ns.interface {}",
    ],
)
def test_runtime_package_scanner_ignores_contextual_names_in_declaration_tails(
    tmp_path: Path,
    declaration: str,
) -> None:
    source = (
        f'{declaration} / require("inert-after-declaration") /; require("real-after-declaration");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-declaration",
    )


def test_runtime_package_scanner_recognizes_ambient_global_declaration(
    tmp_path: Path,
) -> None:
    source = (
        'export {}; declare global {} / require("inert-after-global") /; '
        'require("real-after-global");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-global",
    )


def test_runtime_package_scanner_keeps_global_assignment_object_executable(
    tmp_path: Path,
) -> None:
    source = 'global = {} / require("real-global-divisor") / divisor;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-global-divisor",
    )


@pytest.mark.parametrize("name", ["interface", "module", "namespace", "type"])
@pytest.mark.parametrize("context", ["", "function outer() { ", "class C { "])
def test_runtime_package_scanner_keeps_contextual_name_object_initializers_executable(
    tmp_path: Path,
    name: str,
    context: str,
) -> None:
    closing = " }" if context else ""
    source = f'{context}{name} = {{}} / require("real-object-divisor") / divisor;{closing}'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-object-divisor",
    )


@pytest.mark.parametrize("terminator", ["\n", "\r", "\r\n", "\u2028", "\u2029"])
@pytest.mark.parametrize(
    "declaration",
    [
        "class C {}",
        "@dec class C {}",
        "@a()\n@b.c<T>(x) export default class C {}",
    ],
)
@pytest.mark.parametrize("previous", ["foo()", "const value = 1"])
def test_runtime_package_scanner_recognizes_asi_separated_class_declarations(
    tmp_path: Path,
    previous: str,
    declaration: str,
    terminator: str,
) -> None:
    source = (
        f'{previous}{terminator}{declaration} / require("inert-after-class") /; '
        'require("real-after-class");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-class",
    )


@pytest.mark.parametrize("previous", ["foo!", "const value = foo as const", "foo<string>"])
@pytest.mark.parametrize("decorator", ["", "@dec "])
def test_runtime_package_scanner_recognizes_typescript_asi_expression_terminals(
    tmp_path: Path,
    previous: str,
    decorator: str,
) -> None:
    source = (
        f'{previous}\n{decorator}class C {{}} / require("inert-after-class") /; '
        'require("real-after-class");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-after-class",
    )


@pytest.mark.parametrize("previous", ["!", "left >"])
def test_runtime_package_scanner_preserves_declaration_keyword_expression_continuations(
    tmp_path: Path,
    previous: str,
) -> None:
    source = f'{previous}\nclass C {{}} / require("real-expression-divisor") / divisor;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-expression-divisor",
    )


@pytest.mark.parametrize("extension", [".jsx", ".tsx"])
@pytest.mark.parametrize(
    "element",
    [
        "<A></A>",
        "<><A><B /></A></>",
        "<UI.Panel></UI.Panel>",
        "<svg:path></svg:path>",
        "<A />",
    ],
)
def test_runtime_package_scanner_handles_jsx_inside_decorators(
    tmp_path: Path,
    extension: str,
    element: str,
) -> None:
    source = (
        f'@dec({element}) class C {{}} / require("inert-after-jsx-class") /; '
        'require("real-after-jsx-class");'
    )

    assert _runtime_module_specifiers(
        source,
        source_path=tmp_path / f"runtime{extension}",
    ) == ("real-after-jsx-class",)


def test_runtime_package_scanner_handles_jsx_inside_template_expression(
    tmp_path: Path,
) -> None:
    source = (
        'const rendered = `${<A></A>} / require("inert-template-text") /`; '
        'require("real-after-template");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-template",
    )


@pytest.mark.parametrize(
    "element",
    [
        '<span>require("inert-text")</span>',
        '<>import("inert-import") require("inert-require")</>',
        '<A><UI.Panel>@require("inert-nested")</UI.Panel></A>',
    ],
)
def test_runtime_package_scanner_ignores_raw_jsx_text(
    tmp_path: Path,
    element: str,
) -> None:
    source = f'const element = {element}; require("real-after-jsx");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-jsx",
    )


def test_runtime_package_scanner_captures_jsx_expression_containers(
    tmp_path: Path,
) -> None:
    source = '<A value={require("real-attribute")}>{import("real-child")}require("inert-text")</A>;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-attribute",
        "real-child",
    )


@pytest.mark.parametrize("element", ["<A></A>", "<A />", "<></>"])
def test_runtime_package_scanner_treats_completed_jsx_as_expression_value(
    tmp_path: Path,
    element: str,
) -> None:
    source = f'const ratio = {element} / require("real-jsx-divisor") / divisor;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-jsx-divisor",
    )


@pytest.mark.parametrize(
    "element",
    [
        "< A></A>",
        "< ></ >",
        "<A / >",
        '<Select<string> dep={require("real-generic-dep")} />',
        "<Select<Map<string, number>> />",
        "<Select<<T>() => T> />",
    ],
)
def test_runtime_package_scanner_handles_tsx_tag_trivia_and_type_arguments(
    tmp_path: Path,
    element: str,
) -> None:
    source = f'{element} / require("real-generic-divisor") / divisor;'

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx")) == {
        "real-generic-divisor",
        *({"real-generic-dep"} if "real-generic-dep" in element else set()),
    }


@pytest.mark.parametrize(
    "head",
    [
        "<T,>(value: T) => value",
        "<T extends {}>(value: T) => value",
        "<T = unknown>(value: T) => value",
        "<const T,>(value: T) => value",
    ],
)
def test_runtime_package_scanner_distinguishes_tsx_generic_arrows_from_jsx(
    tmp_path: Path,
    head: str,
) -> None:
    source = f'const generic = {head}; <T></T>; require("real-after-generic");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-generic",
    )


def test_runtime_package_scanner_handles_regex_defaults_in_tsx_generic_arrows(
    tmp_path: Path,
) -> None:
    source = 'const generic = <T,>(value = /\\)/) => require("real-generic-body");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-generic-body",
    )


def test_runtime_package_scanner_preserves_call_signature_shaped_jsx_text(
    tmp_path: Path,
) -> None:
    source = '<T>(x): raw; require("inert-jsx-text")</T>; require("real-after-jsx");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-jsx",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        "interface I { <T>(value: T): T; }",
        "type I = { <T>(value: T): T };",
        "interface I { property: { <T>(value: T): T; }; }",
        "type I = { property: { <T>(value: T): T } };",
    ],
)
def test_runtime_package_scanner_handles_tsx_type_member_call_signatures(
    tmp_path: Path,
    declaration: str,
) -> None:
    source = f'{declaration} require("real-after-call-signature");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-call-signature",
    )


@pytest.mark.parametrize(
    "declaration",
    [
        "declare const callable: { <T>(value: T): T };",
        "const callable: { <T>(value: T): T } = implementation;",
        "function consume(callable: { <T>(value: T): T }) {}",
        "const callable = implementation as { <T>(value: T): T };",
        "const callable = implementation satisfies { <T>(value: T): T };",
        "function make(): { <T>(value: T): T } { return implementation; }",
        "class Container { callable!: { <T>(value: T): T }; }",
        "const container = { consume: (callable: { <T>(value: T): T }) => callable };",
        "const callable: { nested: { <T>(value: T): T } } = implementation;",
    ],
)
def test_runtime_package_scanner_handles_tsx_object_type_call_signatures(
    tmp_path: Path,
    declaration: str,
) -> None:
    source = f'{declaration} require("real-after-object-type");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-object-type",
    )


def test_runtime_package_scanner_keeps_runtime_object_jsx_values_executable(
    tmp_path: Path,
) -> None:
    source = (
        'const container = { nested: { render: <T>(value): raw; require("inert-text")</T> } }; '
        'require("real-after-object");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-object",
    )


@pytest.mark.parametrize(
    ("suffix", "after"),
    [
        ('// </T>\nrequire("real-after-comment");', "real-after-comment"),
        ('const marker = "</T>"; require("real-after-string");', "real-after-string"),
    ],
)
def test_runtime_package_scanner_ignores_unrelated_generic_closing_tag_text(
    tmp_path: Path,
    suffix: str,
    after: str,
) -> None:
    source = f'interface I {{ <T>(value: T): T; }} require("real-before-marker"); {suffix}'

    assert set(
        _runtime_module_specifiers(
            source,
            source_path=tmp_path / "runtime.tsx",
        )
    ) == {
        "real-before-marker",
        after,
    }


@pytest.mark.parametrize(
    "element",
    [
        "<Select<Map</* > */ string>> />",
        "<Select<`>`> />",
    ],
)
def test_runtime_package_scanner_keeps_tsx_type_argument_literals_opaque(
    tmp_path: Path,
    element: str,
) -> None:
    source = (
        f'{element}\nclass C {{}} / require("inert-after-class") /; require("real-after-class");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-class",
    )


def test_runtime_package_scanner_scales_across_tsx_generic_arrows(
    tmp_path: Path,
) -> None:
    source = "\n".join(f"const generic{index} = <T,>(value: T) => value;" for index in range(2_000))

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == ()


def test_runtime_package_scanner_scales_across_nested_tsx_generic_signatures(
    tmp_path: Path,
) -> None:
    nested_type = "string"
    for index in range(2_000):
        nested_type = f"<T{index}>(value{index}: {nested_type}) => T{index}"
    source = f"type Nested = {nested_type};"

    started = time.monotonic()
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == ()
    assert time.monotonic() - started < 1.5


def test_runtime_package_scanner_handles_deep_alternating_jsx_expressions(
    tmp_path: Path,
) -> None:
    depth = 60
    source = "<A>{" * depth + 'require("real-nested")' + "}</A>" * depth

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-nested",
    )


@pytest.mark.parametrize("element", ["<A></A>", "<A />", "<></>", "<span>@</span>"])
@pytest.mark.parametrize("decorator", ["", "@dec "])
def test_runtime_package_scanner_recognizes_asi_after_jsx_values(
    tmp_path: Path,
    element: str,
    decorator: str,
) -> None:
    source = (
        f"const element = {element}\n{decorator}class C {{}} "
        '/ require("inert-after-jsx-class") /; require("real-after-jsx-class");'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (
        "real-after-jsx-class",
    )


def test_runtime_package_scanner_does_not_record_jsx_text_as_decorator_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = typescript_worker._decorator_prefix_before
    decorator_scans = 0

    def counting_decorator_prefix(
        tokens: Sequence[tuple[str, str]],
        end: int,
        *,
        at_index: int | None = None,
    ) -> int | None:
        nonlocal decorator_scans
        decorator_scans += 1
        return original(tokens, end, at_index=at_index)

    monkeypatch.setattr(
        typescript_worker,
        "_decorator_prefix_before",
        counting_decorator_prefix,
    )
    declarations = "\n".join(
        f"const value{index} = left > class C{index} {{}}" for index in range(1_000)
    )
    source = f"const element = <span>@</span>\n{declarations}"

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == ()
    assert decorator_scans == 0


@pytest.mark.parametrize(
    ("extension", "comparison"),
    [
        (".ts", 'left</require("inert-regex")/.test(text)'),
        (".tsx", 'left < /require("inert-regex")/.test(text)'),
    ],
)
def test_runtime_package_scanner_preserves_comparison_to_regex_controls(
    tmp_path: Path,
    extension: str,
    comparison: str,
) -> None:
    source = f'{comparison}; require("real-after-regex");'

    assert _runtime_module_specifiers(
        source,
        source_path=tmp_path / f"runtime{extension}",
    ) == ("real-after-regex",)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ('const value = {...require("real-object-spread")};', "real-object-spread"),
        ('const value = [...require("real-array-spread")];', "real-array-spread"),
        ('consume(...require("real-call-spread"));', "real-call-spread"),
        ('consume(...import("real-import-spread"));', "real-import-spread"),
        ('const element = <A {...require("real-jsx-spread")} />;', "real-jsx-spread"),
    ],
)
def test_runtime_package_scanner_captures_loads_after_spread(
    tmp_path: Path,
    source: str,
    expected: str,
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx") == (expected,)


@pytest.mark.parametrize(
    ("wrapper", "maximum_decorator_scans", "maximum_decorator_span"),
    [
        ("const values = [{comparisons}];", 0, 0),
        ("@dec class Root {{}}; const values = [{comparisons}];", 2, 4),
        ("const values = [@dec class Root {{}},{comparisons}];", 2, 4),
        ("class Host {{ @dec field = {comparisons}; }}", 2, 20),
    ],
)
def test_runtime_package_scanner_bounds_decorator_scans_to_current_frame(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    maximum_decorator_scans: int,
    maximum_decorator_span: int,
) -> None:
    original = typescript_worker._decorator_prefix_before
    decorator_scans = 0
    decorator_span = 0

    def counting_decorator_prefix(
        tokens: Sequence[tuple[str, str]],
        end: int,
        *,
        at_index: int | None = None,
    ) -> int | None:
        nonlocal decorator_scans, decorator_span
        decorator_scans += 1
        decorator_span += end - (at_index if at_index is not None else -1)
        return original(tokens, end, at_index=at_index)

    monkeypatch.setattr(
        typescript_worker,
        "_decorator_prefix_before",
        counting_decorator_prefix,
    )
    comparisons = ",".join(f"left > class C{index} {{}}" for index in range(1_000))
    source = wrapper.format(comparisons=comparisons)

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()
    assert decorator_scans <= maximum_decorator_scans
    assert decorator_span <= maximum_decorator_span


@pytest.mark.parametrize(
    "decorator",
    [
        "@dec([x)",
        "@dec({x)",
        "@()",
        "@<>",
    ],
)
def test_runtime_package_scanner_fails_closed_on_malformed_decorator_groups(
    tmp_path: Path,
    decorator: str,
) -> None:
    source = f'{decorator} class C {{}} / require("decorator-probe") / divisor;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "decorator-probe",
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
        'const value = condition ? left : {} / require("real-divisor");',
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
    "case_expression",
    [
        "left, right",
        "((value: Type) => value)",
        "((value): {field: Type} => value)",
        "(<Value extends {field: Type}>(value: Value) => value)",
        "fn(object.case)",
        "fn(object.default)",
        "[object.case][0]",
        "object.case",
    ],
)
def test_runtime_package_scanner_keeps_switch_clause_blocks_statement_like(
    tmp_path: Path,
    case_expression: str,
) -> None:
    source = f'switch (value) {{ case {case_expression}: {{}} / require("inert-regex-text") /; }}'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == ()


def test_runtime_package_scanner_tracks_generic_ternary_alternate_division(
    tmp_path: Path,
) -> None:
    source = (
        "switch (value) { "
        'case condition ? factory<Left, Right>() : {} / require("real-divisor") / 2: '
        "break; }"
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-divisor",
    )


def test_runtime_package_scanner_tracks_comparison_ternary_alternate_division(
    tmp_path: Path,
) -> None:
    source = (
        "switch (value) { "
        'case condition ? left < right : {} / require("real-divisor") / 2: '
        "break; }"
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == (
        "real-divisor",
    )


@pytest.mark.parametrize(
    "prefix",
    [
        "factory<Left, Right>()",
        "first(), second()",
        "first > Left, Right",
    ],
)
def test_runtime_package_scanner_keeps_asi_label_statement_like(
    tmp_path: Path,
    prefix: str,
) -> None:
    source = f'{prefix}\nlabel: {{}} / require("inert-regex-text") /;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()


@pytest.mark.parametrize(
    "identifier",
    [
        "abstract",
        "async",
        "await",
        "declare",
        "implements",
        "interface",
        "module",
        "namespace",
        "of",
        "type",
    ],
)
def test_runtime_package_scanner_keeps_contextual_identifier_asi_labels_statement_like(
    tmp_path: Path,
    identifier: str,
) -> None:
    source = f'const {identifier} = 1; {identifier}\nlabel: {{}} / require("inert-regex-text") /;'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (
            'const async = 1; async\n{} / require("inert-async") /;',
            (),
        ),
        (
            'async function run() { await\n{} / require("real-await") / 2; }',
            ("real-await",),
        ),
        (
            'for (const value of\n{} / require("real-of") / 2) {}',
            ("real-of",),
        ),
        (
            'function* run() { yield\n{} / require("inert-yield") /; }',
            (),
        ),
    ],
)
def test_runtime_package_scanner_distinguishes_asi_from_expression_continuation(
    tmp_path: Path,
    source: str,
    expected: tuple[str, ...],
) -> None:
    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == expected


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
        'import { createRequire } from "node:module";\n'
        "class Container {\n"
        "  constructor(public\n"
        "    load: NodeRequire = createRequire(import.meta.url)) {\n"
        '    load("hoisted-runtime-helper");\n'
        "  }\n"
        "}",
        'import { createRequire } from "node:module";\n'
        "class Container {\n"
        "  constructor(@decorator()\n"
        "    load: NodeRequire = createRequire(import.meta.url)) {\n"
        '    load("hoisted-runtime-helper");\n'
        "  }\n"
        "}",
    ],
)
def test_runtime_package_scanner_tracks_proven_create_require_forms(
    tmp_path: Path,
    source: str,
) -> None:
    specifiers = _runtime_module_specifiers(source, source_path=tmp_path / "plugin.js")

    assert "hoisted-runtime-helper" in specifiers


def test_runtime_package_scanner_keeps_optional_parameter_after_comparison_local(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "function scoped(\n"
        "  compare = left < right,\n"
        "  optional?,\n"
        "  load: NodeRequire = createRequire(import.meta.url),\n"
        ") {\n"
        '  load("typed-loader");\n'
        "}\n"
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        "typed-loader",
    }


def test_runtime_package_scanner_keeps_same_name_capabilities_scope_local(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        '{ const load = createRequire(import.meta.url); load("inner-only"); }\n'
        "function load(name) { return name; }\n"
        'load("ordinary-root");\n'
    )

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js") == (
        "inner-only",
        "node:module",
    )


@pytest.mark.parametrize(
    ("container", "inside", "outside"),
    [
        (
            "const object = { method() { BODY } };",
            "object-method",
            'load("outside-object");',
        ),
        (
            'class C { a() { BODY } b() { load("sibling-method"); } }',
            "class-method",
            "",
        ),
        (
            'class C { type(): string { BODY } interface() { load("sibling-contextual"); } }',
            "typed-method",
            "",
        ),
        (
            "const arrow = () => { BODY };",
            "arrow",
            'load("outside-arrow");',
        ),
        (
            "const arrow = async (value: string): Promise<void> => { BODY };",
            "typed-arrow",
            'load("outside-typed-arrow");',
        ),
        (
            "const expression = function () { BODY };",
            "function-expression",
            'load("outside-function");',
        ),
        (
            'class C { static { BODY } static { load("sibling-static"); } }',
            "static-block",
            "",
        ),
    ],
)
def test_runtime_package_scanner_keeps_function_like_capabilities_lexically_scoped(
    tmp_path: Path,
    container: str,
    inside: str,
    outside: str,
) -> None:
    body = f'const load = createRequire(import.meta.url); load("{inside}");'
    source = (
        'import { createRequire } from "node:module"; '
        f"{container.replace('BODY', body)} {outside}"
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        inside,
    }


@pytest.mark.parametrize(
    ("container", "expected"),
    [
        (
            'if (ok) { var load = createRequire(import.meta.url); } load("var-block");',
            "var-block",
        ),
        (
            "if (ok) { var sentinel = 1, load = createRequire(import.meta.url); } "
            'load("var-multi");',
            "var-multi",
        ),
        (
            'var load; if (ok) { load = createRequire(import.meta.url); } load("var-assignment");',
            "var-assignment",
        ),
        (
            'try { var load = createRequire(import.meta.url); } finally {} load("var-try");',
            "var-try",
        ),
        (
            "function scoped() { if (ok) { var load = createRequire(import.meta.url); } "
            'load("var-function"); } load("outside-function");',
            "var-function",
        ),
        (
            "class C { static { if (ok) { var load = createRequire(import.meta.url); } "
            'load("var-static"); } } load("outside-static");',
            "var-static",
        ),
    ],
)
def test_runtime_package_scanner_hoists_var_to_the_nearest_var_scope(
    tmp_path: Path,
    container: str,
    expected: str,
) -> None:
    source = 'import { createRequire } from "node:module"; ' + container

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        expected,
    }


@pytest.mark.parametrize(
    "declaration",
    [
        "function scoped(load = createRequire(import.meta.url)) { "
        'load("parameter"); } load("outside-parameter");',
        "const scoped = (load = createRequire(import.meta.url)) => { "
        'load("parameter"); }; load("outside-parameter");',
        "function scoped(load = createRequire(import.meta.url)): { ok: boolean } { "
        'load("parameter"); return { ok: true }; } load("outside-parameter");',
        "class C { scoped(load = createRequire(import.meta.url)): { ok: boolean } { "
        'load("parameter"); return { ok: true }; } } load("outside-parameter");',
    ],
)
def test_runtime_package_scanner_owns_defaulted_loader_parameters_in_the_function(
    tmp_path: Path,
    declaration: str,
) -> None:
    source = 'import { createRequire } from "node:module"; ' + declaration

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        "parameter",
    }


@pytest.mark.parametrize(("keyword", "outside"), [("let", False), ("const", False), ("var", True)])
def test_runtime_package_scanner_scopes_for_head_loader_bindings(
    tmp_path: Path,
    keyword: str,
    outside: bool,
) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        f"for ({keyword} load = createRequire(import.meta.url); ok;) {{ "
        'load("inside-loop"); break; } load("outside-loop");'
    )

    expected = {"node:module", "inside-loop"}
    if outside:
        expected.add("outside-loop")
    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == expected


def test_runtime_package_scanner_allows_disjoint_capability_kinds_with_one_name(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire, Module } from "node:module"; '
        '{ const x = createRequire(import.meta.url); x("loader-scope"); } '
        "{ const x = Module; }"
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "loader-scope",
        "node:module",
    }


def test_runtime_package_scanner_rejects_class_field_loader_storage(tmp_path: Path) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        "class C { load = createRequire(import.meta.url); "
        'other = load("not-a-lexical-loader"); }'
    )

    with pytest.raises(TypeScriptWorkerError, match="class field"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")


@pytest.mark.parametrize(
    "storage",
    [
        "class C { load = (createRequire(import.meta.url)); }",
        "class C { load: NodeRequire = (createRequire(import.meta.url)); }",
        "class C { constructor() { this.load = createRequire(import.meta.url); } }",
        "class C { constructor(public load = createRequire(import.meta.url)) {} "
        'use() { this.load("hidden"); } }',
    ],
)
def test_runtime_package_scanner_rejects_property_backed_loader_variants(
    tmp_path: Path,
    storage: str,
) -> None:
    source = 'import { createRequire } from "node:module"; ' + storage

    with pytest.raises(TypeScriptWorkerError, match="property"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")


@pytest.mark.parametrize(
    "shadow",
    [
        '{ const load = fake; load("phantom"); }',
        'function scoped(load) { load("phantom"); }',
        'try {} catch (load) { load("phantom"); }',
        '{ function load() {} load("phantom"); }',
    ],
)
def test_runtime_package_scanner_respects_non_capability_loader_shadows(
    tmp_path: Path,
    shadow: str,
) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        "const load = createRequire(import.meta.url); "
        f'{shadow} load("real");'
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "node:module",
        "real",
    }


@pytest.mark.parametrize(
    "arrow",
    [
        '(load = local) => load("inert")',
        '(load = local): string => load("inert")',
        'load => load("inert")',
    ],
)
def test_runtime_package_scanner_scopes_expression_arrow_parameter_shadows(
    tmp_path: Path,
    arrow: str,
) -> None:
    source = f'const load = require; const f = {arrow}; load("real");'

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ("real",)


def test_runtime_package_scanner_rejects_expression_arrow_loader_lifetime(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        "const run = (load = createRequire(import.meta.url)): "
        '{ x: string; y: number } => load("inside"); '
        'load("outside");'
    )

    with pytest.raises(TypeScriptWorkerError, match="expression-bodied arrow"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")


def test_runtime_package_scanner_rejects_late_assignment_to_destructured_var(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module"; '
        "if (ok) { var { deeply, nested, destructured, load } = source; "
        "load = createRequire(import.meta.url); } "
        'load("outside");'
    )

    with pytest.raises(TypeScriptWorkerError, match="destructured var binding"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")


def test_runtime_package_scanner_bounds_nested_arrow_scope_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = typescript_worker._declaration_body_head
    scanned_tokens = 0

    def counted(tokens: Sequence[tuple[str, str]]) -> str | None:
        nonlocal scanned_tokens
        scanned_tokens += len(tokens)
        return original(tokens)

    monkeypatch.setattr(typescript_worker, "_declaration_body_head", counted)
    expression = "0"
    for index in range(1_000):
        expression = f"(value{index} = {expression}) => {{}}"
    source = "const nested = " + expression

    assert _runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts") == ()
    assert scanned_tokens < len(source) * 10


@pytest.mark.parametrize(
    "value",
    [
        '`${load("inside-template")}`',
        '<A dep={load("inside-jsx")} />',
    ],
)
def test_runtime_package_scanner_preserves_embedded_expression_lexical_scope(
    tmp_path: Path,
    value: str,
) -> None:
    inside = "inside-jsx" if value.startswith("<") else "inside-template"
    source = (
        'import { createRequire } from "node:module"; '
        "function scoped() { const load = createRequire(import.meta.url); "
        f"return {value}; }} "
        'load("outside-scope");'
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.tsx")) == {
        "node:module",
        inside,
    }


def test_runtime_package_scanner_resolves_nearest_same_name_capability(
    tmp_path: Path,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "{\n"
        "  const load = createRequire(import.meta.url);\n"
        '  load("outer-before");\n'
        "  {\n"
        "    const load = createRequire(import.meta.url);\n"
        '    load("inner");\n'
        "  }\n"
        '  load("outer-after");\n'
        "}\n"
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.js")) == {
        "inner",
        "node:module",
        "outer-after",
        "outer-before",
    }


def test_runtime_package_scanner_scales_across_same_name_sibling_bindings(
    tmp_path: Path,
) -> None:
    dependency_count = 400
    source = 'import { createRequire } from "node:module";\n' + "\n".join(
        f'{{ const load = createRequire(import.meta.url); load("sibling-{index}"); }}'
        for index in range(dependency_count)
    )

    specifiers = set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.js"))

    assert specifiers == {
        "node:module",
        *(f"sibling-{index}" for index in range(dependency_count)),
    }


def test_runtime_package_scanner_updates_nested_scope_indices_incrementally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    depth = 100
    original_index = typescript_worker._ScopeCapabilityIndex
    created = 0
    additions = 0

    class CountingIndex(original_index):
        def __init__(
            self,
            bindings: Mapping[int, tuple[int, str]],
            scope_open: Sequence[int],
            scope_end_exclusive: Sequence[int],
        ) -> None:
            nonlocal created
            created += 1
            super().__init__(bindings, scope_open, scope_end_exclusive)

        def add(self, *, start: int, end: int, capability: str) -> None:
            nonlocal additions
            additions += 1
            super().add(start=start, end=end, capability=capability)

    monkeypatch.setattr(typescript_worker, "_ScopeCapabilityIndex", CountingIndex)
    chunks = ['import { createRequire } from "node:module";']
    for index in range(depth):
        chunks.append("{ const load = createRequire(import.meta.url); {{{")
        chunks.append(f'const alias{index} = load; alias{index}("nested-{index}");')
    chunks.append("}" * (depth * 4))

    specifiers = set(
        _runtime_module_specifiers("\n".join(chunks), source_path=tmp_path / "runtime.js")
    )

    assert specifiers == {
        "node:module",
        *(f"nested-{index}" for index in range(depth)),
    }
    assert created == 2
    assert additions == depth + 1


def test_create_require_scope_scan_does_not_copy_every_token_prefix(tmp_path: Path) -> None:
    tokens = _AccessCountingTokens(
        [
            token
            for _ in range(2_000)
            for token in (
                ("identifier", "call"),
                ("punctuation", "("),
                ("punctuation", ")"),
                ("punctuation", ";"),
            )
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.sliced_items < len(tokens) * 20


def test_create_require_scope_scan_keeps_deep_nesting_memory_linear(tmp_path: Path) -> None:
    depth = 2_000
    tokens = tuple(
        [("punctuation", "{")] * depth
        + [("identifier", "noop"), ("punctuation", ";")]
        + [("punctuation", "}")] * depth
    )

    tracemalloc.start()
    try:
        assert (
            _create_require_module_specifiers(
                tokens,
                source_path=tmp_path / "runtime.js",
            )
            == ()
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 16 * 1024 * 1024


def test_create_require_scope_lookup_bounds_sparse_capability_cache(tmp_path: Path) -> None:
    capability_count = 800
    source = (
        'import { createRequire } from "node:module";\n'
        + "\n".join(
            f"const load{index} = createRequire(import.meta.url);"
            for index in range(capability_count)
        )
        + "\n"
        + "{" * capability_count
        + "\n"
        + "\n".join(f'load{index}("deep-{index}");' for index in range(capability_count))
        + "\n"
        + "}" * capability_count
    )
    tokens = typescript_worker._runtime_javascript_tokens(
        source,
        source_path=tmp_path / "runtime.js",
    )

    tracemalloc.start()
    try:
        specifiers = _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert set(specifiers) == {f"deep-{index}" for index in range(capability_count)}
    assert peak < 16 * 1024 * 1024


def test_create_require_scope_scan_does_not_rescan_statement_colons(tmp_path: Path) -> None:
    tokens = _AccessCountingTokens(
        [
            token
            for index in range(500)
            for token in (
                ("identifier", f"outer{index}"),
                ("punctuation", ":"),
                ("identifier", f"inner{index}"),
                ("punctuation", ":"),
                ("identifier", "function"),
                ("identifier", f"fn{index}"),
                ("punctuation", "("),
                ("punctuation", ")"),
                ("punctuation", "{"),
                ("punctuation", "}"),
                ("identifier", "case"),
                ("identifier", f"condition{index}"),
                ("punctuation", ":"),
                ("identifier", "function"),
                ("identifier", f"caseFn{index}"),
                ("punctuation", "("),
                ("punctuation", ")"),
                ("punctuation", "{"),
                ("punctuation", "}"),
                ("identifier", "else"),
                ("identifier", f"elseLabel{index}"),
                ("punctuation", ":"),
                ("punctuation", "{"),
                ("punctuation", "}"),
                ("identifier", "do"),
                ("identifier", f"doLabel{index}"),
                ("punctuation", ":"),
                ("punctuation", "{"),
                ("punctuation", "}"),
            )
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


def test_create_require_scope_scan_does_not_rescan_typed_parameter_prefixes(
    tmp_path: Path,
) -> None:
    tokens = _AccessCountingTokens(
        [
            ("identifier", "function"),
            ("identifier", "typed"),
            ("punctuation", "("),
            *[
                token
                for index in range(500)
                for token in (
                    ("identifier", f"arg{index}"),
                    ("punctuation", ":"),
                    ("identifier", "string"),
                    ("punctuation", ","),
                    ("identifier", "public"),
                    ("identifier", f"property{index}"),
                    ("punctuation", ":"),
                    ("identifier", "string"),
                    ("punctuation", ","),
                    ("punctuation", "@"),
                    ("identifier", "factory"),
                    ("punctuation", "("),
                    ("punctuation", ")"),
                    ("punctuation", "."),
                    ("identifier", "decorator"),
                    ("punctuation", "<"),
                    ("identifier", "string"),
                    ("punctuation", ">"),
                    ("punctuation", "("),
                    ("punctuation", ")"),
                    ("identifier", f"decorated{index}"),
                    ("punctuation", ":"),
                    ("identifier", "string"),
                    ("punctuation", ","),
                )
            ],
            ("punctuation", ")"),
            ("punctuation", "{"),
            ("punctuation", "}"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.ts",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        (("Type", "=", "value"), 2),
        (("(", "Inner", "=", "ignored", ")", "=", "value"), 6),
        (("[", "Inner", "=", "ignored", "]", "=", "value"), 6),
        (("{", "Inner", "=", "ignored", "}", "=", "value"), 6),
        (("<", "Inner", "=", "ignored", ">", "=", "value"), 6),
        (("(", "Inner", "=", "ignored"), -1),
        (("(", "Inner", "]", "=", "ignored", ")", "=", "value"), 7),
        (("Type", ",", "value", "=", "ignored"), -1),
        (("Type", ")", "=", "ignored"), -1),
        (("Type", "="), 2),
    ],
)
def test_annotation_initializer_starts_match_tolerant_forward_scan(
    values: tuple[str, ...],
    expected: int,
) -> None:
    punctuation = {"(", ")", "[", "]", "{", "}", "<", ">", "=", ","}
    tokens = tuple(
        ("punctuation" if value in punctuation else "identifier", value) for value in values
    )

    assert _annotation_initializer_starts(tokens)[0] == expected


def test_create_require_scope_scan_does_not_rescan_nested_type_suffixes(
    tmp_path: Path,
) -> None:
    depth = 500
    tokens = _AccessCountingTokens(
        [
            ("identifier", "const"),
            ("identifier", "value"),
            ("punctuation", ":"),
            *[
                token
                for index in range(depth)
                for token in (
                    ("punctuation", "("),
                    ("identifier", f"arg{index}"),
                    ("punctuation", ":"),
                )
            ],
            ("identifier", "Result"),
            *[
                token
                for _index in range(depth)
                for token in (
                    ("punctuation", ")"),
                    ("punctuation", "=>"),
                    ("identifier", "Result"),
                )
            ],
            ("punctuation", "="),
            ("identifier", "ordinaryValue"),
            ("punctuation", ";"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.ts",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


def test_runtime_package_scanner_tracks_loader_after_nested_function_type(
    tmp_path: Path,
) -> None:
    annotation = "Result"
    for index in range(100):
        annotation = f"(arg{index}: {annotation}) => Result"
    source = (
        'import { createRequire } from "node:module";\n'
        f"const load: ({annotation}) = createRequire(import.meta.url);\n"
        'load("nested-type-dependency");\n'
    )

    assert set(_runtime_module_specifiers(source, source_path=tmp_path / "runtime.ts")) == {
        "nested-type-dependency",
        "node:module",
    }


def test_create_require_scope_scan_does_not_rescan_braced_expressions(tmp_path: Path) -> None:
    tokens = _AccessCountingTokens(
        [
            token
            for _ in range(500)
            for token in (
                ("punctuation", "("),
                ("punctuation", "{"),
                ("punctuation", "}"),
                ("punctuation", ")"),
            )
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 50


def test_create_require_scope_scan_does_not_rescan_control_or_asi_labels(
    tmp_path: Path,
) -> None:
    values: list[tuple[str, str]] = []
    line_breaks_before: list[bool] = []
    for index in range(500):
        control = (
            ("identifier", "if"),
            ("punctuation", "("),
            ("identifier", "ok"),
            ("punctuation", ")"),
            ("identifier", f"controlled{index}"),
            ("punctuation", ":"),
            ("punctuation", "{"),
            ("punctuation", "}"),
        )
        values.extend(control)
        line_breaks_before.extend(False for _ in control)
        call_and_label = (
            ("identifier", "call"),
            ("punctuation", "("),
            ("punctuation", ")"),
            ("identifier", f"afterCall{index}"),
            ("punctuation", ":"),
            ("punctuation", "{"),
            ("punctuation", "}"),
        )
        values.extend(call_and_label)
        line_breaks_before.extend((False, False, False, True, False, False, False))
    tokens = _AccessCountingTokens(values)
    tokens.line_breaks_before = tuple(line_breaks_before)

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


def test_create_require_scope_scan_does_not_rescan_conditional_colons(
    tmp_path: Path,
) -> None:
    tokens = _AccessCountingTokens(
        [
            ("identifier", "const"),
            ("identifier", "values"),
            ("punctuation", "="),
            ("punctuation", "["),
            *[
                token
                for index in range(500)
                for token in (
                    ("identifier", f"condition{index}"),
                    ("punctuation", "?"),
                    ("identifier", f"left{index}"),
                    ("punctuation", "+"),
                    ("identifier", f"right{index}"),
                    ("punctuation", ":"),
                    ("punctuation", "{"),
                    ("punctuation", "}"),
                    ("punctuation", ","),
                )
            ],
            ("punctuation", "]"),
            ("punctuation", ";"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


def test_create_require_scope_scan_does_not_rescan_object_property_colons(
    tmp_path: Path,
) -> None:
    tokens = _AccessCountingTokens(
        [
            ("identifier", "const"),
            ("identifier", "values"),
            ("punctuation", "="),
            ("punctuation", "{"),
            *[
                token
                for index in range(500)
                for token in (
                    ("identifier", f"property{index}"),
                    ("punctuation", ":"),
                    ("punctuation", "{"),
                    ("punctuation", "}"),
                    ("punctuation", ","),
                )
            ],
            ("punctuation", "}"),
            ("punctuation", ";"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 100


def test_runtime_scanners_do_not_rescan_object_value_colons(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = typescript_worker._colon_opens_statement_block
    scanned_prefix_items = 0

    def count_scanned_prefix(
        tokens: list[tuple[str, str]],
        *,
        enclosing_statement_brace: bool | None,
        end: int | None = None,
        line_breaks_before: list[bool] | None = None,
    ) -> bool:
        nonlocal scanned_prefix_items
        scanned_prefix_items += len(tokens) if end is None else end
        return original(
            tokens,
            enclosing_statement_brace=enclosing_statement_brace,
            end=end,
            line_breaks_before=line_breaks_before,
        )

    monkeypatch.setattr(
        typescript_worker,
        "_colon_opens_statement_block",
        count_scanned_prefix,
    )
    sources = (
        "const values = ["
        + ",".join(f"condition{index} ? left{index} : {{}}" for index in range(500))
        + "];",
        "const values = {" + ",".join(f"property{index}: {{}}" for index in range(500)) + "};",
        "interface Values {\n"
        + "".join(f"method{index}(): {{value: string}}\n" for index in range(500))
        + "}",
    )
    for source in sources:
        before = scanned_prefix_items
        tokens = typescript_worker._runtime_javascript_tokens(
            source,
            source_path=tmp_path / "runtime.js",
        )
        assert (
            _create_require_module_specifiers(
                tokens,
                source_path=tmp_path / "runtime.js",
            )
            == ()
        )

        assert tokens
        assert scanned_prefix_items - before < len(tokens) * 20


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


@pytest.mark.parametrize(
    "wrapper",
    [
        "class Wrapper { require() { return load; } }",
        "class Wrapper { get other() { return load; } }",
        "class Wrapper { get require() { if (enabled) { return load; } } }",
    ],
)
def test_runtime_package_scanner_only_allows_direct_require_getter_returns(
    tmp_path: Path,
    wrapper: str,
) -> None:
    source = (
        'import { createRequire } from "node:module";\n'
        "const load = createRequire(import.meta.url);\n"
        f"{wrapper}\n"
    )

    with pytest.raises(TypeScriptWorkerError, match="passes or ambiguously uses loader alias"):
        _runtime_module_specifiers(source, source_path=tmp_path / "runtime.js")


def test_create_require_scope_scan_does_not_rescan_require_getter_bodies(
    tmp_path: Path,
) -> None:
    return_count = 600
    tokens = _AccessCountingTokens(
        [
            ("identifier", "import"),
            ("punctuation", "{"),
            ("identifier", "createRequire"),
            ("punctuation", "}"),
            ("identifier", "from"),
            ("string", "node:module"),
            ("punctuation", ";"),
            ("identifier", "const"),
            ("identifier", "load"),
            ("punctuation", "="),
            ("identifier", "createRequire"),
            ("punctuation", "("),
            ("identifier", "import"),
            ("punctuation", "."),
            ("identifier", "meta"),
            ("punctuation", "."),
            ("identifier", "url"),
            ("punctuation", ")"),
            ("punctuation", ";"),
            ("identifier", "const"),
            ("identifier", "wrapper"),
            ("punctuation", "="),
            ("punctuation", "{"),
            ("identifier", "get"),
            ("identifier", "require"),
            ("punctuation", "("),
            ("punctuation", ")"),
            ("punctuation", "{"),
            *[
                token
                for _ in range(return_count)
                for token in (
                    ("identifier", "return"),
                    ("identifier", "load"),
                    ("punctuation", ";"),
                )
            ],
            ("punctuation", "}"),
            ("punctuation", "}"),
            ("punctuation", ";"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
            shadowed_native_loaders=frozenset(),
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 40


def test_create_require_scope_scan_does_not_rescan_semicolonless_getter_returns(
    tmp_path: Path,
) -> None:
    return_count = 1_000
    tokens = _AccessCountingTokens(
        [
            ("identifier", "const"),
            ("identifier", "load"),
            ("punctuation", "="),
            ("identifier", "require"),
            ("punctuation", ";"),
            ("identifier", "const"),
            ("identifier", "wrapper"),
            ("punctuation", "="),
            ("punctuation", "{"),
            ("identifier", "get"),
            ("identifier", "require"),
            ("punctuation", "("),
            ("punctuation", ")"),
            ("punctuation", "{"),
            *[
                token
                for _ in range(return_count)
                for token in (
                    ("identifier", "return"),
                    ("identifier", "load"),
                )
            ],
            ("punctuation", "}"),
            ("punctuation", "}"),
            ("punctuation", ";"),
        ]
    )

    assert (
        _create_require_module_specifiers(
            tokens,
            source_path=tmp_path / "runtime.js",
            shadowed_native_loaders=frozenset(),
        )
        == ()
    )
    assert tokens.indexed_items < len(tokens) * 40


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
