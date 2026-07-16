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

async function freshnessModule(workspace: FixtureWorkspace) {
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
  return session.analyzeContracts().modules[0]!;
}

async function freshnessDigests(workspace: FixtureWorkspace): Promise<{
  structural: string;
  prose: string;
  api: string;
  environment: string;
}> {
  const contract = await freshnessModule(workspace);
  return {
    structural: contract.structuralDigest,
    prose: contract.proseDigest,
    api: contract.apiDigest,
    environment: contract.semanticEnvironmentDigest!,
  };
}

async function structuralDigest(workspace: FixtureWorkspace): Promise<string> {
  return (await freshnessDigests(workspace)).structural;
}

test("relative imported type closure changes structurally but ignores trivia", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { SlugOptions } from "../types.js";
jaunt.magicModule();
/** Make a slug. */
export function slugify(title: string, options: SlugOptions): string {
  return jaunt.magic();
}
`,
  );
  write(
    workspace.root,
    "src/types.ts",
    `import type { SlugPolicy } from "./policy.js";
export interface SlugOptions { policy: SlugPolicy; }
`,
  );
  write(
    workspace.root,
    "src/policy.ts",
    "export interface SlugPolicy { separator: string; }\n",
  );
  const original = await structuralDigest(workspace);

  write(
    workspace.root,
    "src/policy.ts",
    "export interface SlugPolicy { separator: string; lowercase: boolean; }\n",
  );
  const semanticEdit = await structuralDigest(workspace);
  expect(semanticEdit).not.toBe(original);

  write(
    workspace.root,
    "src/policy.ts",
    `// Formatting and comments are not contract structure.
export   interface SlugPolicy {
  separator : string ;

  /* keep output lowercase */ lowercase : boolean ;
}
`,
  );
  expect(await structuralDigest(workspace)).toBe(semanticEdit);
});

for (const compilerPackage of [
  "@typescript/typescript58",
  "@typescript/typescript6",
] as const) {
  test(`import aliases and import-type quotes are structurally neutral on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/types.ts",
      `export interface Payload { value: string; }
export interface AlternatePayload { value: string; }
export interface Box<T> { value: T; }
export default interface DefaultPayload { fallback: string; }
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type DefaultPayload from "../types.js";
import type { Payload as LocalPayload } from "../types.js";
import type * as Models from "../types.js";
jaunt.magicModule();
export type Wrapped = { value: LocalPayload };
export interface PayloadBox extends Models.Box<LocalPayload> {}
/** Read the payload. */
export function read(
  value: LocalPayload,
  boxed: Models.Box<LocalPayload>,
  fallback: DefaultPayload,
  direct: import("../types.js").Payload,
): string {
  return jaunt.magic();
}
`,
    );
    const original = await freshnessDigests(workspace);

    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from '@usejaunt/ts/spec';
import type RenamedDefault from '../types.js';
import type { Payload as RenamedPayload } from '../types.js';
import type * as Domain from '../types.js';
jaunt.magicModule();
export type Wrapped = { value: RenamedPayload };
export interface PayloadBox extends Domain.Box<RenamedPayload> {}
/** Read the payload. */
export function read(
  value: RenamedPayload,
  boxed: Domain.Box<RenamedPayload>,
  fallback: RenamedDefault,
  direct: import('../types.js').Payload,
): string {
  return jaunt.magic();
}
`,
    );
    const cosmetic = await freshnessDigests(workspace);
    expect(cosmetic).toEqual(original);

    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from '@usejaunt/ts/spec';
import type RenamedDefault from '../types.js';
import type { AlternatePayload as RenamedPayload } from '../types.js';
import type * as Domain from '../types.js';
jaunt.magicModule();
export type Wrapped = { value: RenamedPayload };
export interface PayloadBox extends Domain.Box<RenamedPayload> {}
/** Read the payload. */
export function read(
  value: RenamedPayload,
  boxed: Domain.Box<RenamedPayload>,
  fallback: RenamedDefault,
  direct: import('../types.js').Payload,
): string {
  return jaunt.magic();
}
`,
    );
    const semantic = await freshnessDigests(workspace);
    expect(semantic.structural).not.toBe(cosmetic.structural);
    expect(semantic.api).not.toBe(cosmetic.api);
    expect(semantic.prose).toBe(cosmetic.prose);
  });
}

test("imported explicit function bodies are not structural but signatures are", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { Normalizer } from "../normalize.js";
jaunt.magicModule();
/** Make a slug with the supplied normalizer. */
export function slugify(title: string, normalize: Normalizer): string {
  return jaunt.magic();
}
`,
  );
  write(
    workspace.root,
    "src/normalize.ts",
    `export function normalize(value: string): string {
  return value.trim();
}
export type Normalizer = typeof normalize;
`,
  );
  const original = await structuralDigest(workspace);

  write(
    workspace.root,
    "src/normalize.ts",
    `export function normalize(value: string): string {
  const compact = value.replace(/\\s+/g, " ");
  return compact.toLowerCase();
}
export type Normalizer = typeof normalize;
`,
  );
  expect(await structuralDigest(workspace)).toBe(original);

  write(
    workspace.root,
    "src/normalize.ts",
    `export function normalize(value: string | null): string {
  return value?.trim() ?? "";
}
export type Normalizer = typeof normalize;
`,
  );
  expect(await structuralDigest(workspace)).not.toBe(original);
});

test("imported public TSDoc is prose while explicit-return bodies are freshness-neutral", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import { normalize } from "./index.context.js";
jaunt.magicModule();
/** Make a slug with the shared normalizer. */
export function slugify(title: string): string {
  return jaunt.magic();
}
export type Normalizer = typeof normalize;
`,
  );
  write(
    workspace.root,
    "src/slug/index.context.ts",
    `/** Normalize a title before slug generation. */
export function normalize(value: string): string {
  return value.trim();
}
`,
  );
  const original = await freshnessDigests(workspace);

  write(
    workspace.root,
    "src/slug/index.context.ts",
    `/** Normalize and canonicalize a title before slug generation. */
export function normalize(value: string): string {
  return value.trim();
}
`,
  );
  const documentationEdit = await freshnessDigests(workspace);
  expect(documentationEdit.structural).toBe(original.structural);
  expect(documentationEdit.prose).not.toBe(original.prose);
  expect(documentationEdit.api).not.toBe(original.api);

  write(
    workspace.root,
    "src/slug/index.context.ts",
    `/** Normalize and canonicalize a title before slug generation. */
export function normalize(value: string): string {
  return value.replace(/\\s+/g, " ").trim().toLowerCase();
}
`,
  );
  expect(await freshnessDigests(workspace)).toEqual(documentationEdit);
});

test("resolved package declarations and lock state participate in structural freshness", async () => {
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
        dependencies: { "@fixture/contracts": "1.0.0" },
        devDependencies: { typescript: "6.0.2" },
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "package-lock.json",
    `${JSON.stringify(
      {
        lockfileVersion: 3,
        packages: { "node_modules/@fixture/contracts": { version: "1.0.0" } },
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "node_modules/@fixture/contracts/package.json",
    `${JSON.stringify({ name: "@fixture/contracts", version: "1.0.0", type: "module", types: "./index.d.ts" })}\n`,
  );
  write(
    workspace.root,
    "node_modules/@fixture/contracts/index.d.ts",
    "export interface SlugOptions { separator: string; }\n",
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { SlugOptions } from "@fixture/contracts";
jaunt.magicModule();
/** Make a slug. */
export function slugify(title: string, options: SlugOptions): string {
  return jaunt.magic();
}
`,
  );
  const original = await freshnessDigests(workspace);

  write(
    workspace.root,
    "node_modules/@fixture/contracts/index.d.ts",
    "export interface SlugOptions { separator: string; maxLength: number; }\n",
  );
  const declarationEdit = await freshnessDigests(workspace);
  expect(declarationEdit.structural).not.toBe(original.structural);
  expect(declarationEdit.environment).not.toBe(original.environment);

  write(
    workspace.root,
    "node_modules/@fixture/contracts/index.d.ts",
    "// comment\nexport interface SlugOptions { separator : string ; maxLength : number ; }\n",
  );
  expect(await freshnessDigests(workspace)).toEqual(declarationEdit);

  write(
    workspace.root,
    "package-lock.json",
    `${JSON.stringify(
      {
        lockfileVersion: 3,
        packages: { "node_modules/@fixture/contracts": { version: "1.0.1" } },
      },
      null,
      2,
    )}\n`,
  );
  const lockEdit = await freshnessDigests(workspace);
  expect(lockEdit.structural).not.toBe(declarationEdit.structural);
  expect(lockEdit.environment).toBe(declarationEdit.environment);
});

test("compatibility identity normalizes only Jaunt tool package metadata", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const manifest = (version: string) =>
    `${JSON.stringify({
      name: "fixture",
      private: true,
      type: "module",
      devDependencies: {
        "@usejaunt/ts": version,
        typescript: "6.0.2",
      },
    })}\n`;
  const lock = (version: string) =>
    `${JSON.stringify({
      lockfileVersion: 3,
      packages: {
        "": { devDependencies: { "@usejaunt/ts": version } },
        "node_modules/@usejaunt/ts": { version },
      },
    })}\n`;
  write(workspace.root, "package.json", manifest("0.1.0-alpha.1"));
  write(workspace.root, "package-lock.json", lock("0.1.0-alpha.1"));
  const before = await freshnessDigests(workspace);

  write(workspace.root, "package.json", manifest("0.1.0-alpha.2"));
  write(workspace.root, "package-lock.json", lock("0.1.0-alpha.2"));
  const after = await freshnessDigests(workspace);

  expect(after.structural).not.toBe(before.structural);
  expect(after.environment).toBe(before.environment);
});

test("packageManager is tooling provenance rather than semantic compatibility", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const manifest = (packageManager?: string) =>
    `${JSON.stringify({
      name: "fixture",
      private: true,
      type: "module",
      ...(packageManager ? { packageManager } : {}),
      devDependencies: {
        "@usejaunt/ts": "0.1.0",
        typescript: "6.0.2",
      },
    })}\n`;
  write(workspace.root, "package.json", manifest());
  const before = await freshnessModule(workspace);

  write(workspace.root, "package.json", manifest("pnpm@11.5.0"));
  const added = await freshnessModule(workspace);
  expect(added.structuralDigest).not.toBe(before.structuralDigest);
  expect(added.semanticEnvironmentDigest).toBe(
    before.semanticEnvironmentDigest,
  );
  expect(added.toolingProvenanceRecords).toEqual(
    expect.arrayContaining([
      expect.objectContaining({
        id: "tooling:packageManager:package.json",
      }),
    ]),
  );

  write(workspace.root, "package.json", manifest("npm@11.5.1"));
  const changed = await freshnessModule(workspace);
  expect(changed.structuralDigest).not.toBe(added.structuralDigest);
  expect(changed.semanticEnvironmentDigest).toBe(
    added.semanticEnvironmentDigest,
  );
  expect(changed.toolingProvenanceRecords).not.toEqual(
    added.toolingProvenanceRecords,
  );
});

test("packageManager normalization is limited to the root manifest field", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify({
      name: "fixture",
      private: true,
      type: "module",
      metadata: [{ packageManager: "semantic-nested-value" }],
    })}\n`,
  );
  const before = await freshnessModule(workspace);
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify({
      name: "fixture",
      private: true,
      type: "module",
      metadata: [{ packageManager: "changed-semantic-nested-value" }],
    })}\n`,
  );
  const after = await freshnessModule(workspace);
  expect(after.semanticEnvironmentDigest).not.toBe(
    before.semanticEnvironmentDigest,
  );
});

test.each([
  {
    name: "pnpm",
    path: "pnpm-lock.yaml",
    lock: (tool: string, other: string) => `lockfileVersion: '9.0'
importers:
  .:
    devDependencies:
      '@usejaunt/ts':
        specifier: ${tool}
        version: ${tool}
packages:
  '@usejaunt/ts@${tool}':
    resolution: {integrity: sha512-tool-${tool}}
  'left-pad@${other}':
    resolution: {integrity: sha512-left-${other}}
`,
  },
  {
    name: "Yarn",
    path: "yarn.lock",
    lock: (tool: string, other: string) => `"@usejaunt/ts@${tool}":
  version "${tool}"
  resolved "https://registry.npmjs.org/@usejaunt/ts/-/ts-${tool}.tgz"
  integrity sha512-tool-${tool}

"left-pad@${other}":
  version "${other}"
  integrity sha512-left-${other}
`,
  },
  {
    name: "Bun",
    path: "bun.lock",
    lock: (tool: string, other: string) => `{
  "lockfileVersion": 1,
  "workspaces": {
    "": { "devDependencies": { "@usejaunt/ts": "${tool}" } },
  },
  "packages": {
    "@usejaunt/ts": ["@usejaunt/ts@${tool}", "", {}, "sha512-tool-${tool}"],
    "left-pad": ["left-pad@${other}", "", {}, "sha512-left-${other}"],
  },
}
`,
  },
])(
  "$name lockfiles defer compatibility identity to the resolved declaration closure",
  async ({ path, lock }) => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const manifest = (version: string) =>
      `${JSON.stringify({
        name: "fixture",
        private: true,
        type: "module",
        devDependencies: {
          "@usejaunt/ts": version,
          typescript: "6.0.2",
        },
      })}\n`;
    write(workspace.root, "package.json", manifest("0.1.0-alpha.1"));
    write(workspace.root, path, lock("0.1.0-alpha.1", "1.0.0"));
    const before = await freshnessDigests(workspace);

    write(workspace.root, "package.json", manifest("0.1.0-alpha.2"));
    write(workspace.root, path, lock("0.1.0-alpha.2", "1.0.0"));
    const toolUpgrade = await freshnessDigests(workspace);
    expect(toolUpgrade.structural).not.toBe(before.structural);
    expect(toolUpgrade.environment).toBe(before.environment);

    write(workspace.root, path, lock("0.1.0-alpha.2", "1.1.0"));
    const dependencyUpgrade = await freshnessDigests(workspace);
    expect(dependencyUpgrade.structural).not.toBe(toolUpgrade.structural);
    expect(dependencyUpgrade.environment).toBe(toolUpgrade.environment);
  },
);

test("external package declarations stay in the worker snapshot but not writable preconditions", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const packageRoot = mkdtempSync(
    resolve(tmpdir(), "jaunt-ts-declaration-package-"),
  );
  roots.push(packageRoot);
  write(
    workspace.root,
    "package.json",
    `${JSON.stringify(
      {
        name: "fixture",
        private: true,
        type: "module",
        dependencies: { "@fixture/contracts": "1.0.0" },
        devDependencies: { typescript: "6.0.2" },
      },
      null,
      2,
    )}\n`,
  );
  write(
    packageRoot,
    "package.json",
    `${JSON.stringify(
      {
        name: "@fixture/contracts",
        version: "1.0.0",
        type: "module",
        types: "./index.d.ts",
      },
      null,
      2,
    )}\n`,
  );
  write(
    packageRoot,
    "index.d.ts",
    "export interface SlugOptions { separator: string; }\n",
  );
  mkdirSync(resolve(workspace.root, "node_modules/@fixture"), {
    recursive: true,
  });
  symlinkSync(
    packageRoot,
    resolve(workspace.root, "node_modules/@fixture/contracts"),
    "dir",
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { SlugOptions } from "@fixture/contracts";
jaunt.magicModule();
/** Make a slug. */
export function slugify(title: string, options: SlugOptions): string {
  return jaunt.magic();
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
  const metadata = session.metadata();
  expect(metadata.inputHashes).not.toHaveProperty(
    "node_modules/@fixture/contracts/index.d.ts",
  );
  expect(
    Object.keys(metadata.inputHashes).every((path) => !path.includes("..")),
  ).toBe(true);

  write(
    packageRoot,
    "index.d.ts",
    "export interface SlugOptions { separator: string; maxLength: number; }\n",
  );
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
});
