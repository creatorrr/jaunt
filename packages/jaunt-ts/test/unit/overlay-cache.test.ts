import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import ts from "@typescript/typescript6";
import { afterEach, expect, test } from "vitest";
import { OverlayProgramCache } from "../../src/analyzer/overlay.js";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

test("removing a virtual candidate reads the committed file instead of reusing it", () => {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-overlay-cache-"));
  roots.push(root);
  const path = resolve(root, "candidate.ts");
  const committed = "export const value = 'committed';\n";
  const candidate = "export const value = 'candidate';\n";
  writeFileSync(path, committed);
  const cache = new OverlayProgramCache();
  const options: ts.CompilerOptions = {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    noEmit: true,
  };

  const proposed = cache.create(
    "project:native",
    ts,
    [path],
    options,
    new Map([[path, candidate]]),
  );
  expect(proposed.getSourceFile(path)?.text).toBe(candidate);

  const restored = cache.create(
    "project:native",
    ts,
    [path],
    options,
    new Map(),
  );
  expect(restored.getSourceFile(path)?.text).toBe(committed);
});

test("consecutive overlays reuse programs containing package redirects", () => {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-overlay-redirect-"));
  roots.push(root);
  const write = (relative: string, source: string): void => {
    const path = resolve(root, relative);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, source);
  };
  write(
    "src/main.ts",
    'import type { A } from "a";\n' +
      'import type { B } from "b";\n' +
      "export type Both = A & B;\n",
  );
  for (const packageName of ["a", "b"]) {
    write(
      `node_modules/${packageName}/package.json`,
      JSON.stringify({
        name: packageName,
        version: "1.0.0",
        types: "index.d.ts",
      }),
    );
    write(
      `node_modules/${packageName}/index.d.ts`,
      `import type { Shared } from "shared";\nexport interface ${packageName.toUpperCase()} { value: Shared }\n`,
    );
    write(
      `node_modules/${packageName}/node_modules/shared/package.json`,
      JSON.stringify({
        name: "shared",
        version: "1.0.0",
        types: "index.d.ts",
      }),
    );
    write(
      `node_modules/${packageName}/node_modules/shared/index.d.ts`,
      "export interface Shared { value: string }\n",
    );
  }

  const cache = new OverlayProgramCache();
  const main = resolve(root, "src/main.ts");
  const options: ts.CompilerOptions = {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.NodeNext,
    moduleResolution: ts.ModuleResolutionKind.NodeNext,
    noEmit: true,
  };
  const first = cache.create("project:native", ts, [main], options, new Map());
  expect(
    first
      .getSourceFiles()
      .some(
        (sourceFile) =>
          "redirectInfo" in sourceFile && sourceFile.redirectInfo !== undefined,
      ),
  ).toBe(true);

  const second = cache.create("project:native", ts, [main], options, new Map());
  expect(ts.getPreEmitDiagnostics(second)).toEqual([]);
  expect(cache.state().at(0)?.reusedSourceFiles).toBeGreaterThan(0);
});
