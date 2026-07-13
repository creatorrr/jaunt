from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from jaunt.config import load_config
from jaunt.cli import main
from jaunt.errors import JauntGenerationError
from jaunt.generate.base import GenerationRequest, GeneratorBackend, ModuleSpecContext
from jaunt.typescript.builder import run_build, run_sync, worker_session
from jaunt.typescript.contracts import (
    _add_contract_tag,
    _project_contract,
    _remove_contract_tag,
    run_eject,
)
from jaunt.typescript.migrate import apply_typescript_migration, plan_typescript_migration
from jaunt.typescript.design import run_design
from jaunt.typescript.status import run_check, run_status
from jaunt.typescript.tester import run_test


class _SlugGenerator(GeneratorBackend):
    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        assert request.language == "ts"
        if request.target_path == "src/__generated__/slug.ts":
            return (
                "const __jaunt_impl_slugify = (title: string): string =>\n"
                '  title.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-")'
                '.replace(/^-|-$/g, "");\n',
                None,
                (),
            )
        if request.target_path == "src/__generated__/case.ts":
            return (
                "const __jaunt_impl_upper = (value: string): string => value.toUpperCase();\n",
                None,
                (),
            )
        raise AssertionError(request.target_path)


class _GreetingGenerator(GeneratorBackend):
    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        assert request.language == "ts"
        assert request.target_path == "src/__generated__/index.ts"
        return (
            "const __jaunt_impl_greet = (name: string): string => `Hello, ${name}!`;\n",
            None,
            (),
        )


class _DesignGenerator(GeneratorBackend):
    def __init__(self, declaration: str) -> None:
        self.declaration = declaration

    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        assert request.kind == "design"
        return self.declaration, None, ()


class _DependencyGenerator(GeneratorBackend):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.targets: list[str] = []
        self.base_variant = 1
        self.expect_absent = True

    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        self.targets.append(request.target_path)
        if self.expect_absent:
            assert not (self.root / "src/__generated__/base.ts").exists()
            assert not (self.root / "src/__generated__/slug.ts").exists()
        if request.target_path == "src/__generated__/base.ts":
            expression = "value.trim()" if self.base_variant == 1 else "`${value}`.trim()"
            return (
                f"const __jaunt_impl_base = (value: string): string => {expression};\n",
                None,
                (),
            )
        if request.target_path == "src/__generated__/slug.ts":
            dependencies = request.context_files["_context/dependencies.json"]
            assert '"moduleId": "ts:src/base"' in dependencies
            assert '"facadeSpecifier": "../base.js"' in dependencies
            assert "_context/dependency_0.api.ts" in request.context_files
            return (
                'import { base } from "../base.js";\n'
                "const __jaunt_impl_slugify = (title: string): string => "
                'base(title).toLowerCase().replace(/\\s+/g, "-");\n',
                None,
                (),
            )
        raise AssertionError(request.target_path)


class _ProjectReferenceGenerator(GeneratorBackend):
    def __init__(self) -> None:
        self.targets: list[str] = []

    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        self.targets.append(request.target_path)
        if request.target_path == "packages/core/src/normalize/__generated__/index.ts":
            return (
                "const __jaunt_impl_normalize = (value: string): string => value.trim();\n",
                None,
                (),
            )
        if request.target_path == "packages/app/src/slug/__generated__/index.ts":
            dependencies = request.context_files["_context/dependencies.json"]
            assert '"moduleId": "ts:packages/core/src/normalize/index"' in dependencies
            assert '"facadeSpecifier": "@core/normalize/index.js"' in dependencies
            return (
                'import { normalize } from "@core/normalize/index.js";\n'
                "const __jaunt_impl_slugify = (value: string): string => "
                "normalize(value).toLowerCase();\n",
                None,
                (),
            )
        raise AssertionError(request.target_path)


class _EjectLifecycleGenerator(GeneratorBackend):
    async def generate_module(self, ctx: ModuleSpecContext, **_kwargs: Any):
        raise AssertionError(ctx)

    async def generate_request(self, request: GenerationRequest, **_kwargs: Any):
        if request.kind == "build":
            assert request.target_path == "src/__generated__/slug.ts"
            return (
                "const __jaunt_impl_slugify = (title: string): string =>\n"
                '  title.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-")'
                '.replace(/^-|-$/g, "");\n'
                "class __jaunt_impl_Store {\n"
                "  constructor() {}\n"
                '  get(): string { return "stored"; }\n'
                "}\n",
                None,
                (),
            )
        assert request.kind == "test"
        tier = str(request.cache_payload["tier"])
        expected = "hello-ejected" if tier == "example" else "already-clean"
        source_value = " Hello, Ejected! " if tier == "example" else "already-clean"
        held_out_probe = (
            'import { existsSync } from "node:fs";\n'
            'test("derived source is absent", () => { '
            'if (import.meta.url.includes("/__generated__/")) '
            'expect(existsSync(new URL("./slug.derived.test.ts", import.meta.url))).toBe(false); '
            "});\n"
            if tier == "example"
            else "// HELD-OUT-FILESYSTEM-SENTINEL\n"
        )
        return (
            'import { expect, test } from "vitest";\n'
            'import { slugify } from "../../src/slug.js";\n'
            + held_out_probe
            + f'test("{tier} eject behavior", () => '
            f'expect(slugify("{source_value}")).toBe("{expected}"));\n',
            None,
            (),
        )


def _copy_tooling(root: Path) -> None:
    repository = Path(__file__).resolve().parents[1]
    package = repository / "packages" / "jaunt-ts"
    # The official side-by-side package resolves its stable 6.x API through
    # @typescript/old; install those exact bytes under the ordinary project-local
    # `typescript` coordinate expected from adopters.
    compiler = package / "node_modules" / "@typescript" / "old"
    if not (package / "dist" / "worker" / "main.js").is_file() or not compiler.is_dir():
        pytest.skip("real TypeScript worker dependencies are not built; run npm ci && npm test")

    installed = root / "node_modules" / "@usejaunt" / "ts"
    installed.mkdir(parents=True)
    shutil.copy2(package / "package.json", installed / "package.json")
    shutil.copytree(package / "dist", installed / "dist")
    if (package / "README.md").is_file():
        shutil.copy2(package / "README.md", installed / "README.md")
    shutil.copytree(compiler, root / "node_modules" / "typescript")


def _write_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "package.json").write_text(
        """{
  "name": "jaunt-ts-real-worker-smoke",
  "private": true,
  "type": "module",
  "devDependencies": {
    "@usejaunt/ts": "0.1.0-alpha.0",
    "typescript": "6.0.2"
  }
}
""",
        encoding="utf-8",
    )
    (root / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
tool_owner = "."

[codex]
model = "gpt-5.6-sol"
""",
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text(
        """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "declaration": true,
    "rootDir": "src",
    "outDir": "dist",
    "verbatimModuleSyntax": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["**/*.jaunt.ts", "**/*.jaunt.tsx"]
}
""",
        encoding="utf-8",
    )
    (root / "src" / "slug.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Lowercase ASCII words and join them with one hyphen. */
export function slugify(title: string): string {
  return jaunt.magic();
}
""",
        encoding="utf-8",
    )
    (root / "src" / "case.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Return the uppercase form of `value`. */
export function upper(value: string): string {
  return jaunt.magic();
}
""",
        encoding="utf-8",
    )
    (root / "src" / "app.ts").write_text(
        """import { upper } from "./case.js";
import { slugify } from "./slug.js";

if (slugify(" Hello, Worker! ") !== "hello-worker" || upper("ok") !== "OK") {
  throw new Error("unexpected slug");
}
""",
        encoding="utf-8",
    )


def _write_design_project(root: Path) -> Path:
    _write_project(root)
    (root / "src/case.jaunt.ts").unlink()
    (root / "src/app.ts").unlink()
    spec = root / "src/slug.jaunt.ts"
    spec.write_text(
        """import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Design the public conversion API. @jauntDesign */
export declare function convert(value: string): string;
""",
        encoding="utf-8",
    )
    return spec


def _write_eject_lifecycle_project(root: Path) -> None:
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "package.json").write_text(
        """{
  "name": "jaunt-ejected-lifecycle-fixture",
  "version": "1.0.0",
  "private": true,
  "type": "module",
  "files": ["dist"],
  "exports": {
    ".": {
      "types": "./dist/slug.d.ts",
      "import": "./dist/slug.js"
    }
  },
  "devDependencies": {
    "@usejaunt/ts": "0.1.0-alpha.0",
    "typescript": "6.0.2",
    "vitest": "4.1.10"
  }
}
""",
        encoding="utf-8",
    )
    (root / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["src"]
test_roots = ["tests"]
projects = ["tsconfig.json"]
test_projects = ["tsconfig.test.json"]
tool_owner = "."

[codex]
model = "gpt-5.6-sol"
""",
        encoding="utf-8",
    )
    (root / "tsconfig.json").write_text(
        """{
  "compilerOptions": {
    "target": "ES2022",
    "module": "NodeNext",
    "moduleResolution": "NodeNext",
    "strict": true,
    "declaration": true,
    "rootDir": "src",
    "outDir": "dist",
    "verbatimModuleSyntax": true
  },
  "include": ["src/**/*.ts"],
  "exclude": ["**/*.jaunt.ts", "**/*.jaunt.tsx"]
}
""",
        encoding="utf-8",
    )
    (root / "tsconfig.test.json").write_text(
        """{
  "extends": "./tsconfig.json",
  "compilerOptions": {
    "noEmit": true,
    "rootDir": ".",
    "types": ["vitest/globals", "node"]
  },
  "include": ["src/**/*.ts", "tests/**/*.ts"],
  "exclude": ["**/*.jaunt.ts", "**/*.jaunt-test.ts"]
}
""",
        encoding="utf-8",
    )
    (root / "src/slug.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
import type { External } from "./model.js";
jaunt.magicModule();
/** A public payload backed by an ordinary imported type. */
export interface Payload { value: External; }
export interface NestedShape<T extends { meta: { id: string; }; }> {
  /** A comment with misleading declaration tokens: } ; */
  render(value: { text: `prefix;${string}`; }): {
    done: boolean;
    callback: () => { ok: true; };
  };
}
export type StructuredAlias<T> = {
  nested: { run(input: T): { value: `result;${string}`; }; };
  callback: (input: { note: "};"; }) => { output: `prefix;${string}`; };
  /* Another misleading declaration ending: ; } */
  marker: "/* ; */";
};
/** Lowercase ASCII words and join them with one hyphen. */
export function slugify(title: string): string { return jaunt.magic(); }
/** A tiny generated store. */
export class Store {
  /** Read the stored value. */
  get(): string { return jaunt.magic(); }
}
""",
        encoding="utf-8",
    )
    (root / "src/model.ts").write_text(
        "export interface External { readonly value: string; }\n",
        encoding="utf-8",
    )
    (root / "tests/slug.jaunt-test.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
import { slugify } from "../src/slug.jaunt.js";
jaunt.magicModule();
/** Cover punctuation, surrounding whitespace, and an already-clean slug. */
export function slugExamples(): void {
  jaunt.testSpec({ targets: [slugify] });
}
""",
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_real_worker_contract_ranges_round_trip_astral_unicode(tmp_path: Path) -> None:
    _copy_tooling(tmp_path)
    _write_project(tmp_path)
    source = tmp_path / "src/contract.ts"
    original = (
        'const decoy = "🧭";\n'
        "/** Navigate toward 🌌 without changing authored Unicode. */\n"
        "export function navigate(value: string): string { return value; }\n"
    )
    source.write_text(original, encoding="utf-8")
    config = load_config(root=tmp_path)

    async with worker_session(tmp_path, config) as (client, _initialized):
        projection = await _project_contract(client, tmp_path, source, "navigate", original)
        # TypeScript positions count the compass as a two-code-unit surrogate pair.
        assert projection["docsStart"] == original.index("/**") + 1
        adopted = _add_contract_tag(original, "navigate", projection)
        adopted_projection = await _project_contract(
            client,
            tmp_path,
            source,
            "navigate",
            adopted,
        )
        restored = _remove_contract_tag(adopted, "navigate", adopted_projection)

    assert "Navigate toward 🌌" in adopted
    assert adopted.count("@jauntContract") == 1
    assert restored == original


@pytest.mark.asyncio
async def test_real_worker_design_rejects_extra_declaration_byte_exact(
    tmp_path: Path,
) -> None:
    _copy_tooling(tmp_path)
    spec = _write_design_project(tmp_path)
    original = spec.read_bytes()
    config = load_config(root=tmp_path)
    generator = _DesignGenerator(
        "export interface Extra { readonly value: string; }\n\n"
        "/** Convert one planned value. */\n"
        "export declare function convert(value: Extra): string;\n"
    )

    preview = await run_design(
        tmp_path,
        config,
        target_id="ts:src/slug#convert",
        generator=generator,
    )

    assert preview.exit_code == 3
    assert any("selected exported declaration" in item.message for item in preview.diagnostics)
    assert spec.read_bytes() == original
    assert not tuple((tmp_path / ".jaunt/design-proposals").glob("*.json"))


@pytest.mark.asyncio
async def test_real_worker_design_apply_rolls_back_unresolved_type_byte_exact(
    tmp_path: Path,
) -> None:
    _copy_tooling(tmp_path)
    spec = _write_design_project(tmp_path)
    original = spec.read_bytes()
    config = load_config(root=tmp_path)
    generator = _DesignGenerator(
        "/** Convert one planned value. */\n"
        "export declare function convert(value: MissingType): string;\n"
    )

    preview = await run_design(
        tmp_path,
        config,
        target_id="ts:src/slug#convert",
        generator=generator,
    )
    assert preview.exit_code == 0

    with pytest.raises(JauntGenerationError, match=r"semantic TypeScript validation.*2304"):
        await run_design(
            tmp_path,
            config,
            target_id="ts:src/slug#convert",
            apply=True,
            generator=generator,
        )

    assert spec.read_bytes() == original
    assert not tuple((tmp_path / ".jaunt/transactions").glob("design-*.json"))


@pytest.mark.asyncio
async def test_real_worker_design_apply_accepts_associated_type_import(
    tmp_path: Path,
) -> None:
    _copy_tooling(tmp_path)
    spec = _write_design_project(tmp_path)
    (tmp_path / "src/model.ts").write_text(
        "export interface PlannedInput { readonly value: string; }\n",
        encoding="utf-8",
    )
    config = load_config(root=tmp_path)
    generator = _DesignGenerator(
        'import { type PlannedInput } from "./model.js";\n\n'
        "/** Convert one planned value. */\n"
        "export declare function convert(value: PlannedInput): string;\n"
    )

    preview = await run_design(
        tmp_path,
        config,
        target_id="ts:src/slug#convert",
        generator=generator,
    )
    assert '+import { type PlannedInput } from "./model.js";' in preview.patch
    applied = await run_design(
        tmp_path,
        config,
        target_id="ts:src/slug#convert",
        apply=True,
        generator=generator,
    )

    assert applied.exit_code == 0
    assert applied.applied
    source = spec.read_text(encoding="utf-8")
    assert 'import { type PlannedInput } from "./model.js";' in source
    assert "import type { type PlannedInput }" not in source
    assert "export function convert(value: PlannedInput): string" in source
    assert "return jaunt.magic();" in source
    assert "@jauntDesign" not in source


@pytest.mark.asyncio
async def test_real_worker_migration_repairs_unbuilt_artifacts_without_model(
    tmp_path: Path,
) -> None:
    _copy_tooling(tmp_path)
    _write_project(tmp_path)
    config = load_config(root=tmp_path)

    plan = await plan_typescript_migration(tmp_path, config)

    assert not plan.blocked
    assert {action.kind for action in plan.actions} == {
        "api-mirror",
        "facade",
        "placeholder",
        "sidecar",
    }
    assert len(apply_typescript_migration(plan)) == 8
    assert (await run_status(tmp_path, config)).unbuilt == frozenset({"ts:src/case", "ts:src/slug"})

    again = await plan_typescript_migration(tmp_path, config)
    assert again.actions == ()
    assert again.writes == ()


@pytest.mark.asyncio
async def test_python_adapter_drives_real_worker_compile_and_runtime(tmp_path: Path) -> None:
    _copy_tooling(tmp_path)
    _write_project(tmp_path)
    config = load_config(root=tmp_path)

    synchronized = await run_sync(tmp_path, config)
    assert synchronized.exit_code == 0
    assert synchronized.placeholders == (
        "src/__generated__/case.ts",
        "src/__generated__/slug.ts",
    )
    assert (await run_status(tmp_path, config)).unbuilt == frozenset({"ts:src/case", "ts:src/slug"})

    built = await run_build(tmp_path, config, generator=_SlugGenerator())
    assert built.exit_code == 0, built.failed
    assert built.generated == frozenset({"ts:src/case", "ts:src/slug"})
    assert (await run_status(tmp_path, config)).fresh == frozenset({"ts:src/case", "ts:src/slug"})
    assert (await run_check(tmp_path, config, magic_only=True)).exit_code == 0

    compiler = tmp_path / "node_modules" / "typescript" / "bin" / "tsc"
    subprocess.run(
        ["node", str(compiler), "-p", "tsconfig.json"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["node", "dist/app.js"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    emitted = "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "dist").rglob("*.js")
    )
    assert "@usejaunt/ts" not in emitted
    assert ".jaunt.js" not in emitted


@pytest.mark.asyncio
async def test_typescript_init_scaffold_syncs_typechecks_and_runs(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text(
        """{
  "name": "jaunt-ts-init-smoke",
  "private": true,
  "type": "module",
  "devDependencies": {
    "@usejaunt/ts": "0.1.0-alpha.0",
    "typescript": "6.0.2"
  }
}
""",
        encoding="utf-8",
    )
    _copy_tooling(tmp_path)
    package_root = Path(__file__).resolve().parents[1] / "packages/jaunt-ts"
    (tmp_path / "node_modules/@types").mkdir()
    (tmp_path / "node_modules/@types/node").symlink_to(
        package_root / "node_modules/@types/node", target_is_directory=True
    )
    assert main(["init", "--language", "ts", "--root", str(tmp_path)]) == 0
    config = load_config(root=tmp_path)

    synchronized = await run_sync(tmp_path, config)
    assert synchronized.exit_code == 0, synchronized.failed
    assert synchronized.placeholders == ("src/__generated__/index.ts",)
    assert (tmp_path / "src/__generated__/index.api.ts").is_file()
    assert (tmp_path / "src/__generated__/index.jaunt.json").is_file()

    built = await run_build(tmp_path, config, generator=_GreetingGenerator())
    assert built.exit_code == 0, built.failed
    assert built.generated == frozenset({"ts:src/index"})

    (tmp_path / "src/app.ts").write_text(
        'import { greet } from "./index.js";\n'
        'if (greet("Ada") !== "Hello, Ada!") throw new Error("bad greeting");\n',
        encoding="utf-8",
    )
    compiler = tmp_path / "node_modules/typescript/bin/tsc"
    subprocess.run(
        ["node", str(compiler), "-p", "tsconfig.json", "--pretty", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["node", "dist/app.js"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert (tmp_path / "dist/index.context.js").is_file()
    emitted_facade = (tmp_path / "dist/index.js").read_text(encoding="utf-8")
    assert 'export * from "./index.context.js";' in emitted_facade
    assert "@usejaunt/ts" not in "\n".join(
        path.read_text(encoding="utf-8") for path in (tmp_path / "dist").rglob("*.js")
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "force_permission_fallback",
    [False, True],
    ids=("automatic-sandbox", "node-permission-fallback"),
)
async def test_real_worker_isolated_magic_eject_packs_and_runs_without_jaunt_tooling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    force_permission_fallback: bool,
) -> None:
    if force_permission_fallback:
        monkeypatch.setattr("jaunt.typescript.tester._bubblewrap_executable", lambda _env: None)
    _copy_tooling(tmp_path)
    package_root = Path(__file__).resolve().parents[1] / "packages/jaunt-ts"
    vitest = package_root / "node_modules/vitest"
    if not vitest.is_dir():
        pytest.skip("Vitest is not installed in packages/jaunt-ts")
    (tmp_path / "node_modules/vitest").symlink_to(vitest, target_is_directory=True)
    node_types = package_root / "node_modules/@types/node"
    (tmp_path / "node_modules/@types").mkdir()
    (tmp_path / "node_modules/@types/node").symlink_to(node_types, target_is_directory=True)
    _write_eject_lifecycle_project(tmp_path)
    config = load_config(root=tmp_path)
    generator = _EjectLifecycleGenerator()

    built = await run_build(tmp_path, config, generator=generator)
    assert built.exit_code == 0, built.failed
    tested = await run_test(tmp_path, config, no_build=True, generator=generator)
    assert tested.exit_code == 0, tested.failed
    assert "HELD-OUT-FILESYSTEM-SENTINEL" not in json.dumps(tested.runner)
    status_after_test = await run_status(tmp_path, config, target_ids=("ts:src/slug",))
    assert status_after_test.fresh == frozenset({"ts:src/slug"}), status_after_test.invalid

    ejected = await run_eject(tmp_path, config, target="ts:src/slug")
    assert ejected.exit_code == 0, ejected.diagnostics
    ordinary = (tmp_path / "src/slug.ts").read_text(encoding="utf-8")
    assert "export const slugify" in ordinary
    assert "__jaunt" not in ordinary
    assert "@usejaunt" not in ordinary
    assert "jaunt:" not in ordinary
    assert "export interface NestedShape" in ordinary
    assert 'import type { External } from "./model.js";' in ordinary
    assert "export interface Payload { value: External; }" in ordinary
    assert "callback: () => { ok: true; };" in ordinary
    assert "export type StructuredAlias" in ordinary
    assert "output: `prefix;${string}`;" in ordinary
    assert "Another misleading declaration ending: ; }" in ordinary
    assert "A tiny generated store." in ordinary
    assert "Read the stored value." in ordinary
    assert "export class Store" in ordinary
    assert not (tmp_path / "src/slug.jaunt.ts").exists()
    assert not any(path.is_file() for path in (tmp_path / "src/__generated__").rglob("*"))
    for tier in ("example", "derived"):
        test_source = (tmp_path / f"tests/slug.{tier}.test.ts").read_text(encoding="utf-8")
        assert 'from "../src/slug.js"' in test_source
        assert "jaunt:" not in test_source

    compiler = tmp_path / "node_modules/typescript/bin/tsc"
    subprocess.run(
        ["node", str(compiler), "-p", "tsconfig.json", "--pretty", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        [
            "node",
            str(vitest / "vitest.mjs"),
            "run",
            "tests/slug.example.test.ts",
            "tests/slug.derived.test.ts",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    emitted = {
        path.relative_to(tmp_path).as_posix(): path.read_text(encoding="utf-8")
        for path in (tmp_path / "dist").rglob("*")
        if path.is_file()
    }
    assert {"dist/slug.js", "dist/slug.d.ts"}.issubset(emitted)
    assert not any(
        marker in source
        for source in emitted.values()
        for marker in ("@usejaunt", ".jaunt", "__generated__", "jaunt:", "__jaunt")
    )

    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    assert npm is not None
    packed = json.loads(
        subprocess.run(
            [npm, "pack", "--json", "--ignore-scripts"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    )
    tarball = tmp_path / packed[0]["filename"]
    consumer = tmp_path / "consumer"
    consumer.mkdir()
    (consumer / "package.json").write_text(
        '{"name":"clean-consumer","private":true,"type":"module"}\n',
        encoding="utf-8",
    )
    subprocess.run(
        [
            npm,
            "install",
            "--offline",
            "--ignore-scripts",
            "--no-audit",
            "--no-fund",
            str(tarball),
        ],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    )
    assert not (consumer / "node_modules/@usejaunt").exists()
    installed = consumer / "node_modules/jaunt-ejected-lifecycle-fixture"
    installed_paths = {
        path.relative_to(installed).as_posix() for path in installed.rglob("*") if path.is_file()
    }
    assert {"dist/slug.js", "dist/slug.d.ts", "package.json"}.issubset(installed_paths)
    assert not any(".jaunt" in path or "__generated__" in path for path in installed_paths)
    installed_manifest = json.loads((installed / "package.json").read_text(encoding="utf-8"))
    assert "@usejaunt/ts" not in installed_manifest.get("dependencies", {})
    (consumer / "index.mjs").write_text(
        'import { slugify } from "jaunt-ejected-lifecycle-fixture";\n'
        'if (slugify(" Clean Consumer ") !== "clean-consumer") throw new Error("bad eject");\n',
        encoding="utf-8",
    )
    subprocess.run(
        ["node", "index.mjs"],
        cwd=consumer,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_real_worker_builds_same_project_dependency_batch_atomically(
    tmp_path: Path,
) -> None:
    _copy_tooling(tmp_path)
    _write_project(tmp_path)
    (tmp_path / "src/case.jaunt.ts").unlink()
    (tmp_path / "src/base.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Normalize one value before it is slugified. */
export function base(value: string): string { return jaunt.magic(); }
""",
        encoding="utf-8",
    )
    (tmp_path / "src/slug.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
import { base } from "./base.jaunt.js";
jaunt.magicModule();
/** Lowercase words normalized by base and join them with one hyphen. */
export function slugify(title: string): string {
  return jaunt.magic({ deps: [base] });
}
""",
        encoding="utf-8",
    )
    (tmp_path / "src/app.ts").write_text(
        'import { slugify } from "./slug.js";\n'
        'if (slugify("Hello World") !== "hello-world") throw new Error("bad slug");\n',
        encoding="utf-8",
    )
    config = load_config(root=tmp_path)
    generator = _DependencyGenerator(tmp_path)

    built = await run_build(tmp_path, config, generator=generator)

    assert built.exit_code == 0, built
    assert generator.targets == [
        "src/__generated__/base.ts",
        "src/__generated__/slug.ts",
    ]
    assert built.generated == frozenset({"ts:src/base", "ts:src/slug"})
    assert 'from "../base.js"' in (tmp_path / "src/__generated__/slug.ts").read_text(
        encoding="utf-8"
    )
    assert (await run_status(tmp_path, config)).fresh == frozenset({"ts:src/base", "ts:src/slug"})

    generator.targets.clear()
    generator.base_variant = 2
    generator.expect_absent = False
    rebuilt = await run_build(
        tmp_path,
        config,
        target_ids=("ts:src/base",),
        force=True,
        generator=generator,
    )
    assert rebuilt.exit_code == 0
    assert generator.targets == ["src/__generated__/base.ts"]
    assert (await run_status(tmp_path, config)).fresh == frozenset({"ts:src/base", "ts:src/slug"})

    base_spec = tmp_path / "src/base.jaunt.ts"
    base_spec.write_text(
        base_spec.read_text(encoding="utf-8").replace(
            "base(value: string): string",
            "base(value: string, suffix?: string): string",
        ),
        encoding="utf-8",
    )
    stale = await run_status(tmp_path, config)
    assert stale.stale == {
        "ts:src/base": "structural",
        "ts:src/slug": "structural",
    }


@pytest.mark.asyncio
async def test_real_worker_builds_solution_project_reference_batch(tmp_path: Path) -> None:
    _copy_tooling(tmp_path)
    (tmp_path / "package.json").write_text(
        """{
  "name": "jaunt-ts-reference-smoke",
  "private": true,
  "type": "module",
  "devDependencies": {
    "@usejaunt/ts": "0.1.0-alpha.0",
    "typescript": "6.0.2"
  }
}
""",
        encoding="utf-8",
    )
    (tmp_path / "jaunt.toml").write_text(
        """\
version = 2

[target.ts]
source_roots = ["packages/*/src"]
test_roots = ["packages/*/tests"]
projects = ["tsconfig.json"]
tool_owner = "."

[codex]
model = "gpt-5.6-sol"
""",
        encoding="utf-8",
    )
    (tmp_path / "tsconfig.json").write_text(
        """{
  "files": [],
  "references": [
    {"path": "./packages/core"},
    {"path": "./packages/app"}
  ]
}
""",
        encoding="utf-8",
    )
    for package in ("core", "app"):
        directory = tmp_path / "packages" / package
        directory.mkdir(parents=True)
        project_config: dict[str, Any] = {
            "compilerOptions": {
                "target": "ES2022",
                "module": "NodeNext",
                "moduleResolution": "NodeNext",
                "strict": True,
                "composite": True,
                "declaration": True,
                "rootDir": "src",
                "outDir": "dist",
                "types": [],
            },
            "include": ["src/**/*.ts"],
            "exclude": ["src/**/*.jaunt.ts"],
        }
        if package == "app":
            project_config["compilerOptions"].update(
                {
                    "baseUrl": ".",
                    "ignoreDeprecations": "6.0",
                    "paths": {"@core/*": ["../core/src/*"]},
                }
            )
            project_config["references"] = [{"path": "../core"}]
        (directory / "tsconfig.json").write_text(
            json.dumps(project_config, indent=2) + "\n", encoding="utf-8"
        )
    core = tmp_path / "packages/core/src/normalize"
    core.mkdir(parents=True)
    (core / "index.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Trim one value. */
export function normalize(value: string): string { return jaunt.magic(); }
""",
        encoding="utf-8",
    )
    app = tmp_path / "packages/app/src/slug"
    app.mkdir(parents=True)
    (app / "index.jaunt.ts").write_text(
        """import * as jaunt from "@usejaunt/ts/spec";
import { normalize } from "@core/normalize/index.jaunt.js";
jaunt.magicModule();
/** Normalize and lowercase one value. */
export function slugify(value: string): string {
  return jaunt.magic({ deps: [normalize] });
}
""",
        encoding="utf-8",
    )
    app_source = tmp_path / "packages/app/src/app.ts"
    app_source.write_text(
        """import { normalize } from "@core/normalize/index.js";
import { slugify } from "./slug/index.js";
export const result: readonly string[] = [normalize(" A "), slugify(" B ")];
""",
        encoding="utf-8",
    )
    config = load_config(root=tmp_path)
    generator = _ProjectReferenceGenerator()

    built = await run_build(tmp_path, config, generator=generator)

    assert built.exit_code == 0, "\n".join(
        diagnostic.message for diagnostics in built.failed.values() for diagnostic in diagnostics
    )
    assert generator.targets == [
        "packages/core/src/normalize/__generated__/index.ts",
        "packages/app/src/slug/__generated__/index.ts",
    ]
    assert built.generated == frozenset(
        {
            "ts:packages/core/src/normalize/index",
            "ts:packages/app/src/slug/index",
        }
    )
    assert (await run_status(tmp_path, config)).fresh == built.generated
    checked = await run_check(tmp_path, config, magic_only=True)
    assert checked.exit_code == 0, "\n".join(
        diagnostic.message for diagnostic in checked.diagnostics
    )
    compiler = tmp_path / "node_modules/typescript/bin/tsc"
    subprocess.run(
        ["node", str(compiler), "-b", "tsconfig.json", "--pretty", "false"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
