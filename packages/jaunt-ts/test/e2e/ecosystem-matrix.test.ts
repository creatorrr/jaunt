import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { AnalyzerSession } from "../../src/worker/session.js";
import {
  createFixtureWorkspace,
  type FixtureWorkspace,
} from "../helpers/workspace.js";

type CompilerPackage = "@typescript/typescript58" | "@typescript/typescript6";

interface MatrixFixture {
  readonly name: string;
  readonly packageType: "module" | "commonjs";
  readonly compilerOptions: Readonly<Record<string, unknown>>;
  readonly emittedModule: "esm" | "commonjs";
  readonly specExtension?: ".ts" | ".tsx";
  readonly specSource: string;
  readonly candidate: string;
  readonly applicationSource: string;
  readonly supportFiles?: Readonly<Record<string, string>>;
  readonly expectedRuntime?: string;
}

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

function commit(
  root: string,
  artifacts: readonly { path: string; content: string }[],
): void {
  for (const artifact of artifacts) {
    write(root, artifact.path, artifact.content);
  }
}

function tsc(workspace: FixtureWorkspace, ...arguments_: string[]): string {
  return execFileSync(
    process.execPath,
    [
      resolve(workspace.root, "node_modules/typescript/lib/tsc.js"),
      ...arguments_,
    ],
    { cwd: workspace.root, encoding: "utf8" },
  );
}

function prepareWorkspace(
  fixture: MatrixFixture,
  compilerPackage: CompilerPackage,
): FixtureWorkspace {
  const workspace = createFixtureWorkspace({ compilerPackage });
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
  const typescriptVersion = (
    JSON.parse(
      readFileSync(
        resolve(workspace.root, "node_modules/typescript/package.json"),
        "utf8",
      ),
    ) as { version: string }
  ).version;
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify(
      {
        name: `jaunt-${fixture.name}-fixture`,
        private: true,
        type: fixture.packageType,
        devDependencies: {
          "@usejaunt/ts": "0.1.0-alpha.0",
          typescript: typescriptVersion,
        },
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "tsconfig.json",
    `${JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          strict: true,
          exactOptionalPropertyTypes: true,
          rootDir: "src",
          outDir: "dist",
          declaration: true,
          types: [],
          ...fixture.compilerOptions,
        },
        include: ["src/**/*.ts", "src/**/*.tsx"],
        exclude: ["src/**/*.jaunt.ts", "src/**/*.jaunt.tsx"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    `src/feature/index.jaunt${fixture.specExtension ?? ".ts"}`,
    fixture.specSource,
  );
  write(workspace.root, "src/app.ts", fixture.applicationSource);
  for (const [path, content] of Object.entries(fixture.supportFiles ?? {})) {
    write(workspace.root, path, content);
  }
  return workspace;
}

async function exerciseFixture(
  fixture: MatrixFixture,
  compilerPackage: CompilerPackage = "@typescript/typescript6",
): Promise<void> {
  const workspace = prepareWorkspace(fixture, compilerPackage);
  const session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "ecosystem-matrix",
    toolVersion: "0.1.0-alpha.0",
  });
  const expectedVersion =
    compilerPackage === "@typescript/typescript58" ? /^5\.8\./ : /^6\./;
  expect(session.initializeResult().typescriptVersion).toMatch(expectedVersion);
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter((diagnostic) => diagnostic.severity === "error"),
  ).toEqual([]);
  const contract = session.analyzeContracts().modules[0]!;
  expect(contract.moduleId).toBe("ts:src/feature/index");

  const beforeSync = session.metadata();
  const synchronized = session.validateOverlay({
    sessionId: beforeSync.sessionId,
    expectedEpoch: beforeSync.epoch,
    expectedSnapshot: beforeSync.snapshot,
    candidates: {},
    syncModuleIds: [contract.moduleId],
  });
  expect(synchronized.valid, JSON.stringify(synchronized.diagnostics)).toBe(
    true,
  );
  expect(
    synchronized.artifacts.some((artifact) => artifact.kind === "placeholder"),
  ).toBe(true);
  commit(workspace.root, synchronized.artifacts);
  tsc(workspace, "-p", "tsconfig.json", "--noEmit");

  const afterSync = session.invalidate({
    paths: synchronized.artifacts.map((artifact) => artifact.path),
  });
  const generated = session.validateOverlay({
    sessionId: afterSync.sessionId,
    expectedEpoch: afterSync.epoch,
    expectedSnapshot: afterSync.snapshot,
    candidates: { [contract.moduleId]: fixture.candidate },
  });
  expect(generated.valid, JSON.stringify(generated.diagnostics)).toBe(true);
  expect(
    generated.artifacts.some((artifact) => artifact.kind === "implementation"),
  ).toBe(true);
  commit(workspace.root, generated.artifacts);

  session.invalidate({
    paths: generated.artifacts.map((artifact) => artifact.path),
  });
  tsc(workspace, "-p", "tsconfig.json", "--noEmit");
  tsc(workspace, "-p", "tsconfig.json");
  expect(existsSync(resolve(workspace.root, "dist/feature/index.d.ts"))).toBe(
    true,
  );
  expect(
    existsSync(resolve(workspace.root, "dist/feature/index.jaunt.js")),
  ).toBe(false);
  const facadeJavaScript = readFileSync(
    resolve(workspace.root, "dist/feature/index.js"),
    "utf8",
  );
  expect(facadeJavaScript).not.toContain(".jaunt");
  expect(facadeJavaScript).not.toContain("@usejaunt/ts");
  if (fixture.emittedModule === "commonjs") {
    expect(facadeJavaScript).toContain("require(");
    expect(facadeJavaScript).toContain('"use strict"');
  } else {
    expect(facadeJavaScript).toContain("export * from");
    expect(facadeJavaScript).not.toContain("require(");
  }

  if (fixture.expectedRuntime !== undefined) {
    const stdout = execFileSync(process.execPath, ["dist/app.js"], {
      cwd: workspace.root,
      encoding: "utf8",
    }).trim();
    expect(stdout).toBe(fixture.expectedRuntime);
  }
}

const nodeNext: MatrixFixture = {
  name: "nodenext-esm",
  packageType: "module",
  emittedModule: "esm",
  compilerOptions: {
    module: "NodeNext",
    moduleResolution: "NodeNext",
  },
  specSource: `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Trim, lowercase, and join whitespace runs with one dash. */
export function slugify(value: string): string { return jaunt.magic(); }
`,
  candidate:
    'const __jaunt_impl_slugify = (value: string): string => value.trim().toLowerCase().replace(/\\s+/g, "-");',
  applicationSource: `import { slugify } from "./feature/index.js";
console.log(slugify(" Hello ESM "));
`,
  expectedRuntime: "hello-esm",
};

describe("TypeScript ecosystem matrix", () => {
  for (const compilerPackage of [
    "@typescript/typescript58",
    "@typescript/typescript6",
  ] as const) {
    test(`NodeNext ESM completes sync, build, declaration emit, and runtime on ${compilerPackage}`, async () => {
      await exerciseFixture(nodeNext, compilerPackage);
    }, 20_000);
  }

  test("Bundler resolution validates extensionless Vite-style source and emits ESM declarations", async () => {
    await exerciseFixture({
      name: "bundler",
      packageType: "module",
      emittedModule: "esm",
      compilerOptions: {
        module: "ESNext",
        moduleResolution: "Bundler",
      },
      specSource: `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return a stable greeting for one name. */
export function greet(name: string): string { return jaunt.magic(); }
`,
      candidate:
        "const __jaunt_impl_greet = (name: string): string => `hello ${name.trim()}`;",
      applicationSource: `import { greet } from "./feature/index";
export const greeting = greet("Bundler");
`,
    });
  }, 20_000);

  test("a CommonJS-emitting NodeNext project completes sync, build, declarations, and runtime", async () => {
    await exerciseFixture({
      name: "commonjs",
      packageType: "commonjs",
      emittedModule: "commonjs",
      compilerOptions: {
        module: "NodeNext",
        moduleResolution: "NodeNext",
      },
      specSource: `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Double a finite number. */
export function double(value: number): number { return jaunt.magic(); }
`,
      candidate:
        "const __jaunt_impl_double = (value: number): number => value * 2;",
      applicationSource: `import { double } from "./feature/index.js";
console.log(double(21));
`,
      expectedRuntime: "42",
    });
  }, 20_000);

  test("a governed TSX component uses a custom JSX factory and emits runnable JavaScript and declarations", async () => {
    await exerciseFixture({
      name: "tsx",
      packageType: "module",
      emittedModule: "esm",
      compilerOptions: {
        module: "NodeNext",
        moduleResolution: "NodeNext",
        jsx: "react",
        jsxFactory: "h",
      },
      specExtension: ".tsx",
      specSource: `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Render the label inside a span virtual node. */
export function Badge(label: string): JSX.Element { return jaunt.magic(); }
`,
      candidate: `import { h } from "../../jsx-runtime.js";
const __jaunt_impl_Badge = (label: string): JSX.Element => <span>{label}</span>;`,
      applicationSource: `import { Badge } from "./feature/index.js";
console.log(JSON.stringify(Badge("ready")));
`,
      supportFiles: {
        "src/jsx-runtime.ts": `export interface VNode {
  readonly tag: string;
  readonly props: Readonly<Record<string, unknown>> | null;
  readonly children: readonly unknown[];
}
export function h(
  tag: string,
  props: Readonly<Record<string, unknown>> | null,
  ...children: readonly unknown[]
): VNode {
  return { tag, props, children };
}
`,
        "src/jsx.d.ts": `import type { VNode } from "./jsx-runtime.js";
declare global {
  namespace JSX {
    interface Element extends VNode {}
    interface IntrinsicElements { span: Record<string, unknown>; }
  }
}
export {};
`,
      },
      expectedRuntime: '{"tag":"span","props":null,"children":["ready"]}',
    });
  }, 20_000);
});
