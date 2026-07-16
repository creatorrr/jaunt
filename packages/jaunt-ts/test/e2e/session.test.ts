import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { AnalyzerSession } from "../../src/worker/session.js";
import ts from "@typescript/typescript6";
import {
  composeCandidate,
  decomposeGeneratedImplementation,
} from "../../src/analyzer/composition.js";
import { loadProjectGraph } from "../../src/analyzer/config.js";
import { validateApiMirrorEquivalence } from "../../src/analyzer/overlay.js";
import {
  createFixtureWorkspace,
  type FixtureWorkspace,
} from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

async function sessionFor(
  options: {
    withClass?: boolean;
    withTestSpec?: boolean;
    compilerPackage?: "@typescript/typescript6" | "@typescript/typescript58";
  } = {},
): Promise<{
  workspace: FixtureWorkspace;
  session: AnalyzerSession;
}> {
  const workspace = createFixtureWorkspace(options);
  roots.push(workspace.root);
  const session = await AnalyzerSession.create({
    root: workspace.root,
    projects: ["tsconfig.json"],
    testProjects: options.withTestSpec ? ["tsconfig.test.json"] : [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "0.1.0-alpha.0",
  });
  return { workspace, session };
}

function commit(
  root: string,
  artifacts: readonly { path: string; content: string }[],
): void {
  for (const artifact of artifacts) {
    const path = resolve(root, artifact.path);
    mkdirSync(dirname(path), { recursive: true });
    writeFileSync(path, artifact.content);
  }
}

describe("analyzer vertical path", () => {
  test("API mirrors prune imports used only by implementation intent", async () => {
    const workspace = createFixtureWorkspace({ withClass: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import type { Store } from "../store/index.jaunt.js";
import { Foo, runtimeOnly } from "../support.js";
jaunt.magicModule({ deps: [Foo, runtimeOnly] });
export interface PublicOptions { Store?: string; runtimeOnly?: string; }
export type PublicBox<Store, runtimeOnly> = readonly [Store, runtimeOnly];
/** Normalize a title. */
export function slugify(title: string): string { return jaunt.magic(); }
/** Return a parameter's runtime type. */
export function inspect(Foo: string): typeof Foo { return jaunt.magic(); }
void (undefined as unknown as Store);
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/support.ts"),
      `export const Foo = "foo";
export const runtimeOnly = (value: string): string => value.trim();
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

    const slug = session
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:src/slug/index")!;
    expect(slug.typeImports).toHaveLength(2);
    expect(slug.apiSource).not.toContain("../store/index.jaunt.js");
    expect(slug.apiSource).not.toContain("../support.js");
  });

  test("strict unused checks accept private stub parameters and synthetic conformance bindings", async () => {
    const workspace = createFixtureWorkspace({ withClass: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import { magic as fill, magicModule as govern } from "@usejaunt/ts";
import { Store } from "../store/index.jaunt.js";
govern({ deps: [Store] });
export interface SlugOptions { readonly trim?: boolean; }
/** Normalize a title. */
export function slugify(title: string, options?: SlugOptions): string {
  return fill();
}
`,
    );
    writeFileSync(
      resolve(workspace.root, "tsconfig.json"),
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            noUnusedLocals: true,
            noUnusedParameters: true,
            exactOptionalPropertyTypes: true,
            types: [],
          },
          include: ["src/**/*.ts"],
          exclude: ["src/**/*.jaunt.ts", "src/**/__generated__/**"],
        },
        null,
        2,
      )}\n`,
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
    const contracts = session.analyzeContracts().modules;
    const slug = contracts.find(
      (contract) => contract.moduleId === "ts:src/slug/index",
    )!;
    const store = contracts.find(
      (contract) => contract.moduleId === "ts:src/store/index",
    )!;
    const metadata = session.metadata();
    const placeholders = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: contracts.map((contract) => contract.moduleId),
      syncModuleIds: contracts.map((contract) => contract.moduleId),
    });
    expect(placeholders.valid, JSON.stringify(placeholders.diagnostics)).toBe(
      true,
    );
    expect(placeholders.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "TS6133" }),
    );
    expect(placeholders.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "TS6192" }),
    );
    const scopedPlaceholders = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: contracts.map((contract) => contract.moduleId),
      syncModuleIds: contracts.map((contract) => contract.moduleId),
      scopeToModuleIds: true,
      baselineUnselected: true,
    });
    expect(
      scopedPlaceholders.valid,
      JSON.stringify(scopedPlaceholders.diagnostics),
    ).toBe(true);
    expect(scopedPlaceholders.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "TS6133" }),
    );
    const dependencyClosure = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: [slug.moduleId, store.moduleId],
      syncModuleIds: [slug.moduleId],
      scopeToModuleIds: true,
      baselineUnselected: true,
    });
    expect(
      dependencyClosure.valid,
      JSON.stringify(dependencyClosure.diagnostics),
    ).toBe(true);
    expect(dependencyClosure.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "TS6133" }),
    );
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [slug.moduleId]:
          "const __jaunt_impl_slugify = (title: string, options?: { readonly trim?: boolean }): string => { void options; return title.trim(); };",
        [store.moduleId]: `class __jaunt_impl_Store {
  readonly #values = new Map<string, string>();
  constructor(prefix?: string) { void prefix; }
  put(key: string, value: string): void { this.#values.set(key, value); }
  get(key: string): string | null { return this.#values.get(key) ?? null; }
  get size(): number { return this.#values.size; }
}`,
      },
    });
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(result.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "TS6133" }),
    );
  });

  test("strict unused checks retain handwritten context parameter diagnostics", async () => {
    const workspace = createFixtureWorkspace({ withClass: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import { Store } from "../store/index.jaunt.js";
jaunt.magicModule({ deps: [Store] });
/** Normalize a title. */
export function slugify(title: string): string { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/store/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A strict store fixture. */
export class Store {
  constructor(prefix?: string) { jaunt.magic(); }
  /** Keep this body. @jauntPreserve */
  handwritten(unused: string): string { return "fixed"; }
  /** Generate this body. */
  generated(value: string): string { return jaunt.magic(); }
}
`,
    );
    writeFileSync(
      resolve(workspace.root, "tsconfig.json"),
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            noUnusedParameters: true,
            types: [],
          },
          include: ["src/**/*.ts"],
          exclude: ["src/**/*.jaunt.ts", "src/**/__generated__/**"],
        },
        null,
        2,
      )}\n`,
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
    const contracts = session.analyzeContracts().modules;
    const slug = contracts.find(
      (contract) => contract.moduleId === "ts:src/slug/index",
    )!;
    const store = contracts.find(
      (contract) => contract.moduleId === "ts:src/store/index",
    )!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: [slug.moduleId, store.moduleId],
      syncModuleIds: [slug.moduleId],
      scopeToModuleIds: true,
      baselineUnselected: true,
    });

    expect(result.valid).toBe(false);
    expect(result.diagnostics).toContainEqual(
      expect.objectContaining({
        code: "TS6133",
        message: expect.stringContaining("'unused'"),
        path: "src/store/index.jaunt.ts",
      }),
    );
    expect(result.diagnostics).not.toContainEqual(
      expect.objectContaining({
        code: "TS6133",
        message: expect.stringMatching(/'(prefix|title|value)'/u),
      }),
    );
  });

  test("runs the supported analyzer path on the TypeScript 5.8 lower bound", async () => {
    const { session } = await sessionFor({
      compilerPackage: "@typescript/typescript58",
    });
    expect(session.initializeResult().typescriptVersion).toBe("5.8.3");
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    expect(session.analyzeContracts().modules).toHaveLength(1);
  });

  test.each(["auto", "force"] as const)(
    "scoped bootstrap ignores unrelated project diagnostics with %s module detection",
    async (moduleDetection) => {
      const workspace = createFixtureWorkspace();
      roots.push(workspace.root);
      writeFileSync(
        resolve(workspace.root, "src/unrelated.ts"),
        'const broken: number = "not-a-number";\nvoid broken;\n',
      );
      const tsconfigPath = resolve(workspace.root, "tsconfig.json");
      const tsconfig = JSON.parse(readFileSync(tsconfigPath, "utf8")) as {
        compilerOptions: Record<string, unknown>;
      };
      tsconfig.compilerOptions.moduleDetection = moduleDetection;
      writeFileSync(tsconfigPath, `${JSON.stringify(tsconfig, null, 2)}\n`);
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
      const contract = session.analyzeContracts().modules[0]!;
      const metadata = session.metadata();

      const scoped = session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: {},
        moduleIds: [contract.moduleId],
        syncModuleIds: [contract.moduleId],
        scopeToModuleIds: true,
      });
      const full = session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: {},
        moduleIds: [contract.moduleId],
        syncModuleIds: [contract.moduleId],
      });

      expect(scoped.valid, JSON.stringify(scoped.diagnostics)).toBe(true);
      expect(full.valid).toBe(false);
      expect(full.diagnostics).toContainEqual(
        expect.objectContaining({ code: "TS2322", path: "src/unrelated.ts" }),
      );
    },
  );

  test("scoped bootstrap honors JSX auto-module detection", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const packagePath = resolve(workspace.root, "package.json");
    const packageJson = JSON.parse(readFileSync(packagePath, "utf8")) as Record<
      string,
      unknown
    >;
    packageJson.type = "commonjs";
    writeFileSync(packagePath, `${JSON.stringify(packageJson, null, 2)}\n`);
    writeFileSync(
      resolve(workspace.root, "src/unrelated.jsx"),
      '/** @type {number} */ const broken = "not-a-number";\nconst view = <div />;\nvoid broken; void view;\n',
    );
    const tsconfigPath = resolve(workspace.root, "tsconfig.json");
    const tsconfig = JSON.parse(readFileSync(tsconfigPath, "utf8")) as {
      compilerOptions: Record<string, unknown>;
      include: string[];
    };
    Object.assign(tsconfig.compilerOptions, {
      allowJs: true,
      checkJs: true,
      jsx: "react-jsx",
      moduleDetection: "auto",
    });
    tsconfig.include = ["src/**/*"];
    writeFileSync(tsconfigPath, `${JSON.stringify(tsconfig, null, 2)}\n`);
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
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const request = {
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: [contract.moduleId],
      syncModuleIds: [contract.moduleId],
    } as const;

    const scoped = session.validateOverlay({
      ...request,
      scopeToModuleIds: true,
    });
    const full = session.validateOverlay(request);

    expect(scoped.valid, JSON.stringify(scoped.diagnostics)).toBe(true);
    expect(full.valid).toBe(false);
    expect(full.diagnostics).toContainEqual(
      expect.objectContaining({ path: "src/unrelated.jsx" }),
    );
  });

  test("scoped bootstrap includes Jaunt modules referenced by public type imports", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    mkdirSync(resolve(workspace.root, "src/model"), { recursive: true });
    writeFileSync(
      resolve(workspace.root, "src/model/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
export interface Model { readonly value: string; }
/** Create a model. */
export function makeModel(value: string): Model { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/model/barrel.ts"),
      'export type { Model } from "./index.js";\n',
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import type { Model } from "../model/barrel.js";
jaunt.magicModule();
/** Read a model value. */
export function slugify(model: Model): string { return jaunt.magic(); }
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
    const contracts = session.analyzeContracts({
      moduleIds: ["ts:src/slug/index"],
    }).modules;
    expect(contracts.map((contract) => contract.moduleId).sort()).toEqual([
      "ts:src/model/index",
      "ts:src/slug/index",
    ]);
    const metadata = session.metadata();
    const scoped = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: contracts.map((contract) => contract.moduleId),
      syncModuleIds: contracts.map((contract) => contract.moduleId),
      scopeToModuleIds: true,
    });
    expect(scoped.valid, JSON.stringify(scoped.diagnostics)).toBe(true);
  });

  test("scoped bootstrap retains configured global declaration roots", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/globals.ts"),
      "export {};\ndeclare global { type AmbientText = string; }\n",
    );
    writeFileSync(
      resolve(workspace.root, "src/global-script.ts"),
      `interface AmbientMarker { readonly marker?: true; }
class AmbientClass { readonly value = "ambient"; }
function ambientFunction(): AmbientClass { return new AmbientClass(); }
const ambientValue = ambientFunction();
`,
    );
    const tsconfigPath = resolve(workspace.root, "tsconfig.json");
    const tsconfig = JSON.parse(readFileSync(tsconfigPath, "utf8")) as {
      compilerOptions: Record<string, unknown>;
    };
    tsconfig.compilerOptions.moduleDetection = "legacy";
    writeFileSync(tsconfigPath, `${JSON.stringify(tsconfig, null, 2)}\n`);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return ambient text unchanged. */
export function slugify(
  value: AmbientText & AmbientMarker & AmbientClass,
  current: typeof ambientValue,
): ReturnType<typeof ambientFunction> { return jaunt.magic(); }
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
    const contracts = session.analyzeContracts({
      moduleIds: ["ts:src/slug/index"],
    }).modules;
    const metadata = session.metadata();
    const scoped = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: contracts.map((contract) => contract.moduleId),
      syncModuleIds: contracts.map((contract) => contract.moduleId),
      scopeToModuleIds: true,
    });
    expect(scoped.valid, JSON.stringify(scoped.diagnostics)).toBe(true);
  });

  test("landing validation keeps unselected modules on their committed baseline", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    mkdirSync(resolve(workspace.root, "src/value"), { recursive: true });
    writeFileSync(
      resolve(workspace.root, "src/value/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return a string value. */
export function value(input: string): string { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/consumer.ts"),
      `import { value } from "./value/index.js";
export const consumed: string = value("committed");
`,
    );
    const params = {
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
    } as const;
    const original = await AnalyzerSession.create(params);
    const originalContracts = original.analyzeContracts().modules;
    const originalMetadata = original.metadata();
    const bootstrap = original.validateOverlay({
      sessionId: originalMetadata.sessionId,
      expectedEpoch: originalMetadata.epoch,
      expectedSnapshot: originalMetadata.snapshot,
      candidates: {},
      moduleIds: originalContracts.map((contract) => contract.moduleId),
      syncModuleIds: originalContracts.map((contract) => contract.moduleId),
    });
    expect(bootstrap.valid, JSON.stringify(bootstrap.diagnostics)).toBe(true);
    commit(workspace.root, bootstrap.artifacts);

    writeFileSync(
      resolve(workspace.root, "src/value/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Return a numeric value. */
export function value(input: number): number { return jaunt.magic(); }
`,
    );
    const refreshed = await AnalyzerSession.create(params);
    const slug = refreshed
      .analyzeContracts()
      .modules.find((contract) => contract.moduleId === "ts:src/slug/index")!;
    const metadata = refreshed.metadata();
    const request = {
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: [slug.moduleId],
      syncModuleIds: [slug.moduleId],
    } as const;
    const prospective = refreshed.validateOverlay(request);
    const baseline = refreshed.validateOverlay({
      ...request,
      baselineUnselected: true,
    });

    expect(prospective.valid).toBe(false);
    expect(prospective.diagnostics).toContainEqual(
      expect.objectContaining({ code: "TS2345", path: "src/consumer.ts" }),
    );
    expect(baseline.valid, JSON.stringify(baseline.diagnostics)).toBe(true);

    const value = refreshed
      .analyzeContracts({ moduleIds: ["ts:src/value/index"] })
      .modules.find((contract) => contract.moduleId === "ts:src/value/index")!;
    const scopedSync = refreshed.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      moduleIds: [value.moduleId],
      syncModuleIds: [value.moduleId],
      scopeToModuleIds: true,
      baselineUnselected: true,
    });
    expect(scopedSync.valid).toBe(false);
    expect(scopedSync.diagnostics).toContainEqual(
      expect.objectContaining({ path: "src/consumer.ts" }),
    );

    const scopedCandidate = refreshed.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [value.moduleId]:
          "const __jaunt_impl_value = (input: number): number => input;",
      },
      moduleIds: [value.moduleId],
      scopeToModuleIds: true,
      baselineUnselected: true,
    });
    expect(scopedCandidate.valid).toBe(false);
    expect(scopedCandidate.diagnostics).toContainEqual(
      expect.objectContaining({ path: "src/consumer.ts" }),
    );
  });

  test("scoped candidate validation excludes consumers of unchanged dependencies", async () => {
    const workspace = createFixtureWorkspace({ withClass: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import { Store } from "../store/index.jaunt.js";
jaunt.magicModule({ deps: [Store] });
/** Normalize a title. */
export function slugify(title: string): string { return jaunt.magic(); }
`,
    );
    const params = {
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
    } as const;
    const original = await AnalyzerSession.create(params);
    const originalModules = original.analyzeContracts().modules;
    const originalMetadata = original.metadata();
    const bootstrap = original.validateOverlay({
      sessionId: originalMetadata.sessionId,
      expectedEpoch: originalMetadata.epoch,
      expectedSnapshot: originalMetadata.snapshot,
      candidates: {},
      moduleIds: originalModules.map((module) => module.moduleId),
      syncModuleIds: originalModules.map((module) => module.moduleId),
    });
    expect(bootstrap.valid, JSON.stringify(bootstrap.diagnostics)).toBe(true);
    commit(workspace.root, bootstrap.artifacts);

    writeFileSync(
      resolve(workspace.root, "src/store-consumer.ts"),
      `import { Store } from "./store/index.js";
export const invalid: number = new Store().get("missing");
`,
    );
    const refreshed = await AnalyzerSession.create(params);
    const modules = refreshed.analyzeContracts().modules;
    const slug = modules.find(
      (module) => module.moduleId === "ts:src/slug/index",
    )!;
    const metadata = refreshed.metadata();
    const candidate = refreshed.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [slug.moduleId]:
          "const __jaunt_impl_slugify = (title: string): string => title.trim();",
      },
      moduleIds: modules.map((module) => module.moduleId),
      scopeToModuleIds: true,
      baselineUnselected: true,
    });

    expect(candidate.valid, JSON.stringify(candidate.diagnostics)).toBe(true);
    expect(candidate.diagnostics).not.toContainEqual(
      expect.objectContaining({ path: "src/store-consumer.ts" }),
    );
  });

  test("discovers, renders IR, and validates an unbuilt sync transaction", async () => {
    const { session } = await sessionFor();
    const workspace = session.analyzeWorkspace();
    expect(workspace.diagnostics).toEqual([]);
    expect(workspace.routes).toEqual([
      expect.objectContaining({
        moduleId: "ts:src/slug/index",
        facadePath: "src/slug/index.ts",
        apiMirrorPath: "src/slug/__generated__/index.api.ts",
        implementationPath: "src/slug/__generated__/index.ts",
      }),
    ]);
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.apiSource).toContain(
      "export declare function slugify(title: string): string;",
    );
    expect(contract.apiSource).not.toContain(".jaunt");
    expect(contract.placeholderSource).toContain("jaunt:state=unbuilt");

    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      syncModuleIds: [contract.moduleId],
    });
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(result.artifacts.map((artifact) => artifact.kind).sort()).toEqual([
      "api-mirror",
      "facade",
      "placeholder",
      "sidecar",
    ]);
    expect(
      result.artifacts.find((artifact) => artifact.kind === "sidecar")?.content,
    ).toContain('"state": "unbuilt"');
  });

  test("accepts a valid reserved binding and rejects narrowing without writes", async () => {
    const { workspace, session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const valid = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          'const __jaunt_impl_slugify = (title: string): string => title.trim().toLowerCase().replace(/\\s+/g, "-");',
      },
    });
    expect(valid.valid, JSON.stringify(valid.diagnostics)).toBe(true);
    expect(
      valid.artifacts.find((artifact) => artifact.kind === "implementation")
        ?.content,
    ).toContain("export const slugify: typeof __JauntApi.slugify");
    expect(
      valid.artifacts.find((artifact) => artifact.kind === "implementation")
        ?.content,
    ).toContain(
      'Object.defineProperty(__jaunt_impl_slugify, "name", { value: "slugify", configurable: true });',
    );
    const repeated = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          'const __jaunt_impl_slugify = (title: string): string => title.trim().toLowerCase().replace(/\\s+/g, "-");',
      },
    });
    expect(repeated.valid, JSON.stringify(repeated.diagnostics)).toBe(true);
    const aliased = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          "const shared = (title: string): string => title.trim(); const __jaunt_impl_slugify = shared;",
      },
    });
    expect(aliased.valid).toBe(false);
    expect(aliased.diagnostics).toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_FUNCTION_ALIAS" }),
    );
    expect(
      session
        .overlayProgramState()
        .some((state) => state.reusedSourceFiles > 0),
    ).toBe(true);
    expect(() =>
      session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: "sha256:stale",
        candidates: {},
        releasePrograms: true,
      }),
    ).toThrow("Workspace changed after analysis");
    expect(session.overlayProgramState()).toEqual([]);
    const released = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          'const __jaunt_impl_slugify = (title: string): string => title.trim().toLowerCase().replace(/\\s+/g, "-");',
      },
      releasePrograms: true,
    });
    expect(released.valid, JSON.stringify(released.diagnostics)).toBe(true);
    expect(session.overlayProgramState()).toEqual([]);
    commit(workspace.root, valid.artifacts);

    const refreshed = session.invalidate({
      paths: valid.artifacts.map((artifact) => artifact.path),
    });
    const invalid = session.validateOverlay({
      sessionId: refreshed.sessionId,
      expectedEpoch: refreshed.epoch,
      expectedSnapshot: refreshed.snapshot,
      candidates: {
        [contract.moduleId]:
          "const __jaunt_impl_slugify = (title: number): string => String(title);",
      },
    });
    expect(invalid.valid).toBe(false);
    expect(invalid.artifacts).toEqual([]);
    expect(
      invalid.diagnostics.some((diagnostic) =>
        diagnostic.code.startsWith("TS"),
      ),
    ).toBe(true);
  });

  test("rejects artifacts when a tracked input changes during validation", async () => {
    const { workspace, session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const candidate =
      "const __jaunt_impl_slugify = (title: string): string => title.trim();";
    let mutated = false;
    const candidates = new Proxy(
      { [contract.moduleId]: candidate },
      {
        get(target, property, receiver) {
          if (property === contract.moduleId && !mutated) {
            mutated = true;
            writeFileSync(
              resolve(workspace.root, "src/app.ts"),
              'export const changedDuringValidation = "sentinel";\n',
            );
          }
          return Reflect.get(target, property, receiver) as string;
        },
      },
    );

    expect(() =>
      session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates,
      }),
    ).toThrowError(
      expect.objectContaining({
        payload: expect.objectContaining({ code: "STALE_SESSION" }),
      }),
    );
    expect(mutated).toBe(true);
    expect(session.metadata()).toEqual(metadata);
  });

  test("rejects candidate type declarations that shadow public API types", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Public options. */
export interface SlugOptions { readonly prefix: string; }
/** Create a slug. */
export function slugify(title: string): string { return jaunt.magic(); }
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
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `type SlugOptions = { readonly prefix: string };
const __jaunt_impl_slugify = (title: string): string => title;`,
      },
    });

    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
    expect(result.diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "JAUNT_TS_PUBLIC_TYPE_SHADOW" }),
      ]),
    );
  });

  test("rejects imported local aliases that shadow public API types", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Public options. */
export interface SlugOptions { readonly prefix: string; }
/** Create a slug. */
export function slugify(title: string): string { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/external.ts"),
      "export interface ExternalOptions { readonly prefix: string; }\n",
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
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `import type { ExternalOptions as SlugOptions } from "../external.js";
const __jaunt_impl_slugify = (title: string): string => title;`,
      },
    });

    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
    expect(result.diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "JAUNT_TS_PUBLIC_TYPE_SHADOW",
          message: expect.stringContaining(
            "import a local binding named SlugOptions",
          ),
        }),
      ]),
    );
  });

  test("rejects imported aliases that shadow a public class type", async () => {
    const { workspace, session } = await sessionFor({ withClass: true });
    writeFileSync(
      resolve(workspace.root, "src/store/external.ts"),
      "export interface ExternalStore { readonly id: string; }\n",
    );
    const contract = session
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:src/store/index")!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `import type { ExternalStore as Store } from "../external.js";
class __jaunt_impl_Store {
  constructor(_prefix?: string) {}
  put(_key: string, _value: string): void {}
  get(_key: string): string | null { return null; }
  get size(): number { return 0; }
}`,
      },
    });

    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
    expect(result.diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          code: "JAUNT_TS_PUBLIC_TYPE_SHADOW",
          message: expect.stringContaining(
            "import a local binding named Store",
          ),
        }),
      ]),
    );
  });

  test("validates and returns one atomic artifact batch for independent modules", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const secondSpec = resolve(workspace.root, "src/case/index.jaunt.ts");
    mkdirSync(dirname(secondSpec), { recursive: true });
    writeFileSync(
      secondSpec,
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Uppercase one value without changing its length. */
export function upper(value: string): string { return jaunt.magic(); }
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
      toolVersion: "0.1.0-alpha.0",
    });
    const modules = session.analyzeContracts().modules;
    expect(modules.map((module) => module.moduleId)).toEqual([
      "ts:src/case/index",
      "ts:src/slug/index",
    ]);
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      moduleIds: modules.map((module) => module.moduleId),
      candidates: {
        "ts:src/case/index":
          "const __jaunt_impl_upper = (value: string): string => value.toUpperCase();",
        "ts:src/slug/index":
          'const __jaunt_impl_slugify = (title: string): string => title.trim().replace(/\\s+/g, "-");',
      },
    });

    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(
      result.artifacts
        .filter((artifact) => artifact.kind === "implementation")
        .map((artifact) => artifact.moduleId)
        .sort(),
    ).toEqual(["ts:src/case/index", "ts:src/slug/index"]);
    expect(
      new Set(result.artifacts.map((artifact) => artifact.path)).size,
    ).toBe(result.artifacts.length);
  });

  test("rejects output and case-folded route collisions before generation", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.tsx"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Collides with the .ts spec's output path. */
export function other(value: string): string { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/Index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Collides case-insensitively with another route. */
export function folded(value: string): string { return jaunt.magic(); }
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
      toolVersion: "0.1.0-alpha.0",
    });

    const collisions = session
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.code === "JAUNT_TS_ROUTE_COLLISION");
    expect(collisions.length).toBeGreaterThanOrEqual(2);
  });

  test("rejects inferred-any and double-assertion boundary escapes", async () => {
    const { session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const inferredAny = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: 'const __jaunt_impl_slugify = JSON.parse("{}");',
      },
    });
    expect(inferredAny.valid).toBe(false);
    expect(
      inferredAny.diagnostics.some(
        (item) => item.code === "JAUNT_TS_BOUNDARY_ANY",
      ),
    ).toBe(true);
    const doubleAssertion = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          "const __jaunt_impl_slugify = ((title: string) => title) as unknown as (title: string) => string;",
      },
    });
    expect(doubleAssertion.valid).toBe(false);
    expect(
      doubleAssertion.diagnostics.some(
        (item) => item.code === "JAUNT_TS_DOUBLE_ASSERTION",
      ),
    ).toBe(true);
  });

  test("rejects element and parenthesized CommonJS export assignments", async () => {
    const { session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const candidates = [
      'module["exports"] = {};',
      "module[`exports`] = {};",
      '(module["exports"]).slugify = () => "bypass";',
      'module["exports"]["slugify"] = () => "bypass";',
      'const exportKey = "exports"; module[exportKey] = {};',
      'Object.assign(module.exports, { slugify: () => "bypass" });',
      'Object.defineProperty(exports, "slugify", { value: () => "bypass" });',
    ];

    for (const assignment of candidates) {
      const result = session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: {
          [contract.moduleId]: `${assignment}\nconst __jaunt_impl_slugify = (title: string): string => title;`,
        },
      });
      expect(result.valid).toBe(false);
      expect(result.artifacts).toEqual([]);
      expect(result.diagnostics).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ code: "JAUNT_TS_MODEL_EXPORT" }),
        ]),
      );
    }
  });

  test("generated candidates may not retain Jaunt tooling imports", async () => {
    const { session } = await sessionFor();
    expect(
      session
        .analyzeWorkspace()
        .diagnostics.filter(
          (item) => item.code === "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
        ),
    ).toEqual([]);
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const candidates = [
      'import * as tooling from "@usejaunt/ts"; void tooling;',
      'const tooling = import("@usejaunt/ts/spec"); void tooling;',
      'const tooling = require("@usejaunt/ts"); void tooling;',
      'import tooling = require("@usejaunt/ts/spec"); void tooling;',
      'type Tooling = import("@usejaunt/ts").MagicOptions; void (0 as unknown as Tooling);',
      '/// <reference types="@usejaunt/ts" />',
    ];
    for (const prefix of candidates) {
      const result = session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: {
          [contract.moduleId]: `${prefix}
const __jaunt_impl_slugify = (title: string): string => title;`,
        },
      });
      expect(result.valid).toBe(false);
      expect(result.artifacts).toEqual([]);
      expect(result.diagnostics).toContainEqual(
        expect.objectContaining({
          code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
        }),
      );
    }

    const authored = await sessionFor({ withTestSpec: true });
    expect(
      authored.session
        .analyzeWorkspace()
        .diagnostics.filter(
          (item) => item.code === "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
        ),
    ).toEqual([]);
  });

  test("generated candidates audit package imports aliases by logical target", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const manifest = JSON.parse(
      readFileSync(resolve(workspace.root, "package.json"), "utf8"),
    ) as Record<string, unknown>;
    manifest.imports = {
      "#tooling": "@usejaunt/ts/spec",
      "#external": "not-declared-package",
      "#internal": "./src/internal.js",
    };
    writeFileSync(
      resolve(workspace.root, "package.json"),
      `${JSON.stringify(manifest, null, 2)}\n`,
    );
    writeFileSync(
      resolve(workspace.root, "src/internal.ts"),
      "export const normalize = (value: string): string => value.trim();\n",
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
      toolVersion: "0.1.0-alpha.0",
    });
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const validate = (candidate: string) =>
      session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: { [contract.moduleId]: candidate },
      });

    const tooling = validate(`import * as tooling from "#tooling";
void tooling;
const __jaunt_impl_slugify = (title: string): string => title;`);
    expect(tooling.valid).toBe(false);
    expect(tooling.artifacts).toEqual([]);
    expect(tooling.diagnostics).toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT" }),
    );

    const external = validate(`import external from "#external";
void external;
const __jaunt_impl_slugify = (title: string): string => title;`);
    expect(external.valid).toBe(false);
    expect(external.artifacts).toEqual([]);
    expect(external.diagnostics).toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
    );

    const internal = validate(`import { normalize } from "#internal";
const __jaunt_impl_slugify = (title: string): string => normalize(title);`);
    expect(internal.valid, JSON.stringify(internal.diagnostics)).toBe(true);
    expect(internal.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
    );
  });

  test("rejects every retained triple-slash directive in generated implementations", async () => {
    const directives = [
      ['/// <reference types="node" />', 1],
      ['///\u00a0<Reference path="./types.d.ts" />', 1],
      ['/// <reference path="../private.jaunt.ts" />', 1],
      ['/// <reference lib="es2022" />', 1],
      ['/// <amd-dependency path="legacy-dependency" name="legacy" />', 1],
      ['\u0085/// <AMD-MODULE name="legacy-module" />', 2],
      ['/// <reference no-default-lib="true" />', 1],
      ['/*lead*/ /// <Reference path="./preceded.d.ts" />', 10],
    ] as const;

    for (const compilerPackage of [
      "@typescript/typescript58",
      "@typescript/typescript6",
    ] as const) {
      const { session } = await sessionFor({ compilerPackage });
      const contract = session.analyzeContracts().modules[0]!;
      const metadata = session.metadata();
      for (const [directive, column] of directives) {
        const result = session.validateOverlay({
          sessionId: metadata.sessionId,
          expectedEpoch: metadata.epoch,
          expectedSnapshot: metadata.snapshot,
          candidates: {
            [contract.moduleId]: `${directive}
const __jaunt_impl_slugify = (title: string): string => title;`,
          },
        });
        const caseName = `${compilerPackage}: ${directive}`;
        expect(result.valid, caseName).toBe(false);
        expect(result.artifacts, caseName).toEqual([]);
        expect(result.diagnostics, caseName).toEqual(
          expect.arrayContaining([
            expect.objectContaining({
              code: "JAUNT_TS_TRIPLE_SLASH_DIRECTIVE",
              line: 1,
              column,
            }),
          ]),
        );
      }
    }
  }, 30_000);

  test("audits createRequire results and imported aliases as require calls", async () => {
    const { session, workspace } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const candidates = [
      `import { createRequire } from "node:module";
const req = createRequire(import.meta.url);`,
      `import { createRequire as makeRequire } from "node:module";
const importedRequire = makeRequire;
const req = importedRequire(import.meta.url);`,
      `import * as moduleApi from "node:module";
const makeRequire = moduleApi["createRequire"];
const req = makeRequire(import.meta.url);`,
      `const { createRequire: makeRequire } = require("node:module");
const req = makeRequire(import.meta.url);`,
      `import moduleApi = require("node:module");
const req = moduleApi.createRequire(import.meta.url);`,
      `const { createRequire: makeRequire } = await import("node:module");
const req = makeRequire(import.meta.url);`,
      `const { require: req } = module;`,
    ];

    for (const loader of candidates) {
      const result = composeCandidate(
        ts,
        workspace.root,
        contract,
        `${loader}
const dep = req("not-declared");
const __jaunt_impl_slugify = (title: string): string => dep(title);`,
      );
      expect(result.diagnostics).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
        ]),
      );
      expect(
        result.diagnostics.some(
          (item) => item.code === "JAUNT_TS_DYNAMIC_IMPORT",
        ),
      ).toBe(false);
    }

    const inlineCandidates = [
      `import { createRequire } from "node:module";
const dep = createRequire(import.meta.url)("not-declared");`,
      `import * as moduleApi from "node:module";
const dep = moduleApi.createRequire(import.meta.url)("not-declared");`,
    ];
    for (const loader of inlineCandidates) {
      const result = composeCandidate(
        ts,
        workspace.root,
        contract,
        `${loader}
const __jaunt_impl_slugify = (title: string): string => dep(title);`,
      );
      expect(result.diagnostics).toEqual(
        expect.arrayContaining([
          expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
        ]),
      );
      expect(
        result.diagnostics.some(
          (item) => item.code === "JAUNT_TS_DYNAMIC_IMPORT",
        ),
      ).toBe(false);
    }
  });

  test("recovers current and alpha.0 candidates for deterministic recomposition", async () => {
    const { session, workspace } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const candidate =
      "const __jaunt_impl_slugify = (title: string): string => title.toLowerCase();";
    const composed = composeCandidate(ts, workspace.root, contract, candidate);
    expect(composed.diagnostics).toEqual([]);

    const current = decomposeGeneratedImplementation(
      ts,
      contract,
      composed.source,
    );
    const legacy = decomposeGeneratedImplementation(
      ts,
      contract,
      composed.source.replace(
        /Object\.defineProperty\(__jaunt_impl_slugify[^\n]*\);\n/,
        "",
      ),
    );

    expect(current.diagnostics).toEqual([]);
    expect(legacy.diagnostics).toEqual([]);
    expect(current.candidateSource?.trim()).toBe(candidate);
    expect(legacy.candidateSource?.trim()).toBe(candidate);
  });

  test("revalidates and recomposes a committed implementation without a candidate", async () => {
    const { session, workspace } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const candidate =
      "const __jaunt_impl_slugify = (title: string): string => title.toLowerCase();";
    const built = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: { [contract.moduleId]: candidate },
    });
    expect(built.valid, JSON.stringify(built.diagnostics)).toBe(true);
    commit(workspace.root, built.artifacts);

    const refreshed = session.invalidate({
      paths: built.artifacts.map((artifact) => artifact.path),
    });
    const recomposed = session.validateOverlay({
      sessionId: refreshed.sessionId,
      expectedEpoch: refreshed.epoch,
      expectedSnapshot: refreshed.snapshot,
      moduleIds: [contract.moduleId],
      candidates: {},
      recomposeModuleIds: [contract.moduleId],
    });

    expect(recomposed.valid, JSON.stringify(recomposed.diagnostics)).toBe(true);
    const implementation = recomposed.artifacts.find(
      (artifact) => artifact.kind === "implementation",
    )?.content;
    expect(implementation).toContain(candidate);
    expect(implementation).toContain(
      'Object.defineProperty(__jaunt_impl_slugify, "name", { value: "slugify", configurable: true });',
    );
  });

  test("recomposition masks preserved bodies and does not duplicate their docs", async () => {
    const { workspace } = await sessionFor();
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A formatter with one audited handwritten operation. */
export class Formatter {
  constructor() { jaunt.magic(); }
  /** Preserve this cast exactly. @jauntPreserve */
  format(value: string): string { return (value as any).trim(); }
  /** Normalize a formatted value. */
  normalize(value: string): string { return jaunt.magic(); }
}
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/app.ts"),
      `import { Formatter } from "./slug/index.js";
export const result = new Formatter().format(" value ");
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
      toolVersion: "0.1.0-alpha.0",
    });
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const built = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `class __jaunt_impl_Formatter {
  constructor() {}
  format(value: string): string { return value; }
  normalize(value: string): string { return value.toLowerCase(); }
}`,
      },
    });
    expect(built.valid, JSON.stringify(built.diagnostics)).toBe(true);
    commit(workspace.root, built.artifacts);

    let refreshed = session.invalidate({
      paths: built.artifacts.map((artifact) => artifact.path),
    });
    for (let attempt = 0; attempt < 2; attempt += 1) {
      const recomposed = session.validateOverlay({
        sessionId: refreshed.sessionId,
        expectedEpoch: refreshed.epoch,
        expectedSnapshot: refreshed.snapshot,
        moduleIds: [contract.moduleId],
        candidates: {},
        recomposeModuleIds: [contract.moduleId],
      });
      expect(recomposed.valid, JSON.stringify(recomposed.diagnostics)).toBe(
        true,
      );
      const implementation = recomposed.artifacts.find(
        (artifact) => artifact.kind === "implementation",
      )!.content;
      expect(implementation).toContain("return (value as any).trim();");
      expect(implementation.match(/Preserve this cast exactly/g)).toHaveLength(
        1,
      );
      commit(workspace.root, recomposed.artifacts);
      refreshed = session.invalidate({
        paths: recomposed.artifacts.map((artifact) => artifact.path),
      });
    }
  });

  test("rejects indirect loader invocation and noncanonical createRequire bases", async () => {
    const { session, workspace } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const loader = `import { createRequire } from "node:module";
const direct = createRequire(import.meta.url);`;
    const candidates = [
      'const load = direct.bind(null); const dep = load("not-declared");',
      'const dep = direct.call(null, "not-declared");',
      'const dep = direct.apply(null, ["not-declared"]);',
      'const load = true ? direct : direct; const dep = load("not-declared");',
      'const [load] = [direct]; const dep = load("not-declared");',
      'const { load } = { load: direct }; const dep = load("not-declared");',
      'const dep = Reflect.apply(direct, null, ["not-declared"]);',
      'let load: typeof direct; ({ load } = { load: direct }); const dep = load("not-declared");',
      'const dep = (0, direct)("not-declared");',
      'const load = module.require.bind(module); const dep = load("not-declared");',
      'const load = createRequire(import.meta.url).bind(null); const dep = load("not-declared");',
      'const load = require("node:module").createRequire.bind(null); const dep = load(import.meta.url)("not-declared");',
      'const moduleApi = true ? require("node:module") : require("node:module"); const dep = moduleApi.createRequire(import.meta.url)("not-declared");',
      'const dep = import("node:module").then(({ createRequire: makeRequire }) => makeRequire(import.meta.url)("not-declared"));',
    ];
    for (const route of candidates) {
      const result = composeCandidate(
        ts,
        workspace.root,
        contract,
        `${loader}
${route}
const __jaunt_impl_slugify = (title: string): string => title;`,
      );
      expect(result.diagnostics).toContainEqual(
        expect.objectContaining({ code: "JAUNT_TS_DYNAMIC_IMPORT" }),
      );
    }

    const noncanonical = composeCandidate(
      ts,
      workspace.root,
      contract,
      `import { createRequire } from "node:module";
const req = createRequire(new URL(".", import.meta.url));
const dep = req("not-declared");
const __jaunt_impl_slugify = (title: string): string => title;`,
    );
    expect(noncanonical.diagnostics).toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_DYNAMIC_IMPORT" }),
    );
  });

  test("class adapters reject a narrowed method despite TypeScript bivariance", async () => {
    const { session } = await sessionFor({ withClass: true });
    const contract = session
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:src/store/index")!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `class __jaunt_impl_Store {
  constructor(_prefix?: string) {}
  put(_key: "only", _value: string): void {}
  get(_key: string): string | null { return null; }
  get size(): number { return 0; }
}`,
      },
    });
    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
  });

  test("accepts a concrete class with the exact authored surface", async () => {
    const { session } = await sessionFor({ withClass: true });
    const contract = session
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:src/store/index")!;
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]: `class __jaunt_impl_Store {
  readonly #values = new Map<string, string>();
  constructor(_prefix?: string) {}
  put(key: string, value: string): void { this.#values.set(key, value); }
  get(key: string): string | null { return this.#values.get(key) ?? null; }
  get size(): number { return this.#values.size; }
}`,
      },
    });
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
  });

  test("test specs are analyzed through their generated-test owner but excluded from emit", async () => {
    const { session } = await sessionFor({ withTestSpec: true });
    const result = session.analyzeWorkspace();
    expect(result.diagnostics, JSON.stringify(result.diagnostics)).toEqual([]);
    expect(result.testSpecs).toEqual([
      expect.objectContaining({
        path: "tests/slug.jaunt-test.ts",
        project: "tsconfig.test.json",
        targets: ["ts:src/slug/index#slugify"],
      }),
    ]);
  });

  test("test ownership uses the configured generated directory", async () => {
    const workspace = createFixtureWorkspace({ withTestSpec: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "tsconfig.test.json"),
      `${JSON.stringify(
        {
          extends: "./tsconfig.json",
          compilerOptions: { noEmit: true },
          include: ["tests/machine/**/*.ts"],
          exclude: ["tests/**/*.jaunt-test.ts"],
        },
        null,
        2,
      )}\n`,
    );
    const session = await AnalyzerSession.create({
      root: workspace.root,
      projects: ["tsconfig.json"],
      testProjects: ["tsconfig.test.json"],
      sourceRoots: ["src"],
      testRoots: ["tests"],
      generatedDir: "machine",
      toolOwner: ".",
      compilerModulePath: workspace.compilerModulePath,
      clientVersion: "test",
      toolVersion: "test",
    });
    expect(session.analyzeWorkspace().testSpecs[0]?.project).toBe(
      "tsconfig.test.json",
    );
  });

  test("resolves aliased test targets to stable module export IDs", async () => {
    const workspace = createFixtureWorkspace({ withTestSpec: true });
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "tests/slug.jaunt-test.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import { slugify as makeSlug } from "../src/slug/index.jaunt.js";
jaunt.magicModule();
/** Slugifies a title. */
export function slugifies(): never {
  return jaunt.testSpec({ targets: [makeSlug] });
}
`,
    );
    const session = await AnalyzerSession.create({
      root: workspace.root,
      projects: ["tsconfig.json"],
      testProjects: ["tsconfig.test.json"],
      sourceRoots: ["src"],
      testRoots: ["tests"],
      generatedDir: "__generated__",
      toolOwner: ".",
      compilerModulePath: workspace.compilerModulePath,
      clientVersion: "test",
      toolVersion: "test",
    });
    expect(session.analyzeWorkspace().testSpecs[0]?.targets).toEqual([
      "ts:src/slug/index#slugify",
    ]);
  });

  test("normalizes symbol-qualified selections and finds artifacts after their spec is deleted", async () => {
    const { workspace, session } = await sessionFor();
    const selected = session.analyzeContracts({
      moduleIds: ["ts:src/slug/index#slugify"],
    });
    expect(selected.modules.map((module) => module.moduleId)).toEqual([
      "ts:src/slug/index",
    ]);
    const metadata = session.metadata();
    const sync = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {},
      syncModuleIds: ["ts:src/slug/index#slugify"],
    });
    expect(sync.valid, JSON.stringify(sync.diagnostics)).toBe(true);
    commit(workspace.root, sync.artifacts);
    writeFileSync(
      resolve(workspace.root, "src/slug/__generated__/index.example.test.ts"),
      "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.\n// jaunt:tier=example\n// jaunt:source=tests/index.jaunt-test.ts\nexport {};\n",
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/__generated__/index.derived.test.ts"),
      "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.\n// jaunt:tier=derived\n// jaunt:source=tests/index.jaunt-test.ts\nexport {};\n",
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/__generated__/index.contract.test.ts"),
      "// ⛓️ jaunt:generated — generated; do not edit.\n// jaunt:state=unbuilt\n// jaunt:module=ts:src/slug/index.contract.test\n",
    );
    rmSync(resolve(workspace.root, "src/slug/index.jaunt.ts"));

    const refreshed = await AnalyzerSession.create({
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
    expect(refreshed.findOrphans().artifacts).toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: "src/slug/__generated__/index.api.ts",
          kind: "api-mirror",
          moduleId: "ts:src/slug/index",
        }),
        expect.objectContaining({
          path: "src/slug/__generated__/index.ts",
          kind: "placeholder",
          moduleId: "ts:src/slug/index",
        }),
        expect.objectContaining({
          path: "src/slug/__generated__/index.jaunt.json",
          kind: "sidecar",
          moduleId: "ts:src/slug/index",
        }),
        expect.objectContaining({
          path: "src/slug/__generated__/index.contract.test.ts",
          kind: "placeholder",
          moduleId: "ts:src/slug/index.contract.test",
        }),
      ]),
    );
    expect(refreshed.findOrphans().artifacts).not.toEqual(
      expect.arrayContaining([
        expect.objectContaining({
          path: "src/slug/__generated__/index.example.test.ts",
        }),
        expect.objectContaining({
          path: "src/slug/__generated__/index.derived.test.ts",
        }),
      ]),
    );
  });

  test("plain sync preserves stale provenance and only explicit restamp advances it", async () => {
    const { workspace, session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const built = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          'const __jaunt_impl_slugify = (title: string): string => title.trim().toLowerCase().replace(/\\s+/g, "-");',
      },
    });
    expect(built.valid, JSON.stringify(built.diagnostics)).toBe(true);
    commit(workspace.root, built.artifacts);
    const implementationPath = resolve(
      workspace.root,
      contract.implementationPath,
    );
    const before = readFileSync(implementationPath, "utf8");
    writeFileSync(
      resolve(workspace.root, contract.specPath),
      readFileSync(resolve(workspace.root, contract.specPath), "utf8").replace(
        "Trim, lowercase, and replace whitespace runs with one dash.",
        "Create a normalized URL slug by trimming, lowercasing, and collapsing whitespace.",
      ),
    );

    const refreshed = await AnalyzerSession.create({
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
    const changed = refreshed.analyzeContracts().modules[0]!;
    const nextMetadata = refreshed.metadata();
    const synchronized = refreshed.validateOverlay({
      sessionId: nextMetadata.sessionId,
      expectedEpoch: nextMetadata.epoch,
      expectedSnapshot: nextMetadata.snapshot,
      candidates: {},
      syncModuleIds: [changed.moduleId],
    });
    expect(synchronized.valid, JSON.stringify(synchronized.diagnostics)).toBe(
      true,
    );
    expect(
      synchronized.artifacts.some(
        (artifact) => artifact.kind === "implementation",
      ),
    ).toBe(false);
    const synchronizedSidecar = synchronized.artifacts.find(
      (artifact) => artifact.kind === "sidecar",
    )!;
    expect(JSON.parse(synchronizedSidecar.content).proseDigest).toBe(
      contract.proseDigest,
    );
    expect(JSON.parse(synchronizedSidecar.content).proseDigest).not.toBe(
      changed.proseDigest,
    );
    commit(workspace.root, synchronized.artifacts);
    expect(readFileSync(implementationPath, "utf8")).toBe(before);

    const restampMetadata = refreshed.metadata();
    const restamped = refreshed.validateOverlay({
      sessionId: restampMetadata.sessionId,
      expectedEpoch: restampMetadata.epoch,
      expectedSnapshot: restampMetadata.snapshot,
      candidates: {},
      restampModuleIds: [changed.moduleId],
    });
    expect(restamped.valid, JSON.stringify(restamped.diagnostics)).toBe(true);
    const implementation = restamped.artifacts.find(
      (artifact) => artifact.kind === "implementation",
    );
    expect(implementation?.content).toContain(
      `// jaunt:prose=${changed.proseDigest}`,
    );
    const restampedSidecar = restamped.artifacts.find(
      (artifact) => artifact.kind === "sidecar",
    )!;
    expect(JSON.parse(restampedSidecar.content).proseDigest).toBe(
      changed.proseDigest,
    );
    const withoutRestampedHeaders = (source: string) =>
      source
        .split("\n")
        .filter(
          (line) => !/^\/\/ jaunt:(?:module|structural|prose|api)=/.test(line),
        )
        .join("\n");
    expect(withoutRestampedHeaders(implementation!.content)).toBe(
      withoutRestampedHeaders(before),
    );
  });

  test("restamp reruns current candidate policy without changing forbidden bytes", async () => {
    const { workspace, session } = await sessionFor();
    const contract = session.analyzeContracts().modules[0]!;
    const metadata = session.metadata();
    const built = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [contract.moduleId]:
          "const __jaunt_impl_slugify = (title: string): string => title.trim();",
      },
    });
    expect(built.valid, JSON.stringify(built.diagnostics)).toBe(true);
    commit(workspace.root, built.artifacts);
    const implementationPath = resolve(
      workspace.root,
      contract.implementationPath,
    );
    const forbidden = readFileSync(implementationPath, "utf8").replace(
      "title.trim()",
      "title as unknown as string",
    );
    writeFileSync(implementationPath, forbidden);

    const refreshed = await AnalyzerSession.create({
      root: workspace.root,
      projects: ["tsconfig.json"],
      testProjects: [],
      sourceRoots: ["src"],
      testRoots: ["tests"],
      generatedDir: "__generated__",
      toolOwner: ".",
      compilerModulePath: workspace.compilerModulePath,
      clientVersion: "test",
      toolVersion: "next-policy",
    });
    const changed = refreshed.analyzeContracts().modules[0]!;
    const refreshedMetadata = refreshed.metadata();
    const restamped = refreshed.validateOverlay({
      sessionId: refreshedMetadata.sessionId,
      expectedEpoch: refreshedMetadata.epoch,
      expectedSnapshot: refreshedMetadata.snapshot,
      candidates: {},
      restampModuleIds: [changed.moduleId],
    });

    expect(restamped.valid).toBe(false);
    expect(restamped.artifacts).toEqual([]);
    expect(restamped.diagnostics).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ code: "JAUNT_TS_DOUBLE_ASSERTION" }),
      ]),
    );
    expect(readFileSync(implementationPath, "utf8")).toBe(forbidden);
  });

  test("inline object contracts survive the mirror and corruption fails the independent proof", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Create a slug. */
export function slugify(title: string, opts?: { ttlSeconds?: number }): string { return jaunt.magic(); }
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
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.apiSource).toContain("opts?: { ttlSeconds?: number }");
    const graph = loadProjectGraph(ts, workspace.root, ["tsconfig.json"], []);
    const corrupted = contract.apiSource.replace(
      "{ ttlSeconds?: number }",
      "object",
    );
    const diagnostics = validateApiMirrorEquivalence(
      ts,
      workspace.root,
      graph.projects[0]!,
      contract,
      corrupted,
    );
    expect(diagnostics.some((item) => item.severity === "error")).toBe(true);
  });

  test("same-project spec dependencies resolve to stable symbol IDs", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    mkdirSync(resolve(workspace.root, "src/base"), { recursive: true });
    writeFileSync(
      resolve(workspace.root, "src/base/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Base operation. */
export function base(value: string): string { return jaunt.magic(); }
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import { base } from "../base/index.jaunt.js";
jaunt.magicModule();
/** Create a slug. */
export function slugify(title: string): string { return jaunt.magic({ deps: [base] }); }
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
    const consumer = session
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:src/slug/index")!;
    expect(consumer.dependencies).toEqual(["ts:src/base/index#base"]);
  });

  test("rejects TypeScript 7 with the stable-API diagnostic rather than not-found", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const fake = resolve(workspace.root, "fake-typescript-7.mjs");
    writeFileSync(fake, 'export default { version: "7.0.2" };\n');
    await expect(
      AnalyzerSession.create({
        root: workspace.root,
        projects: ["tsconfig.json"],
        testProjects: [],
        sourceRoots: ["src"],
        testRoots: ["tests"],
        generatedDir: "__generated__",
        toolOwner: ".",
        compilerModulePath: fake,
        clientVersion: "test",
        toolVersion: "test",
      }),
    ).rejects.toMatchObject({
      payload: expect.objectContaining({ code: "COMPILER_UNSUPPORTED" }),
    });
  });

  test("rejects runtime spec edges, context cycles, and generated-private imports", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.context.ts"),
      'import { slugify } from "./index.js"; export const contextValue = slugify("x");\n',
    );
    writeFileSync(
      resolve(workspace.root, "src/app.ts"),
      `import { slugify as raw } from "./slug/index.jaunt.js";
import { slugify as hidden } from "./slug/__generated__/index.js";
export const result = [raw("x"), hidden("y")];
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
    const codes = new Set(
      session.analyzeWorkspace().diagnostics.map((item) => item.code),
    );
    expect(codes).toContain("JAUNT_TS_RUNTIME_SPEC_IMPORT");
    expect(codes).toContain("JAUNT_TS_CONTEXT_CYCLE");
    expect(codes).toContain("JAUNT_TS_GENERATED_PRIVATE_IMPORT");
  });

  test("allows a facade to own its generated artifacts and context to type-import its mirror", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(
      resolve(workspace.root, "src/slug/index.context.ts"),
      'import type { slugify } from "./__generated__/index.api.js";\nexport type Slugifier = typeof slugify;\n',
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/index.ts"),
      'export * from "./__generated__/index.js";\nexport type { slugify } from "./__generated__/index.api.js";\n',
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
        .diagnostics.filter(
          (item) => item.code === "JAUNT_TS_GENERATED_PRIVATE_IMPORT",
        ),
    ).toEqual([]);
  });
});
