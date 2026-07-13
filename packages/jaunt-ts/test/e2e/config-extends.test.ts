import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import { WorkerError } from "../../src/protocol/errors.js";
import { AnalyzerSession } from "../../src/worker/session.js";
import {
  createFixtureWorkspace,
  type FixtureWorkspace,
} from "../helpers/workspace.js";

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

function rootConfig(extendsPath: string): string {
  return `${JSON.stringify(
    {
      extends: extendsPath,
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        noEmit: true,
        exactOptionalPropertyTypes: true,
        types: [],
      },
      include: ["src/**/*.ts"],
      exclude: [
        "src/**/*.jaunt.ts",
        "src/**/*.jaunt-test.ts",
        "src/**/__generated__/**",
      ],
    },
    null,
    2,
  )}\n`;
}

async function sessionFor(
  workspace: FixtureWorkspace,
): Promise<AnalyzerSession> {
  return AnalyzerSession.create({
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
}

function expectStaleValidation(session: AnalyzerSession): void {
  const metadata = session.metadata();
  try {
    session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      syncModuleIds: ["ts:src/slug/index"],
    });
    throw new Error("expected stale session rejection");
  } catch (error) {
    expect(error).toBeInstanceOf(WorkerError);
    expect((error as WorkerError).payload.code).toBe("STALE_SESSION");
  }
}

test("every workspace config in an extends chain is hashed and validated", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "configs/strict.json",
    `${JSON.stringify({ compilerOptions: { strict: true } }, null, 2)}\n`,
  );
  write(
    workspace.root,
    "configs/base.json",
    `${JSON.stringify(
      {
        extends: "./strict.json",
        compilerOptions: { forceConsistentCasingInFileNames: true },
      },
      null,
      2,
    )}\n`,
  );
  write(workspace.root, "tsconfig.json", rootConfig("./configs/base.json"));

  const session = await sessionFor(workspace);
  const before = session.metadata();
  expect(Object.keys(before.inputHashes)).toEqual(
    expect.arrayContaining([
      "configs/base.json",
      "configs/strict.json",
      "tsconfig.json",
    ]),
  );
  const beforeHash =
    session.analyzeWorkspace().projects[0]!.compilerOptionsHash;

  write(
    workspace.root,
    "configs/strict.json",
    `${JSON.stringify(
      { compilerOptions: { strict: true, noUncheckedIndexedAccess: true } },
      null,
      2,
    )}\n`,
  );
  expectStaleValidation(session);

  const refreshed = await sessionFor(workspace);
  expect(refreshed.metadata().snapshot).not.toBe(before.snapshot);
  expect(
    refreshed.analyzeWorkspace().projects[0]!.compilerOptionsHash,
  ).not.toBe(beforeHash);
});

test("package configs reached through node_modules are accepted and remain in the snapshot", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const packageRoot = mkdtempSync(
    resolve(tmpdir(), "jaunt-ts-config-package-"),
  );
  roots.push(packageRoot);
  write(
    packageRoot,
    "package.json",
    `${JSON.stringify(
      {
        name: "@fixture/tsconfig",
        version: "1.0.0",
        exports: "./tsconfig.json",
      },
      null,
      2,
    )}\n`,
  );
  write(
    packageRoot,
    "tsconfig.json",
    `${JSON.stringify({ compilerOptions: { strict: true } }, null, 2)}\n`,
  );
  mkdirSync(resolve(workspace.root, "node_modules/@fixture"), {
    recursive: true,
  });
  symlinkSync(
    packageRoot,
    resolve(workspace.root, "node_modules/@fixture/tsconfig"),
    "dir",
  );
  write(workspace.root, "tsconfig.json", rootConfig("@fixture/tsconfig"));

  const session = await sessionFor(workspace);
  const before = session.metadata();
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  // A physical package-store path is intentionally not exposed as a writable
  // Python precondition, but its bytes still participate in the worker snapshot.
  expect(before.inputHashes).not.toHaveProperty(
    "node_modules/@fixture/tsconfig/tsconfig.json",
  );

  write(
    packageRoot,
    "tsconfig.json",
    `${JSON.stringify(
      { compilerOptions: { strict: true, noUncheckedIndexedAccess: true } },
      null,
      2,
    )}\n`,
  );
  expectStaleValidation(session);
  expect((await sessionFor(workspace)).metadata().snapshot).not.toBe(
    before.snapshot,
  );
});

test("direct tsconfig extends paths outside the workspace are rejected", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const externalRoot = mkdtempSync(
    resolve(tmpdir(), "jaunt-ts-config-outside-"),
  );
  roots.push(externalRoot);
  write(
    externalRoot,
    "base.json",
    `${JSON.stringify({ compilerOptions: { strict: true } }, null, 2)}\n`,
  );
  write(
    workspace.root,
    "tsconfig.json",
    rootConfig(resolve(externalRoot, "base.json")),
  );

  const session = await sessionFor(workspace);
  const diagnostics = session
    .analyzeWorkspace()
    .diagnostics.filter((item) => item.severity === "error");
  expect(diagnostics).toHaveLength(1);
  expect(diagnostics[0]).toMatchObject({ code: "TS5012" });
  expect(diagnostics[0]!.message).toContain(
    "outside the workspace/package roots",
  );
});
