import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { AnalyzerSession } from "../../src/worker/session.js";
import ts from "@typescript/typescript6";
import { loadProjectGraph } from "../../src/analyzer/config.js";
import { validateApiMirrorEquivalence } from "../../src/analyzer/overlay.js";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

async function sessionFor(source: string, context?: string) {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  writeFileSync(resolve(workspace.root, "src/slug/index.jaunt.ts"), source);
  writeFileSync(resolve(workspace.root, "src/app.ts"), "export {};\n");
  if (context !== undefined) {
    writeFileSync(
      resolve(workspace.root, "src/slug/index.context.ts"),
      context,
    );
  }
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
  return { workspace, session };
}

function validate(
  session: AnalyzerSession,
  moduleId: string,
  candidate: string,
) {
  const metadata = session.metadata();
  return session.validateOverlay({
    sessionId: metadata.sessionId,
    expectedEpoch: metadata.epoch,
    expectedSnapshot: metadata.snapshot,
    candidates: { [moduleId]: candidate },
  });
}

describe("concrete class semantics", () => {
  test("copies a preserved body and its paired-context import deterministically", async () => {
    const { session } = await sessionFor(
      `import * as jaunt from "@usejaunt/ts/spec";
import { normalize } from "./index.context.js";
jaunt.magicModule();
/** A formatter with one audited handwritten operation. */
export class Formatter {
  constructor() { jaunt.magic(); }
  /** Normalize one value. @jauntPreserve */
  format(value: string): string { return normalize(value); }
}
`,
      `/** Normalize a user-visible value. */
export function normalize(value: string): string { return value.trim().toLowerCase(); }
`,
    );
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.contextDocs).toEqual([
      expect.objectContaining({
        id: "workspace:src/slug/index.context.ts",
        exports: [expect.objectContaining({ symbol: "normalize" })],
      }),
    ]);
    const result = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Formatter {
  constructor() {}
  format(value: string): string { return "model-authored"; }
}`,
    );
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    const implementation = result.artifacts.find(
      (artifact) => artifact.kind === "implementation",
    )!.content;
    expect(implementation).toContain(
      'import { normalize } from "../index.context.js";',
    );
    expect(implementation).toContain("return normalize(value);");
    expect(implementation).not.toContain("model-authored");
    expect(implementation).toContain("Normalize one value. @jauntPreserve");
  });

  test("supports a generic local-base class with overloads, accessors, fields, and inherited members", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A named base. */
export class Base {
  constructor() { jaunt.magic(); }
  /** Return the base name. */
  name(): string { return jaunt.magic(); }
}
/** A typed child. */
export class Child<T extends string> extends Base {
  constructor(value: T) { jaunt.magic(); }
  convert(this: Child<T>, value: T): T;
  convert(this: Child<T>, value: string): string;
  convert(this: Child<T>, value: T | string): T | string { return jaunt.magic(); }
  get current(): T { return jaunt.magic(); }
  set current(value: T) { jaunt.magic(); }
  static readonly kind: "child";
  optional?: number;
}
`);
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.apiSource).toContain(
      "export declare class Child<T extends string> extends Base",
    );
    expect(contract.apiSource.match(/convert\(/g)).toHaveLength(2);
    const result = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Base {
  constructor() {}
  name(): string { return "base"; }
}
class __jaunt_impl_Child<T extends string> extends __jaunt_impl_Base {
  #current: T;
  static readonly kind: "child" = "child";
  optional?: number;
  constructor(value: T) { super(); this.#current = value; }
  convert(this: __jaunt_impl_Child<T>, value: T): T;
  convert(this: __jaunt_impl_Child<T>, value: string): string;
  convert(this: __jaunt_impl_Child<T>, value: T | string): T | string { return value; }
  get current(): T { return this.#current; }
  set current(value: T) { this.#current = value; }
}`,
    );
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
  });

  test("rejects wrong heritage, inherited overrides, and modifier drift", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Base contract. */
export class Base {
  constructor() { jaunt.magic(); }
  read(value: string): string { return jaunt.magic(); }
}
/** Child contract. */
export class Child extends Base {
  constructor() { jaunt.magic(); }
  readonly value: string;
  optional?: number;
}
`);
    const contract = session.analyzeContracts().modules[0]!;
    const wrongBase = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Base { constructor() {} read(value: string): string { return value; } }
class __jaunt_impl_Child { constructor() {} readonly value = "x"; optional?: number; }`,
    );
    expect(wrongBase.valid).toBe(false);
    expect(
      wrongBase.diagnostics.some(
        (item) => item.code === "JAUNT_TS_CANDIDATE_HERITAGE",
      ),
    ).toBe(true);

    const override = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Base { constructor() {} read(value: string): string { return value; } }
class __jaunt_impl_Child extends __jaunt_impl_Base {
  constructor() { super(); }
  read(value: "narrow"): string { return value; }
  value = "x";
  optional: number = 1;
}`,
    );
    expect(override.valid).toBe(false);
    const codes = new Set(override.diagnostics.map((item) => item.code));
    expect(codes).toContain("JAUNT_TS_EXTRA_PUBLIC_MEMBER");
    expect(codes).toContain("JAUNT_TS_MEMBER_MODIFIER");
  });

  test("strict adapters reject narrowed generic constructors, methods, and setters", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A generic value box. */
export class Box<T extends string> {
  constructor(value: T) { jaunt.magic(); }
  map<U extends T>(this: Box<T>, value: U): U { return jaunt.magic(); }
  get current(): T { return jaunt.magic(); }
  set current(value: T) { jaunt.magic(); }
}
`);
    const contract = session.analyzeContracts().modules[0]!;
    const result = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Box<T extends string> {
  constructor(_value: "only") {}
  map<U extends "only">(this: __jaunt_impl_Box<T> & { narrow: true }, value: U): U { return value; }
  get current(): T { throw new Error("unreachable"); }
  set current(_value: "only") {}
}`,
    );
    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
    expect(result.diagnostics.some((item) => item.code.startsWith("TS"))).toBe(
      true,
    );
  });

  test("injects an ordinary paired-context base and validates its inherited surface", async () => {
    const { session } = await sessionFor(
      `import * as jaunt from "@usejaunt/ts/spec";
import { ContextBase } from "./index.context.js";
jaunt.magicModule();
/** Concrete child of an audited context class. */
export class Child extends ContextBase {
  own(): string { return jaunt.magic(); }
}
`,
      `/** Stable context base. */
export class ContextBase {
  constructor(readonly value: string) {}
  inherited(): string { return this.value; }
}
`,
    );
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    const contract = session.analyzeContracts().modules[0]!;
    const result = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Child extends ContextBase {
  own(): string { return this.inherited(); }
}`,
    );
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(
      result.artifacts.find((artifact) => artifact.kind === "implementation")
        ?.content,
    ).toContain('import { ContextBase } from "../index.context.js";');
    expect(contract.placeholderSource).toContain(
      "ConstructorParameters<typeof ContextBase>",
    );
    const narrowed = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_Child extends ContextBase {
  constructor(value: number) { super(String(value)); }
  own(): string { return this.inherited(); }
}`,
    );
    expect(narrowed.valid).toBe(false);
  });

  test("treats an imported governed base as an implicit build dependency", async () => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    writeFileSync(resolve(workspace.root, "src/app.ts"), "export {};\n");
    mkdirSync(resolve(workspace.root, "src/base"), { recursive: true });
    writeFileSync(
      resolve(workspace.root, "src/base/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A generated base class. */
export class Base {
  constructor() { jaunt.magic(); }
  inherited(value: string): string { return jaunt.magic(); }
}
`,
    );
    writeFileSync(
      resolve(workspace.root, "src/slug/index.jaunt.ts"),
      `import * as jaunt from "@usejaunt/ts/spec";
import { Base } from "../base/index.jaunt.js";
jaunt.magicModule();
/** A generated child class. */
export class Child extends Base {
  own(value: string): string { return jaunt.magic(); }
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
      toolVersion: "0.1.0-alpha.0",
    });
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    const contracts = session.analyzeContracts().modules;
    const base = contracts.find(
      (module) => module.moduleId === "ts:src/base/index",
    )!;
    const child = contracts.find(
      (module) => module.moduleId === "ts:src/slug/index",
    )!;
    expect(child.dependencies).toContain("ts:src/base/index#Base");
    const metadata = session.metadata();
    const result = session.validateOverlay({
      sessionId: metadata.sessionId,
      expectedEpoch: metadata.epoch,
      expectedSnapshot: metadata.snapshot,
      candidates: {
        [base.moduleId]: `class __jaunt_impl_Base {
  constructor() {}
  inherited(value: string): string { return value; }
}`,
        [child.moduleId]: `class __jaunt_impl_Child extends Base {
  own(value: string): string { return this.inherited(value); }
}`,
      },
    });
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(
      result.artifacts.find(
        (artifact) =>
          artifact.kind === "implementation" &&
          artifact.moduleId === child.moduleId,
      )?.content,
    ).toContain('from "../../base/index.js"');
  });

  test("inherits a generic base constructor when the child omits one", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Generic base value. */
export class GenericBase<T> {
  constructor(value: T) { jaunt.magic(); }
  value(): T { return jaunt.magic(); }
}
/** String-specialized child with an inherited constructor. */
export class GenericChild<T extends string> extends GenericBase<T> {
  upper(): string { return jaunt.magic(); }
}
`);
    expect(session.analyzeWorkspace().diagnostics).toEqual([]);
    const contract = session.analyzeContracts().modules[0]!;
    const result = validate(
      session,
      contract.moduleId,
      `class __jaunt_impl_GenericBase<T> {
  #value: T;
  constructor(value: T) { this.#value = value; }
  value(): T { return this.#value; }
}
class __jaunt_impl_GenericChild<T extends string> extends __jaunt_impl_GenericBase<T> {
  upper(): string { return this.value().toUpperCase(); }
}`,
    );
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(contract.placeholderSource).toContain(
      "ConstructorParameters<typeof __jaunt_unbuilt_GenericBase<T>>",
    );
  });

  test("implicit unbuilt constructors and static fields throw instead of yielding values", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A class whose constructor is implicit. */
export class Unbuilt {
  static readonly label: string;
  value: string;
  read(): string { return jaunt.magic(); }
}
`);
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.placeholderSource).toContain(
      "constructor(..._args: unknown[])",
    );
    expect(contract.placeholderSource).toContain("static get label(): string");
    expect(contract.placeholderSource).toContain(
      "Jaunt implementation is unbuilt",
    );
  });

  test("rejects assertion laundering and unaudited dynamic package loading", async () => {
    const { session } =
      await sessionFor(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Preserve one identity value. */
export function identity(value: string): string { return jaunt.magic(); }
`);
    const contract = session.analyzeContracts().modules[0]!;
    for (const candidate of [
      `const bad = (value: number): number => value;
const __jaunt_impl_identity = bad as never;`,
      `const bad: unknown = (value: number): number => value;
const __jaunt_impl_identity = bad as (value: string) => string;`,
      `const __jaunt_impl_identity = (value: string): string => {
  void import("fast-check");
  return value;
};`,
      `const load = require;
const __jaunt_impl_identity = (value: string): string => {
  void load("fast-check");
  return value;
};`,
    ]) {
      const result = validate(session, contract.moduleId, candidate);
      expect(result.valid, candidate).toBe(false);
      expect(result.artifacts).toEqual([]);
    }
  });

  test("keeps only public overload signatures and independently proves documented type declarations", async () => {
    const source = `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Options shown in editor hovers. */
export interface Options<T extends string = string> { readonly value: T }
/** Parse either supported input without changing its type. */
export function parse(value: string): string;
export function parse(value: number): number;
export function parse(value: string | number): string | number { return jaunt.magic(); }
`;
    const { workspace, session } = await sessionFor(source);
    const contract = session.analyzeContracts().modules[0]!;
    expect(contract.apiSource).toContain("Options shown in editor hovers.");
    expect(contract.apiSource.match(/declare function parse/g)).toHaveLength(2);
    const valid = validate(
      session,
      contract.moduleId,
      `function __jaunt_impl_parse(value: string): string;
function __jaunt_impl_parse(value: number): number;
function __jaunt_impl_parse(value: string | number): string | number { return value; }`,
    );
    expect(valid.valid, JSON.stringify(valid.diagnostics)).toBe(true);

    const project = loadProjectGraph(ts, workspace.root, ["tsconfig.json"], [])
      .projects[0]!;
    const corrupted = contract.apiSource.replace(
      "readonly value: T",
      "readonly value: number",
    );
    expect(
      validateApiMirrorEquivalence(
        ts,
        workspace.root,
        project,
        contract,
        corrupted,
      ).some((item) => item.severity === "error"),
    ).toBe(true);

    const reformatted = source
      .replace("readonly value: T", "readonly   value : T")
      .replace("parse(value", "parse( value");
    const { session: cosmetic } = await sessionFor(reformatted);
    expect(cosmetic.analyzeContracts().modules[0]!.structuralDigest).toBe(
      contract.structuralDigest,
    );
  });
});
