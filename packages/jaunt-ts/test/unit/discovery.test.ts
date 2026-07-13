import { rmSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import compiler from "@typescript/typescript6";
import { afterEach, expect, test } from "vitest";
import { loadProjectGraph } from "../../src/analyzer/config.js";
import { discoverSpecModule } from "../../src/analyzer/discovery.js";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

function analyze(source: string) {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const specPath = resolve(workspace.root, "src/slug/index.jaunt.ts");
  writeFileSync(specPath, source);
  const graph = loadProjectGraph(
    compiler,
    workspace.root,
    ["tsconfig.json"],
    [],
  );
  return discoverSpecModule(
    compiler,
    workspace.root,
    specPath,
    "__generated__",
    graph.projects,
  );
}

test("discovery rejects ambiguous or underspecified public boundaries", () => {
  const result = analyze(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
export interface Unsafe { value: any }
interface Hidden { value: string }
function helper(value: string): string { return value; }
/** A deliberately invalid boundary. */
export default function broken({ value }) { return jaunt.magic(); }
`);

  const codes = new Set(result.diagnostics.map((item) => item.code));
  expect(codes).toContain("JAUNT_TS_ANY_BOUNDARY");
  expect(codes).toContain("JAUNT_TS_TYPE_EXPORT_REQUIRED");
  expect(codes).toContain("JAUNT_TS_RUNTIME_DECLARATION");
  expect(codes).toContain("JAUNT_TS_DEFAULT_EXPORT");
  expect(codes).toContain("JAUNT_TS_BINDING_PATTERN");
  expect(codes).toContain("JAUNT_TS_EXPLICIT_TYPE_REQUIRED");
});

test("discovery reports syntax errors in private inputs excluded from emit", () => {
  const result = analyze(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Broken syntax. */
export function broken(value: string): string { return jaunt.magic(;
`);

  expect(result.diagnostics.some((item) => /^TS\d+$/.test(item.code))).toBe(
    true,
  );
});

test("a declaration-only design target is analyzable but not yet buildable", () => {
  const result = analyze(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/**
 * Design a stable slug API.
 * @jauntDesign
 */
export declare function slugify(value: string): string;
`);

  expect(result.symbols.map((item) => item.name)).toEqual(["slugify"]);
  expect(result.diagnostics).toEqual([]);
});

test("class discovery fails closed for nominal, abstract, and unrepresentable shapes", () => {
  const result = analyze(`import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Invalid nominal class. */
export abstract class Invalid<T> {
  private value: T;
  protected abstract read(): T;
  constructor(public input: T) { jaunt.magic(); }
  [Symbol.iterator](): Iterator<T> { return jaunt.magic(); }
}
`);
  const codes = new Set(result.diagnostics.map((item) => item.code));
  expect(codes).toContain("JAUNT_TS_ABSTRACT_CLASS");
  expect(codes).toContain("JAUNT_TS_ABSTRACT_MEMBER");
  expect(codes).toContain("JAUNT_TS_NOMINAL_MEMBER");
  expect(codes).toContain("JAUNT_TS_PARAMETER_PROPERTY");
  expect(codes).toContain("JAUNT_TS_UNSUPPORTED_MEMBER_NAME");
});

test("preserved bodies reject arbitrary runtime imports and spec-local values", () => {
  const result = analyze(`import * as jaunt from "@usejaunt/ts/spec";
import { readFileSync } from "node:fs";
jaunt.magicModule();
/** A governed helper that preserved code may not capture directly. */
export function helper(path: string): string { return jaunt.magic(); }
/** A guarded reader. */
export class Reader {
  constructor() { jaunt.magic(); }
  /** @jauntPreserve */
  read(path: string): string { return helper(readFileSync(path, "utf8")); }
}
`);
  expect(
    result.diagnostics.some(
      (item) => item.code === "JAUNT_TS_PRESERVE_REFERENCE",
    ),
  ).toBe(true);
});
