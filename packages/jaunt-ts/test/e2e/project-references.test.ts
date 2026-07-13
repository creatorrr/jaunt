import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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

function config(extra: Record<string, unknown> = {}): Record<string, unknown> {
  const { compilerOptions: extraCompilerOptions, ...extraProjectOptions } =
    extra;
  return {
    compilerOptions: {
      target: "ES2022",
      module: "NodeNext",
      moduleResolution: "NodeNext",
      strict: true,
      composite: true,
      declaration: true,
      rootDir: "src",
      outDir: "dist",
      types: [],
      ...(extraCompilerOptions as Record<string, unknown> | undefined),
    },
    include: ["src/**/*.ts"],
    exclude: ["src/**/*.jaunt.ts"],
    ...extraProjectOptions,
  };
}

function prepareReferenceWorkspace(): ReturnType<
  typeof createFixtureWorkspace
> {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
  write(
    workspace.root,
    "tsconfig.json",
    `${JSON.stringify(
      {
        files: [],
        references: [{ path: "./packages/core" }, { path: "./packages/app" }],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/core/tsconfig.json",
    `${JSON.stringify(config(), null, 2)}\n`,
  );
  write(
    workspace.root,
    "packages/app/tsconfig.json",
    `${JSON.stringify(
      config({
        compilerOptions: {
          baseUrl: ".",
          ignoreDeprecations: "6.0",
          paths: { "@core/*": ["../core/src/*"] },
          rootDirs: ["src", "../core/src"],
        },
        references: [{ path: "../core" }],
      }),
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/core/src/normalize/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Trim one value. */
export function normalize(value: string): string { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "packages/app/src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import { normalize } from "@core/normalize/index.jaunt.js";
jaunt.magicModule();
/** Normalize and lowercase a slug. */
export function slugify(value: string): string {
  return jaunt.magic({ deps: [normalize] });
}
`,
  );
  write(
    workspace.root,
    "packages/app/src/app.ts",
    `import { normalize } from "@core/normalize/index.js";
import { slugify } from "./slug/index.js";
export const values: readonly string[] = [normalize(" A "), slugify(" B ")];
`,
  );
  return workspace;
}

async function referenceSession(
  workspace: ReturnType<typeof createFixtureWorkspace>,
): Promise<AnalyzerSession> {
  return AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["packages/*/src"],
    testRoots: ["packages/*/tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
}

function prepareIncrementalWorkspace(): ReturnType<
  typeof createFixtureWorkspace
> {
  const workspace = prepareReferenceWorkspace();
  write(
    workspace.root,
    "tsconfig.json",
    `${JSON.stringify(
      {
        files: [],
        references: [
          { path: "./packages/core" },
          { path: "./packages/app" },
          { path: "./packages/independent" },
        ],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/independent/tsconfig.json",
    `${JSON.stringify(config(), null, 2)}\n`,
  );
  write(
    workspace.root,
    "packages/independent/src/value/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return an independent value. */
export function value(input: string): string { return jaunt.magic(); }
`,
  );
  return workspace;
}

function programGenerations(session: AnalyzerSession): Record<string, number> {
  return Object.fromEntries(
    session
      .analysisProgramState()
      .map((state) => [state.projectId, state.generation]),
  );
}

test("invalidation reuses unrelated Programs and rebuilds the affected reference closure", async () => {
  const workspace = prepareIncrementalWorkspace();
  const session = await referenceSession(workspace);
  const initial = programGenerations(session);
  expect(Object.keys(initial).sort()).toEqual([
    "packages/app/tsconfig.json",
    "packages/core/tsconfig.json",
    "packages/independent/tsconfig.json",
  ]);

  const coreSpec = "packages/core/src/normalize/index.jaunt.ts";
  write(
    workspace.root,
    coreSpec,
    readFileSync(resolve(workspace.root, coreSpec), "utf8").replace(
      "Trim one value.",
      "Trim one value deterministically.",
    ),
  );
  const afterCoreMetadata = session.invalidate({ paths: [coreSpec] });
  const afterCore = programGenerations(session);
  expect(afterCoreMetadata.epoch).toBe(1);
  expect(afterCore["packages/core/tsconfig.json"]).not.toBe(
    initial["packages/core/tsconfig.json"],
  );
  expect(afterCore["packages/app/tsconfig.json"]).not.toBe(
    initial["packages/app/tsconfig.json"],
  );
  expect(afterCore["packages/independent/tsconfig.json"]).toBe(
    initial["packages/independent/tsconfig.json"],
  );
  expect(
    session
      .analysisProgramState()
      .find((state) => state.projectId === "packages/independent/tsconfig.json")
      ?.reused,
  ).toBe(true);
  expect(
    session
      .analysisProgramState()
      .find((state) => state.projectId === "packages/core/tsconfig.json")
      ?.reusedSourceFiles,
  ).toBeGreaterThan(0);
  expect(
    session
      .analysisProgramState()
      .find((state) => state.projectId === "packages/app/tsconfig.json")
      ?.reusedSourceFiles,
  ).toBeGreaterThan(0);
  expect(
    session
      .analyzeContracts()
      .modules.find(
        (module) => module.project === "packages/core/tsconfig.json",
      )?.specSource,
  ).toContain("deterministically");

  const appConfig = "packages/app/tsconfig.json";
  const appConfigValue = JSON.parse(
    readFileSync(resolve(workspace.root, appConfig), "utf8"),
  ) as { compilerOptions: Record<string, unknown> };
  appConfigValue.compilerOptions.noUncheckedIndexedAccess = true;
  write(
    workspace.root,
    appConfig,
    `${JSON.stringify(appConfigValue, null, 2)}\n`,
  );
  const beforeConfig = programGenerations(session);
  session.invalidate({ paths: [appConfig] });
  const afterConfig = programGenerations(session);
  expect(afterConfig["packages/app/tsconfig.json"]).not.toBe(
    beforeConfig["packages/app/tsconfig.json"],
  );
  expect(afterConfig["packages/core/tsconfig.json"]).toBe(
    beforeConfig["packages/core/tsconfig.json"],
  );
  expect(afterConfig["packages/independent/tsconfig.json"]).toBe(
    beforeConfig["packages/independent/tsconfig.json"],
  );
}, 15_000);

test("repeated warm edits keep one Program per config without churning siblings", async () => {
  const workspace = prepareIncrementalWorkspace();
  const session = await referenceSession(workspace);
  const stable = programGenerations(session);
  const appSpec = "packages/app/src/slug/index.jaunt.ts";
  const snapshots = new Set([session.metadata().snapshot]);
  let previousAppGeneration = stable["packages/app/tsconfig.json"]!;

  for (let revision = 1; revision <= 8; revision += 1) {
    write(
      workspace.root,
      appSpec,
      `import * as jaunt from "@usejaunt/ts/spec";
import { normalize } from "@core/normalize/index.jaunt.js";
jaunt.magicModule();
/** Normalize and lowercase a slug, revision ${revision}. */
export function slugify(value: string): string {
  return jaunt.magic({ deps: [normalize] });
}
`,
    );
    const metadata = session.invalidate({ paths: [appSpec] });
    const state = session.analysisProgramState();
    const generations = programGenerations(session);
    expect(state).toHaveLength(3);
    expect(generations["packages/app/tsconfig.json"]).not.toBe(
      previousAppGeneration,
    );
    expect(generations["packages/core/tsconfig.json"]).toBe(
      stable["packages/core/tsconfig.json"],
    );
    expect(generations["packages/independent/tsconfig.json"]).toBe(
      stable["packages/independent/tsconfig.json"],
    );
    previousAppGeneration = generations["packages/app/tsconfig.json"]!;
    snapshots.add(metadata.snapshot);
  }

  expect(session.metadata().epoch).toBe(8);
  expect(snapshots.size).toBe(9);
  expect(
    session
      .analyzeContracts()
      .modules.find((module) => module.project === "packages/app/tsconfig.json")
      ?.specSource,
  ).toContain("revision 8");
}, 20_000);

test("solution roots route composite projects and validate a cross-project generated dependency", async () => {
  const workspace = prepareReferenceWorkspace();
  const session = await referenceSession(workspace);
  const analysis = session.analyzeWorkspace();
  expect(
    analysis.diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  expect(
    analysis.projects.map(({ id, role, references }) => ({
      id,
      role,
      references,
    })),
  ).toEqual([
    {
      id: "packages/app/tsconfig.json",
      role: "production",
      references: ["packages/core/tsconfig.json"],
    },
    {
      id: "packages/core/tsconfig.json",
      role: "production",
      references: [],
    },
    {
      id: "tsconfig.json",
      role: "solution",
      references: ["packages/app/tsconfig.json", "packages/core/tsconfig.json"],
    },
  ]);
  const modules = session.analyzeContracts().modules;
  expect(
    modules.find((module) => module.moduleId.includes("app"))?.dependencies,
  ).toEqual(["ts:packages/core/src/normalize/index#normalize"]);

  const metadata = session.metadata();
  const result = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {
      "ts:packages/core/src/normalize/index":
        "const __jaunt_impl_normalize = (value: string): string => value.trim();",
      "ts:packages/app/src/slug/index":
        'import { normalize } from "@core/normalize/index.js";\n' +
        "const __jaunt_impl_slugify = (value: string): string => normalize(value).toLowerCase();",
    },
  });
  expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
  expect(result.affectedProjects).toEqual([
    "packages/core/tsconfig.json",
    "packages/app/tsconfig.json",
  ]);
  expect(
    result.artifacts
      .filter((artifact) => artifact.kind === "implementation")
      .map((artifact) => artifact.moduleId)
      .sort(),
  ).toEqual([
    "ts:packages/app/src/slug/index",
    "ts:packages/core/src/normalize/index",
  ]);
}, 15_000);

test("compiler option hashes are portable across copied project-reference workspaces", async () => {
  const left = prepareReferenceWorkspace();
  const right = prepareReferenceWorkspace();
  const projectHashes = async (
    workspace: ReturnType<typeof createFixtureWorkspace>,
  ) =>
    Object.fromEntries(
      (await referenceSession(workspace))
        .analyzeWorkspace()
        .projects.map((project) => [project.id, project.compilerOptionsHash]),
    );

  const original = await projectHashes(left);
  expect(await projectHashes(right)).toEqual(original);

  write(
    right.root,
    "packages/app/tsconfig.json",
    `${JSON.stringify(
      config({
        compilerOptions: {
          baseUrl: ".",
          ignoreDeprecations: "6.0",
          paths: { "@core/*": ["../core/src/*"] },
          rootDirs: ["src", "../core/src"],
          outDir: "build",
        },
        references: [{ path: "../core" }],
      }),
      null,
      2,
    )}\n`,
  );
  const changed = await projectHashes(right);
  expect(changed["packages/app/tsconfig.json"]).not.toBe(
    original["packages/app/tsconfig.json"],
  );
  expect(changed["packages/core/tsconfig.json"]).toBe(
    original["packages/core/tsconfig.json"],
  );
});

test("a referenced API proposal is rejected when an ordinary downstream project no longer typechecks", async () => {
  const workspace = prepareReferenceWorkspace();
  rmSync(resolve(workspace.root, "packages/app/src/slug"), {
    recursive: true,
    force: true,
  });
  write(
    workspace.root,
    "packages/core/src/normalize/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return the normalized value length. */
export function normalize(value: string): number { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "packages/app/src/app.ts",
    `import { normalize } from "@core/normalize/index.js";
export const normalized: string = normalize(" A ");
`,
  );
  const session = await referenceSession(workspace);
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  const metadata = session.metadata();
  const result = session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: {
      "ts:packages/core/src/normalize/index":
        "const __jaunt_impl_normalize = (value: string): number => value.trim().length;",
    },
  });
  expect(result.valid).toBe(false);
  expect(result.artifacts).toEqual([]);
  expect(
    result.diagnostics.some(
      (item) => item.code === "TS2322" && item.path?.endsWith("app.ts"),
    ),
  ).toBe(true);
});

test("a leaf project resolving a different compiler identity fails deterministically", async () => {
  const workspace = prepareReferenceWorkspace();
  write(
    workspace.root,
    "packages/core/node_modules/typescript/lib/typescript.js",
    "export const version = '5.8.3';\n",
  );
  const session = await referenceSession(workspace);
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter(
        (item) => item.code === "JAUNT_TS_COMPILER_IDENTITY_MISMATCH",
      ),
  ).toEqual([expect.objectContaining({ path: "packages/core/tsconfig.json" })]);
});

test("overlapping production ownership is ambiguous even when config depths differ", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
  write(
    workspace.root,
    "tsconfig.json",
    `${JSON.stringify(config({ include: ["packages/**/*.ts"] }), null, 2)}\n`,
  );
  write(
    workspace.root,
    "packages/app/tsconfig.json",
    `${JSON.stringify(config(), null, 2)}\n`,
  );
  write(
    workspace.root,
    "packages/app/src/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return one value. */
export function value(input: string): string { return jaunt.magic(); }
`,
  );
  await expect(
    AnalyzerSession.create({
      root: workspace.root,
      projects: ["tsconfig.json", "packages/app/tsconfig.json"],
      testProjects: [],
      sourceRoots: ["packages/*/src"],
      testRoots: [],
      generatedDir: "__generated__",
      toolOwner: ".",
      compilerModulePath: workspace.compilerModulePath,
      clientVersion: "test",
      toolVersion: "test",
    }),
  ).rejects.toMatchObject({
    payload: expect.objectContaining({ code: "PROJECT_AMBIGUOUS" }),
  });
});

test("overlapping test-project claims are rejected instead of choosing one", async () => {
  const workspace = createFixtureWorkspace({ withTestSpec: true });
  roots.push(workspace.root);
  for (const name of ["a", "b"]) {
    write(
      workspace.root,
      `tsconfig.test-${name}.json`,
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            types: [],
          },
          include: ["tests/__generated__/**/*.ts"],
          exclude: ["tests/**/*.jaunt-test.ts"],
        },
        null,
        2,
      )}\n`,
    );
  }
  await expect(
    AnalyzerSession.create({
      root: workspace.root,
      projects: ["tsconfig.json"],
      testProjects: ["tsconfig.test-a.json", "tsconfig.test-b.json"],
      sourceRoots: ["src"],
      testRoots: ["tests"],
      generatedDir: "__generated__",
      toolOwner: ".",
      compilerModulePath: workspace.compilerModulePath,
      clientVersion: "test",
      toolVersion: "test",
    }),
  ).rejects.toMatchObject({
    payload: expect.objectContaining({ code: "PROJECT_AMBIGUOUS" }),
  });
});
