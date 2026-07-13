import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import { AnalyzerSession } from "../../src/worker/session.js";
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

async function sessionFor(
  root: string,
  compilerModulePath: string,
): Promise<AnalyzerSession> {
  return AnalyzerSession.create({
    root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
}

test("boundary any checks accept exact Promise and Array types but reject actual any arguments", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Resolve one normalized value. */
export async function normalizeAsync(value: string): Promise<string> {
  return jaunt.magic();
}
/** Copy all normalized values. */
export function copyValues(values: string[]): string[] { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "src/app.ts",
    `import { copyValues } from "./slug/index.js";
export const values = copyValues(["x"]);
`,
  );
  const session = await sessionFor(
    workspace.root,
    workspace.compilerModulePath,
  );
  const metadata = session.metadata();
  const valid = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {
      "ts:src/slug/index":
        "const __jaunt_impl_normalizeAsync = async (value: string): Promise<string> => value.trim();\n" +
        "const __jaunt_impl_copyValues = (values: string[]): string[] => [...values];",
    },
  });
  expect(valid.valid, JSON.stringify(valid.diagnostics)).toBe(true);

  const invalid = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {
      "ts:src/slug/index":
        "const __jaunt_impl_normalizeAsync = async (value: string): Promise<string> => value.trim();\n" +
        "const __jaunt_impl_copyValues = (values: any[]): string[] => values;",
    },
  });
  expect(invalid.valid).toBe(false);
  expect(
    invalid.diagnostics.some((item) => item.code === "JAUNT_TS_BOUNDARY_ANY"),
  ).toBe(true);
}, 15_000);

test("extensionless facade exports and transitive context cycles are rejected", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.ts",
    `export * as privateSpec from "./index.jaunt";
export * from "./__generated__/index.js";
`,
  );
  write(
    workspace.root,
    "src/slug/index.context.ts",
    `import { helper } from "../helper";
export const contextual = helper;
`,
  );
  write(
    workspace.root,
    "src/helper.ts",
    `export { slugify as helper } from "./slug/index";
`,
  );
  const session = await sessionFor(
    workspace.root,
    workspace.compilerModulePath,
  );
  const codes = new Set(
    session.analyzeWorkspace().diagnostics.map((item) => item.code),
  );
  expect(codes).toContain("JAUNT_TS_RUNTIME_SPEC_IMPORT");
  expect(codes).toContain("JAUNT_TS_CONTEXT_CYCLE");

  const metadata = session.metadata();
  const synchronized = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {},
    syncModuleIds: ["ts:src/slug/index"],
  });
  expect(synchronized.valid).toBe(false);
  expect(synchronized.artifacts).toEqual([]);
  expect(
    synchronized.diagnostics.some(
      (item) => item.code === "JAUNT_TS_FACADE_SPEC_LEAK",
    ),
  ).toBe(true);
});

test("ordinary project files participate in the commit precondition snapshot", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const session = await sessionFor(
    workspace.root,
    workspace.compilerModulePath,
  );
  expect(session.metadata().inputHashes).toHaveProperty("src/app.ts");
});

test("test-only package provenance follows configured roots, not directory names", async () => {
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
        devDependencies: {
          typescript: "6.0.2",
          vitest: "4.1.10",
        },
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "src/tests/production.ts",
    `import { expect } from "vitest";
export const productionAssertion = expect;
`,
  );
  write(
    workspace.root,
    "checks/support.ts",
    `import { expect } from "vitest";
export const testAssertion = expect;
`,
  );
  const session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["checks"],
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
  ).toEqual(["src/tests/production.ts"]);
});
