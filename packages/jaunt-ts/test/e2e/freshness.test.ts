import {
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test, vi } from "vitest";
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

function importedTypeContext(source: string): string {
  const begin =
    "// <jaunt:imported-type-context version=2 encoding=base64-json>";
  const end = "// </jaunt:imported-type-context>";
  const start = source.lastIndexOf(begin);
  const finish = source.indexOf(end, start);
  expect(start).toBeGreaterThanOrEqual(0);
  expect(finish).toBeGreaterThan(start);
  const block = source.slice(start, finish + end.length);
  const records = [
    ...block.matchAll(
      /^\/\/ jaunt:imported-type-record=(?<payload>[A-Za-z0-9+/]+={0,2})$/gmu,
    ),
  ];
  return records
    .map((match) => {
      const parsed = JSON.parse(
        Buffer.from(match.groups!.payload!, "base64").toString("utf8"),
      ) as { id: string; priority: string; source: string };
      return `${JSON.stringify({ id: parsed.id, priority: parsed.priority })}\n${parsed.source}`;
    })
    .join("\n");
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

  test(`path-aliased tsx types reach model context through a barrel on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "tsconfig.json",
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            exactOptionalPropertyTypes: true,
            types: [],
            baseUrl: ".",
            paths: { "@/*": ["src/*"] },
            jsx: "preserve",
          },
          include: ["src/**/*.ts", "src/**/*.tsx"],
          exclude: [
            "src/**/*.jaunt.ts",
            "src/**/*.jaunt-test.ts",
            "src/**/__generated__/**",
          ],
        },
        null,
        2,
      )}\n`,
    );
    write(
      workspace.root,
      "src/components/memory/entity-highlights.tsx",
      `export interface ActualMemoryEntityItem {
  id: string;
  canonical_name: string;
  one_liner: string;
  context: string;
  aliases: string[];
  entity_type: string;
  source_label: string;
  provenance: { source: string };
}
export interface EntityProvenance { source: string; captured_at: string; }
export const runtimeOnly = () => "do not expose implementation code";
export class HiddenRuntime {
  secret(): string { return runtimeOnly(); }
}
`,
    );
    write(
      workspace.root,
      "src/components/memory/index.ts",
      `import type { ActualMemoryEntityItem } from "./entity-highlights.js";
interface InnerMemoryEntityItem extends ActualMemoryEntityItem {}
export type { InnerMemoryEntityItem as MemoryEntityItem };
export { runtimeOnly } from "./entity-highlights.js";
`,
    );
    write(
      workspace.root,
      "src/slug/index.context.ts",
      'export const authoredContextSentinel = "preserved-before-types";\n',
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import { type MemoryEntityItem as LocalEntity } from "@/components/memory/index.js";
jaunt.magicModule();
/** Read one entity. */
export function entityLabel(
  item: LocalEntity,
  provenance: import("@/components/memory/entity-highlights.js").EntityProvenance,
): string {
  return jaunt.magic();
}
`,
    );

    const contract = await freshnessModule(workspace);
    const contextSource = contract.contextSource ?? "";
    expect(contextSource).toMatch(
      /^export const authoredContextSentinel = "preserved-before-types";/u,
    );
    expect(contextSource).toContain(
      "// <jaunt:imported-type-context version=2 encoding=base64-json>",
    );
    expect(contextSource).toContain("// jaunt:imported-type-record=");
    expect(contextSource).not.toContain("interface ActualMemoryEntityItem");
    const source = importedTypeContext(contextSource);
    expect(source).toContain(
      '"id":"workspace:src/components/memory/entity-highlights.tsx"',
    );
    expect(source).toContain("interface InnerMemoryEntityItem");
    expect(source).toContain("interface ActualMemoryEntityItem");
    expect(source).toContain("interface EntityProvenance");
    for (const field of [
      "id",
      "canonical_name",
      "one_liner",
      "context",
      "aliases",
      "entity_type",
      "source_label",
      "provenance",
    ]) {
      expect(source).toContain(`${field}:`);
    }
    expect(source).not.toContain("runtimeOnly");
    expect(source).not.toContain("HiddenRuntime");
    expect(source).not.toContain("return");
  });

  test(`ordinary imports seed only declaration-surface model context on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "tsconfig.json",
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            exactOptionalPropertyTypes: true,
            verbatimModuleSyntax: false,
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
      )}\n`,
    );
    write(
      workspace.root,
      "src/models.ts",
      `export interface Entity {
  id: string;
  label: string;
}
export interface ShadowedReturn {
  returnMarker: string;
}
export const ComputedEntityKey: unique symbol = Symbol("computed-entity-key");
export interface ComputedEntityKey {
  leakedTypeNamespaceMarker: string;
}
export const UnusedComputedKey: unique symbol = Symbol("unused-computed-key");
export class ImportedBase {
  baseMarker = "base";
  runtimeBaseMethod(): string {
    return "base-runtime-implementation";
  }
}
export class RuntimeOnly {
  secret = "must-not-leak";
}
export function runtimeOnly(): string {
  return "runtime-implementation";
}
`,
    );
    write(
      workspace.root,
      "src/namespace-models.ts",
      `export interface NamespaceEntity {
  namespaceMarker: string;
}
export interface NestedNamespaceEntity {
  nestedNamespaceMarker: string;
}
export class NamespaceRuntimeOnly {
  secret = "namespace-runtime-class";
}
export function namespaceRuntimeOnly(): string {
  return "namespace-runtime-function";
}
`,
    );
    write(
      workspace.root,
      "src/shadowed-value.ts",
      `export const ShadowedValue = { leakedValueParameterMarker: true };
export function shadowedValue(ShadowedValue: string): typeof ShadowedValue {
  return ShadowedValue;
}
export interface InferredLocal { leakedInferMarker: string; }
export type ShadowedInfer<T> = T extends infer InferredLocal ? InferredLocal : never;
export interface ShadowedValueEnvelope {
  read: typeof shadowedValue;
  inferred: ShadowedInfer<string>;
}
`,
    );
    write(
      workspace.root,
      "src/namespace-wrapper.ts",
      `import type * as Models from "./namespace-models.js";
export interface NamespaceEnvelope {
  entity: Models.NestedNamespaceEntity;
}
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import { ComputedEntityKey, Entity, ImportedBase, RuntimeOnly, ShadowedReturn, runtimeOnly } from "../models.js";
import * as Models from "../namespace-models.js";
import { NamespaceEnvelope } from "../namespace-wrapper.js";
import { ShadowedValueEnvelope } from "../shadowed-value.js";
const runtimeInstance = new RuntimeOnly();
const runtimeResult = runtimeOnly();
void runtimeInstance;
void runtimeResult;
type InferredLocal<T> = T extends infer RuntimeOnly ? RuntimeOnly : never;
const inferredLocal: InferredLocal<string> = "local";
void inferredLocal;
jaunt.magicModule();
function localShadow(RuntimeOnly: { local: string }): string {
  type LocalRuntimeOnly = typeof RuntimeOnly;
  const value: LocalRuntimeOnly = RuntimeOnly;
  return value.local;
}
void localShadow({ local: "local" });
function valueNamespaceShadow(ShadowedReturn: string): ShadowedReturn {
  void ShadowedReturn;
  return { returnMarker: "type" };
}
void valueNamespaceShadow("value");
/** Read an entity imported through TypeScript's type elision. */
export function entityLabel(entity: Entity): string {
  return jaunt.magic();
}
/** Read one member of an otherwise runtime-heavy namespace import. */
export function namespaceLabel(entity: Models.NamespaceEntity): string {
  return jaunt.magic();
}
/** Read a namespace member reached through another declaration. */
export function nestedNamespaceLabel(entity: NamespaceEnvelope): string {
  return jaunt.magic();
}
/** Read a transitive declaration whose value parameter shadows an exported value. */
export function shadowedValueLabel(entity: ShadowedValueEnvelope): string {
  return jaunt.magic();
}
/** Extend an imported concrete base using its declaration surface. */
export class DerivedEntity extends ImportedBase {
  constructor() { jaunt.magic(); }
}
/** Read a computed type key while its spelling is shadowed only in the type namespace. */
export function computedEntityValue<ComputedEntityKey>(
  entity: { [ComputedEntityKey]: string; payload: ComputedEntityKey },
): string {
  return jaunt.magic();
}
/** Echo a generic whose name shadows, but does not reference, the runtime import. */
export function echo<RuntimeOnly>(value: RuntimeOnly): RuntimeOnly {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain("interface Entity");
    expect(source).toContain("id: string");
    expect(source).toContain("label: string");
    expect(source).toContain("interface ShadowedReturn");
    expect(source).toContain("returnMarker: string");
    expect(source).toContain("declare class ImportedBase");
    expect(source).toContain("baseMarker: string");
    expect(source).toContain("runtimeBaseMethod(): string");
    expect(source).toContain("declare const ComputedEntityKey: unique symbol");
    expect(source).not.toContain("interface ComputedEntityKey");
    expect(source).not.toContain("leakedTypeNamespaceMarker");
    expect(source).toContain("interface NamespaceEntity");
    expect(source).toContain("namespaceMarker: string");
    expect(source).toContain("interface NamespaceEnvelope");
    expect(source).toContain("interface NestedNamespaceEntity");
    expect(source).toContain("nestedNamespaceMarker: string");
    expect(source).toContain("interface ShadowedValueEnvelope");
    expect(source).toContain("declare function shadowedValue(");
    expect(source).toContain("type ShadowedInfer");
    expect(source).not.toContain("declare const ShadowedValue");
    expect(source).not.toContain("leakedValueParameterMarker");
    expect(source).not.toContain("interface InferredLocal");
    expect(source).not.toContain("leakedInferMarker");
    expect(source).not.toContain("RuntimeOnly");
    expect(source).not.toContain("runtimeOnly");
    expect(source).not.toContain("must-not-leak");
    expect(source).not.toContain("runtime-implementation");
    expect(source).not.toContain("base-runtime-implementation");
    expect(source).not.toContain("UnusedComputedKey");
    expect(source).not.toContain("NamespaceRuntimeOnly");
    expect(source).not.toContain("namespaceRuntimeOnly");
    expect(source).not.toContain("namespace-runtime-class");
    expect(source).not.toContain("namespace-runtime-function");
  });

  test(`verbatim module syntax retains only declaration-needed value imports on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "tsconfig.json",
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            exactOptionalPropertyTypes: true,
            verbatimModuleSyntax: true,
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
      )}\n`,
    );
    write(
      workspace.root,
      "src/models.ts",
      `export class ImportedBase {
  baseMarker = "base";
  baseMethod(): string {
    return "base-runtime-implementation";
  }
}
export class RuntimeOnly {
  secret = "runtime-only-marker";
}
export function runtimeOnly(): string {
  return "runtime-function-marker";
}
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import { ImportedBase, RuntimeOnly, runtimeOnly } from "../models.js";
const runtimeInstance = new RuntimeOnly();
const runtimeResult = runtimeOnly();
void runtimeInstance;
void runtimeResult;
jaunt.magicModule();
/** Extend the concrete base while preserving its declaration surface. */
export class DerivedEntity extends ImportedBase {
  constructor() { jaunt.magic(); }
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain("declare class ImportedBase");
    expect(source).toContain("baseMarker: string");
    expect(source).toContain("baseMethod(): string");
    expect(source).not.toContain("RuntimeOnly");
    expect(source).not.toContain("runtimeOnly");
    expect(source).not.toContain("runtime-only-marker");
    expect(source).not.toContain("runtime-function-marker");
    expect(source).not.toContain("base-runtime-implementation");
  });

  test(`class type context is declaration-only on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/entity-types.ts",
      `function sealed(value: Function): void { void value; }
function runtimeNumber(): number { return Date.now(); }
@sealed
export class EntityFactory {
  #secret = "hidden";
  private token = "private";
  public label = "entity";
  constructor(public readonly prefix = "entity") {}
  format(value = "item") { return this.prefix + value; }
  get size() { return runtimeNumber(); }
}
export enum EntityMode {
  Stable = "stable",
  Dynamic = runtimeNumber(),
}
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { EntityFactory, EntityMode } from "../entity-types.js";
jaunt.magicModule();
/** Read one entity factory. */
export function readFactory(factory: EntityFactory, mode: EntityMode): string {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain("export declare class EntityFactory");
    expect(source).toContain("label: string");
    expect(source).toContain("format(value?: string): string");
    expect(source).toContain("get size(): number");
    expect(source).toContain('Stable = "stable"');
    expect(source).not.toContain("@sealed");
    expect(source).not.toContain("#secret");
    expect(source).not.toContain("private token");
    expect(source).not.toContain("runtimeNumber");
    expect(source).not.toContain("return");
  });

  test(`nested conditional infer scope preserves an outer imported type on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/imported.ts",
      `export interface ImportedResult {
  genuineImportMarker: string;
}
`,
    );
    write(
      workspace.root,
      "src/envelope.ts",
      `import type { ImportedResult } from "./imported.js";
export type NestedConditional<T> = T extends (
  T extends infer ImportedResult ? ImportedResult : never
) ? ImportedResult : never;
export interface Envelope {
  value: NestedConditional<string>;
}
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { Envelope } from "../envelope.js";
jaunt.magicModule();
/** Read the outer conditional result. */
export function readResult(value: Envelope): string {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain("type NestedConditional");
    expect(source).toContain("interface ImportedResult");
    expect(source).toContain("genuineImportMarker: string");
  });

  test(`nested conditional branches retain enclosing infer ownership on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/imported.ts",
      `export interface CheckOwned {
  leakedCheckBinderMarker: string;
}
export interface TrueOwned {
  leakedTrueBinderMarker: string;
}
export interface FalseOwned {
  leakedFalseBinderMarker: string;
}
export interface NestedExtendsOwned {
  genuineNestedExtendsImportMarker: string;
}
`,
    );
    write(
      workspace.root,
      "src/envelope.ts",
      `import type {
  CheckOwned,
  FalseOwned,
  NestedExtendsOwned,
  TrueOwned,
} from "./imported.js";
export type NestedOwnership<T> = T extends (
  (infer CheckOwned) extends infer NestedExtendsOwned
    ? infer TrueOwned
    : infer FalseOwned
) ? [CheckOwned, TrueOwned, FalseOwned, NestedExtendsOwned] : never;
export interface Envelope {
  value: NestedOwnership<string>;
}
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { Envelope } from "../envelope.js";
jaunt.magicModule();
/** Read the nested ownership result. */
export function readResult(value: Envelope): string {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain("type NestedOwnership");
    expect(source).toContain("interface NestedExtendsOwned");
    expect(source).toContain("genuineNestedExtendsImportMarker: string");
    expect(source).not.toContain("leakedCheckBinderMarker");
    expect(source).not.toContain("leakedTrueBinderMarker");
    expect(source).not.toContain("leakedFalseBinderMarker");
  });

  test(`requested model types close over declaration-safe local dependencies on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/contracts/support.ts",
      `export class RequiredFields {
  requiredId: string = crypto.randomUUID();
  requiredVersion = 1;
}
export function normalizeKey(value: string): string {
  return value.trim().toLowerCase();
}
export const defaults = { retries: 3, mode: "strict" as const };
export const runtimeOnly = () => Date.now();
`,
    );
    write(
      workspace.root,
      "src/contracts/request.ts",
      `import {
  RequiredFields as WorkspaceBase,
  defaults as workspaceDefaults,
  normalizeKey as workspaceNormalizer,
} from "./support.js";
interface LeftNode { right?: RightNode; }
interface RightNode { left?: LeftNode; }
type Normalizer = typeof workspaceNormalizer;
type Defaults = typeof workspaceDefaults;
export interface RequestContract extends WorkspaceBase {
  graph: LeftNode;
  normalize: Normalizer;
  defaults: Defaults;
}
`,
    );
    write(
      workspace.root,
      "src/contracts/index.ts",
      `export type { RequestContract as PublicRequest } from "./request.js";
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { PublicRequest as LocalRequest } from "../contracts/index.js";
jaunt.magicModule();
/** Read a request contract. */
export function readRequest(request: LocalRequest): string {
  return jaunt.magic();
}
`,
    );

    const first = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    const second = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(second).toBe(first);
    expect(first).toContain("interface RequestContract extends WorkspaceBase");
    expect(first).toContain("declare class RequiredFields");
    expect(first).toContain("requiredId: string");
    expect(first).toContain("requiredVersion: number");
    expect(first).toContain("interface LeftNode");
    expect(first).toContain("interface RightNode");
    expect(first).toContain(
      "declare function normalizeKey(value: string): string",
    );
    expect(first).toContain("type Normalizer = typeof workspaceNormalizer");
    expect(first).toContain("declare const defaults:");
    expect(first).toContain("type Defaults = typeof workspaceDefaults");
    expect(first).not.toContain("crypto.randomUUID");
    expect(first).not.toContain("trim().toLowerCase");
    expect(first).not.toContain("Date.now");
    expect(first).not.toContain("runtimeOnly");
  });

  test(`namespace, default, and import-equals aliases reach model context on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/contracts/legacy.cts",
      `export class Base {
  inheritedRequired: string = "required";
  read(): string { return this.inheritedRequired; }
}
`,
    );
    write(
      workspace.root,
      "src/contracts/context.cts",
      `import Legacy = require("./legacy.cjs");
import ExportedBase = require("./legacy-barrel.cjs");
namespace Domain {
  export interface Identifier { value: string; helper: NamespaceHelper; }
  export namespace Nested {
    export interface NestedIdentifier { nestedValue: string; }
    export function runtimeNestedNamespaceValue(): number { return Date.now(); }
  }
  export function runtimeNamespaceValue(): number { return Date.now(); }
}
namespace Domain {
  export interface NamespaceHelper { helperValue: string; }
  export function runtimeMergedNamespaceValue(): number { return Date.now(); }
}
class DefaultContract {
  requiredFallback: string = "fallback";
}
export interface Composite extends Legacy.Base {
  id: Domain.Identifier;
  nested: Domain.Nested.NestedIdentifier;
  fallback: DefaultContract;
  exported: ExportedBase;
}
export default DefaultContract;
`,
    );
    write(
      workspace.root,
      "src/contracts/legacy-default.cts",
      `class ExportedBase {
  exportRequired: string = "exported";
}
export = ExportedBase;
`,
    );
    write(
      workspace.root,
      "src/contracts/legacy-barrel.cts",
      `import ExportedBase = require("./legacy-default.cjs");
export = ExportedBase;
`,
    );
    write(
      workspace.root,
      "src/contracts/hidden-namespace-leak.cts",
      `export interface HiddenNamespaceLeak { hiddenLeak: string; }
`,
    );
    write(
      workspace.root,
      "src/contracts/legacy-namespace.cts",
      `import type { HiddenNamespaceLeak } from "./hidden-namespace-leak.cjs";
namespace LegacyModels {
  export namespace Domain {
    export namespace Nested {
      export interface Entity { nestedEntityValue: string; }
      export class RuntimeNestedClass { secret = "runtime-nested-class"; }
      export function runtimeNestedValue(): HiddenNamespaceLeak { return Date.now() as never; }
    }
    export interface UnusedDomainSibling { unused: string; }
  }
  export function runtimeTopLevelValue(): number { return Date.now(); }
}
export = LegacyModels;
`,
    );
    write(
      workspace.root,
      "src/contracts/legacy-namespace-property-barrel.cts",
      `import Root = require("./legacy-namespace.cjs");
export = Root.Domain;
`,
    );
    write(
      workspace.root,
      "src/contracts/legacy-namespace-barrel.cts",
      `import Domain = require("./legacy-namespace-property-barrel.cjs");
import Public = Domain.Nested;
export = Public;
`,
    );
    write(
      workspace.root,
      "src/contracts/default-source.ts",
      `export default class ForwardedContract {
  forwardedRequired: string = "forwarded";
}
`,
    );
    write(
      workspace.root,
      "src/contracts/default-barrel.ts",
      `import type ForwardedContract from "./default-source.js";
export default ForwardedContract;
`,
    );
    write(
      workspace.root,
      "src/contracts/default-function-source.ts",
      `export default function forwardedFactory(value = "factory"): string {
  return value;
}
`,
    );
    write(
      workspace.root,
      "src/contracts/direct-only.cts",
      `class DirectOnlyContract {
  directOnlyRequired: string = "direct";
}
export = DirectOnlyContract;
`,
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type DefaultContract, { Composite } from "../contracts/context.cjs";
import type ForwardedContract from "../contracts/default-barrel.js";
import type forwardedFactory from "../contracts/default-function-source.js";
import DirectLegacy = require("../contracts/direct-only.cjs");
import LegacyModels = require("../contracts/legacy-namespace-barrel.cjs");
jaunt.magicModule();
/** Read legacy and default contracts. */
export function readComposite(
  value: Composite,
  fallback: DefaultContract,
  forwarded: ForwardedContract,
  factory: typeof forwardedFactory,
  direct: DirectLegacy,
  nested: LegacyModels.Entity,
): string {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain('import Legacy = require("./legacy.cjs")');
    expect(source).toContain("namespace Domain");
    expect(source).toContain("interface Identifier");
    expect(source).toContain("value: string");
    expect(source).toContain("interface NamespaceHelper");
    expect(source).toContain("helperValue: string");
    expect(source).toContain("namespace Nested");
    expect(source).toContain("interface NestedIdentifier");
    expect(source).toContain("nestedValue: string");
    expect(source).toContain("namespace LegacyModels");
    expect(source).toContain("interface Entity");
    expect(source).toContain("nestedEntityValue: string");
    expect(source).toContain("declare class DefaultContract");
    expect(source).toContain("requiredFallback: string");
    expect(source).toContain("declare class Base");
    expect(source).toContain("inheritedRequired: string");
    expect(source).toContain("declare class ExportedBase");
    expect(source).toContain("exportRequired: string");
    expect(source).toContain("forwardedRequired: string");
    expect(source).toContain("export default class ForwardedContract");
    expect(source).toContain("export default function forwardedFactory");
    expect(source).not.toContain("export default declare");
    expect(source).toContain("declare class DirectOnlyContract");
    expect(source).toContain("directOnlyRequired: string");
    expect(source).not.toContain("Date.now");
    expect(source).not.toContain("runtimeNamespaceValue");
    expect(source).not.toContain("runtimeMergedNamespaceValue");
    expect(source).not.toContain("runtimeNestedNamespaceValue");
    expect(source).not.toContain("RuntimeNestedClass");
    expect(source).not.toContain("runtimeNestedValue");
    expect(source).not.toContain("UnusedDomainSibling");
    expect(source).not.toContain("runtimeTopLevelValue");
    expect(source).not.toContain("runtime-nested-class");
    expect(source).not.toContain("HiddenNamespaceLeak");
    expect(source).not.toContain("hiddenLeak");
    expect(source).not.toContain("return this.inheritedRequired");
    expect(source).not.toContain('= "fallback"');
  });

  test(`symlinked workspace package types exclude third-party declarations on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "packages/contracts/package.json",
      `${JSON.stringify({
        name: "@workspace/contracts",
        private: true,
        type: "module",
        types: "./src/index.ts",
      })}\n`,
    );
    write(
      workspace.root,
      "packages/contracts/src/index.ts",
      "export interface WorkspaceEntity { id: string; label: string; }\n",
    );
    mkdirSync(resolve(workspace.root, "node_modules/@workspace"), {
      recursive: true,
    });
    symlinkSync(
      resolve(workspace.root, "packages/contracts"),
      resolve(workspace.root, "node_modules/@workspace/contracts"),
      "dir",
    );
    write(
      workspace.root,
      "node_modules/@third/types/package.json",
      `${JSON.stringify({
        name: "@third/types",
        type: "module",
        types: "./index.d.ts",
      })}\n`,
    );
    write(
      workspace.root,
      "node_modules/@third/types/index.d.ts",
      "export interface ThirdPartyEntity { opaque: string; }\n",
    );
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { WorkspaceEntity } from "@workspace/contracts";
import type { ThirdPartyEntity } from "@third/types";
jaunt.magicModule();
/** Read workspace and package entities. */
export function readEntity(local: WorkspaceEntity, third: ThirdPartyEntity): string {
  return jaunt.magic();
}
`,
    );

    const source = importedTypeContext(
      (await freshnessModule(workspace)).contextSource ?? "",
    );
    expect(source).toContain(
      '"id":"workspace:packages/contracts/src/index.ts"',
    );
    expect(source).toContain("interface WorkspaceEntity");
    expect(source).not.toContain("ThirdPartyEntity");
    expect(source).not.toContain("node_modules/@third/types");
  });
}

test("type context budgets UTF-8 bytes after globally prioritizing requested declarations", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const supportingLiteral = "s".repeat(60_000);
  const requestedLiteral = "d".repeat(8_000);
  write(
    workspace.root,
    "src/0-support.ts",
    `export type HugeSupportingLiteral = "${supportingLiteral}";
`,
  );
  write(
    workspace.root,
    "src/a-types.ts",
    `import type { HugeSupportingLiteral } from "./0-support.js";
export interface DirectA { a: string; huge?: HugeSupportingLiteral; }
`,
  );
  write(
    workspace.root,
    "src/z-types.ts",
    `export type DirectZ = "${requestedLiteral}";
`,
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { DirectA } from "../a-types.js";
import type { DirectZ } from "../z-types.js";
jaunt.magicModule();
/** Read both direct types. */
export function readBoth(a: DirectA, z: DirectZ): string { return jaunt.magic(); }
`,
  );

  const source = importedTypeContext(
    (await freshnessModule(workspace)).contextSource ?? "",
  );
  expect(source).toContain("interface DirectA");
  expect(source).toContain("type DirectZ");
  expect(source).toContain(requestedLiteral);
  expect(source).not.toContain(supportingLiteral);
  expect(source).toContain("Jaunt omitted 1 type-context chunks");
  expect(Buffer.byteLength(source, "utf8")).toBeLessThanOrEqual(64 * 1024);
});

test("imported type records use Unicode code-unit ordering", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/z-types.ts",
    "/** The code-unit-first type. */\nexport interface ZType { readonly z: string; }\n",
  );
  write(
    workspace.root,
    "src/ä-types.ts",
    "/** The locale-sensitive type. */\nexport interface UmlautType { readonly umlaut: string; }\n",
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { ZType } from "../z-types.js";
import type { UmlautType } from "../ä-types.js";
jaunt.magicModule();
/** Read types whose paths expose locale-sensitive collation. */
export function readTypes(z: ZType, umlaut: UmlautType): string {
  return jaunt.magic();
}
`,
  );

  const localeCompare = vi
    .spyOn(String.prototype, "localeCompare")
    .mockImplementation(function (this: string, other: string): number {
      const left = String(this);
      const right = String(other);
      return left < right ? 1 : left > right ? -1 : 0;
    });
  const contract = await (async () => {
    try {
      return await freshnessModule(workspace);
    } finally {
      localeCompare.mockRestore();
    }
  })();
  const source = importedTypeContext(contract.contextSource ?? "");
  const zIndex = source.indexOf('"id":"workspace:src/z-types.ts"');
  const umlautIndex = source.indexOf('"id":"workspace:src/ä-types.ts"');
  expect(zIndex).toBeGreaterThanOrEqual(0);
  expect(umlautIndex).toBeGreaterThanOrEqual(0);
  expect(zIndex).toBeLessThan(umlautIndex);

  const environmentIds =
    contract.semanticEnvironmentRecords?.map((record) => record.id) ?? [];
  const zEnvironmentIndex = environmentIds.indexOf("workspace:src/z-types.ts");
  const umlautEnvironmentIndex = environmentIds.indexOf(
    "workspace:src/ä-types.ts",
  );
  expect(zEnvironmentIndex).toBeGreaterThanOrEqual(0);
  expect(umlautEnvironmentIndex).toBeGreaterThanOrEqual(0);
  expect(zEnvironmentIndex).toBeLessThan(umlautEnvironmentIndex);
  const proseIds = contract.contextDocs.map((record) => record.id);
  const zProseIndex = proseIds.indexOf("workspace:src/z-types.ts");
  const umlautProseIndex = proseIds.indexOf("workspace:src/ä-types.ts");
  expect(zProseIndex).toBeGreaterThanOrEqual(0);
  expect(umlautProseIndex).toBeGreaterThanOrEqual(0);
  expect(zProseIndex).toBeLessThan(umlautProseIndex);
});

test("type-context truncation uses Unicode code-unit path ordering", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const zLiteral = "z".repeat(40_000);
  const umlautLiteral = "u".repeat(40_000);
  write(
    workspace.root,
    "src/z-types.ts",
    `export type ZType = "${zLiteral}";\n`,
  );
  write(
    workspace.root,
    "src/ä-types.ts",
    `export type UmlautType = "${umlautLiteral}";\n`,
  );
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { ZType } from "../z-types.js";
import type { UmlautType } from "../ä-types.js";
jaunt.magicModule();
/** Read two direct types that cannot both fit in model context. */
export function readTypes(z: ZType, umlaut: UmlautType): string {
  return jaunt.magic();
}
`,
  );

  const source = importedTypeContext(
    (await freshnessModule(workspace)).contextSource ?? "",
  );
  expect(source).toContain(zLiteral);
  expect(source).not.toContain(umlautLiteral);
  expect(source).toContain("Jaunt omitted 1 type-context chunks");
});

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

for (const compilerPackage of [
  "@typescript/typescript58",
  "@typescript/typescript6",
] as const) {
  test(`tagged-template text is type-neutral while substitutions and plain templates are not on ${compilerPackage}`, async () => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    write(
      workspace.root,
      "src/slug/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import type { Brief } from "../types.js";
jaunt.magicModule();
/** Read a brief title. */
export function titleOf(brief: Brief): string {
  return jaunt.magic();
}
`,
    );
    write(
      workspace.root,
      "src/types.ts",
      `import "./queries.js";
export interface Brief { id: string; title: string; }
`,
    );
    write(
      workspace.root,
      "src/queries.ts",
      `declare function gql(strings: TemplateStringsArray, ...values: unknown[]): string;
export type Label = \`brief-\${string}\`;
export const LABEL = \`brief \${1}\`;
export const LIST = gql\`query { briefs { id } }\`;
export const NESTED = gql\`query { \${\`brief \${1}\`} }\`;
export const QUERY = gql\`query { brief(id: \${1}) { id } }\`;
export const FIELD = \`id\`;
`,
    );
    const original = await freshnessDigests(workspace);

    write(
      workspace.root,
      "src/queries.ts",
      `declare function gql(strings: TemplateStringsArray, ...values: unknown[]): string;
export type Label = \`brief-\${string}\`;
export const LABEL = \`brief \${1}\`;
export const LIST = gql\`query { briefs { id title } }\`;
export const NESTED = gql\`query { changed \${\`brief \${1}\`} }\`;
export const QUERY = gql\`query { brief(id: \${1}) { id title } }\`;
export const FIELD = \`id\`;
`,
    );
    const textEdit = await freshnessDigests(workspace);
    expect(textEdit).toEqual(original);

    write(
      workspace.root,
      "src/queries.ts",
      `declare function gql(strings: TemplateStringsArray, ...values: unknown[]): string;
export type Label = \`brief-\${string}\`;
export const LABEL = \`brief \${1}\`;
export const LIST = gql\`query { briefs { id title } }\`;
export const NESTED = gql\`query { changed \${\`brief \${1}\`} }\`;
export const QUERY = gql\`query { brief(id: \${"1"}) { id title } }\`;
export const FIELD = \`id\`;
`,
    );
    const substitutionEdit = await freshnessDigests(workspace);
    expect(substitutionEdit.structural).not.toBe(textEdit.structural);

    write(
      workspace.root,
      "src/queries.ts",
      `declare function gql(strings: TemplateStringsArray, ...values: unknown[]): string;
export type Label = \`brief-\${string}\`;
export const LABEL = \`brief \${1}\`;
export const LIST = gql\`query { briefs { id title } }\`;
export const NESTED = gql\`query { changed \${\`brief \${1}\`} }\`;
export const QUERY = gql\`query { brief(id: \${"1"}) { id title } }\`;
export const FIELD = \`title\`;
`,
    );
    expect((await freshnessDigests(workspace)).structural).not.toBe(
      substitutionEdit.structural,
    );
  });
}

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
