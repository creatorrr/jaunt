import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import { AnalyzerSession } from "../../src/worker/session.js";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

function write(root: string, path: string, source: string): void {
  const target = resolve(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, source);
}

test("combined overlays allow only declared dependency facades", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/base/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Normalize one value. */
export function base(value: string): string { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "src/other/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return another value. */
export function other(value: string): string { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import { base } from "../base/index.jaunt.js";
jaunt.magicModule();
/** Create a slug through the declared normalizer. */
export function slugify(title: string): string {
  return jaunt.magic({ deps: [base] });
}
`,
  );
  const session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  const metadata = session.metadata();
  const candidates = {
    "ts:src/base/index":
      "const __jaunt_impl_base = (value: string): string => value.trim();",
    "ts:src/other/index":
      "const __jaunt_impl_other = (value: string): string => value;",
    "ts:src/slug/index":
      'import { base } from "../../base/index.js";\n' +
      "const __jaunt_impl_slugify = (title: string): string => base(title);",
  };
  const valid = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates,
  });
  expect(valid.valid, JSON.stringify(valid.diagnostics)).toBe(true);
  expect(
    valid.artifacts
      .filter((artifact) => artifact.kind === "implementation")
      .map((artifact) => artifact.moduleId)
      .sort(),
  ).toEqual(["ts:src/base/index", "ts:src/other/index", "ts:src/slug/index"]);

  const undeclared = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {
      "ts:src/slug/index":
        'import { other } from "../../other/index.js";\n' +
        "const __jaunt_impl_slugify = (title: string): string => other(title);",
    },
  });
  expect(undeclared.valid).toBe(false);
  expect(undeclared.artifacts).toEqual([]);
  expect(
    undeclared.diagnostics.some(
      (item) => item.code === "JAUNT_TS_UNDECLARED_DEPENDENCY_IMPORT",
    ),
  ).toBe(true);
}, 15_000);

test("package audits include test roots and allow dev dependencies only there", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify(
      {
        name: "fixture",
        private: true,
        type: "module",
        devDependencies: { typescript: "6.0.2", vitest: "3.2.4" },
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "tests/support.ts",
    `import { expect } from "vitest";
export const assertEqual = (actual: unknown, expected: unknown): void => {
  expect(actual).toEqual(expected);
};
`,
  );

  let session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter(
        (item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE",
      ),
  ).toEqual([]);

  write(
    workspace.root,
    "src/production.ts",
    `import { expect } from "vitest";
export const productionAssertion = expect;
`,
  );
  session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE")
      .map((item) => item.path),
  ).toEqual(["src/production.ts"]);
});
