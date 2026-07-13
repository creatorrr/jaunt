import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import ts from "@typescript/typescript6";
import { resolvePackageImportResolution } from "../../src/analyzer/dependencies.js";
import { auditPackageImport } from "../../src/analyzer/provenance.js";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

function write(root: string, path: string, content: string): void {
  const target = resolve(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function fixture(manifest: Record<string, unknown>): {
  readonly root: string;
  readonly importer: string;
} {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-ts-provenance-"));
  roots.push(root);
  write(root, "package.json", `${JSON.stringify(manifest, null, 2)}\n`);
  write(root, "src/generated.ts", "export {};\n");
  return { root, importer: resolve(root, "src/generated.ts") };
}

test("package imports aliases preserve logical package authorization", () => {
  const { root, importer } = fixture({
    name: "fixture",
    dependencies: { "@fixture/base": "workspace:*" },
    imports: {
      "#tooling": "@usejaunt/ts/spec",
      "#external": "not-declared/subpath",
      "#internal": "./src/internal.js",
      "#workspace": "@fixture/base",
      "#nested": "#tooling",
      "#conditional": {
        types: "./src/internal.ts",
        default: "not-declared",
      },
      "#pattern/*": "@fixture/base/*",
      "#legacy-external/": "not-declared-legacy/",
      "#legacy-internal/": "./src/legacy/",
    },
  });

  for (const specifier of ["#tooling", "#nested"]) {
    expect(
      auditPackageImport(root, importer, specifier, false, undefined, false),
    ).toEqual(
      expect.objectContaining({ code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT" }),
    );
  }
  for (const specifier of [
    "#external",
    "#conditional",
    "#legacy-external/helper",
  ]) {
    expect(
      auditPackageImport(root, importer, specifier, false, undefined, false),
    ).toEqual(expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }));
  }
  for (const specifier of [
    "#internal",
    "#workspace",
    "#pattern/helper",
    "#legacy-internal/helper",
  ]) {
    expect(
      auditPackageImport(root, importer, specifier, false, undefined, false),
    ).toBeUndefined();
  }
});

test("package imports aliases cannot be authorized by unrelated physical resolution", () => {
  const { root, importer } = fixture({
    name: "fixture",
    imports: {
      "#external": "not-declared/subpath",
      "#declared": "declared-package/subpath",
    },
    dependencies: { "declared-package": "1.0.0" },
  });
  write(
    root,
    "packages/other/package.json",
    `${JSON.stringify({ name: "other-package" }, null, 2)}\n`,
  );
  write(root, "packages/other/index.ts", "export {};\n");

  for (const resolvedWorkspaceFile of [
    importer,
    resolve(root, "packages/other/index.ts"),
  ]) {
    expect(
      auditPackageImport(
        root,
        importer,
        "#external",
        false,
        { resolvedWorkspaceFile },
        false,
      ),
    ).toEqual(expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }));
  }

  expect(
    auditPackageImport(
      root,
      importer,
      "#declared",
      false,
      { resolvedWorkspaceFile: resolve(root, "packages/other/index.ts") },
      false,
    ),
  ).toEqual(
    expect.objectContaining({
      code: "JAUNT_TS_UNDECLARED_PACKAGE",
      message: expect.stringContaining("other-package"),
    }),
  );
});

test("package imports aliases fail closed on unsafe relative targets", () => {
  const { root, importer } = fixture({
    name: "fixture",
    imports: {
      "#escape": "./../outside.js",
      "#modules": "./node_modules/hidden/index.js",
      "#cycle-a": "#cycle-b",
      "#cycle-b": "#cycle-a",
      "#pattern/*": "./safe/*",
      "#bad-prefix/": "./not-a-directory",
    },
  });

  for (const specifier of [
    "#escape",
    "#modules",
    "#cycle-a",
    "#pattern/../outside",
    "#bad-prefix/child",
  ]) {
    expect(
      auditPackageImport(root, importer, specifier, false, undefined, false),
    ).toEqual(
      expect.objectContaining({ code: "JAUNT_TS_PACKAGE_IMPORTS_INVALID" }),
    );
  }
});

test("resolution exposes workspace ownership but not external store paths", () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const manifest = JSON.parse(
    readFileSync(resolve(workspace.root, "package.json"), "utf8"),
  ) as Record<string, unknown>;
  manifest.imports = {
    "#internal": "./src/internal.js",
    "#tooling": "@usejaunt/ts/spec",
  };
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify(manifest, null, 2)}\n`,
  );
  write(workspace.root, "src/internal.ts", "export const value = 1;\n");
  const importer = resolve(workspace.root, "src/slug/__generated__/index.ts");
  const options: ts.CompilerOptions = {
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    target: ts.ScriptTarget.ES2022,
  };

  expect(
    resolvePackageImportResolution(
      ts,
      workspace.root,
      importer,
      "#internal",
      options,
    ),
  ).toEqual({
    resolvedWorkspaceFile: resolve(workspace.root, "src/internal.ts"),
  });
  // @usejaunt/ts is symlinked to this checkout, as a pnpm-style external store
  // package may be. Its physical path must never be treated as workspace
  // authorization; the manifest's logical target is audited separately.
  expect(
    resolvePackageImportResolution(
      ts,
      workspace.root,
      importer,
      "#tooling",
      options,
    ),
  ).toBeUndefined();
});
