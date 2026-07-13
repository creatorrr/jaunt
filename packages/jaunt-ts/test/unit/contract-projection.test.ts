import compiler from "@typescript/typescript6";
import compiler58 from "@typescript/typescript58";
import { expect, test } from "vitest";
import { projectContractDeclaration } from "../../src/analyzer/contract_projection.js";

test("function projection preserves complex declarations and removes every implementation", () => {
  const source = `export interface Box<T> {
  /** The wrapped value. */
  value: T;
}
export type PublicTag = "public-tag" | { readonly nested: { ok: true } };

/** Convert a value without exposing its implementation. */
export function convert<T extends { id: string }>(
  value: T,
  mapper: (item: T) => { result: Promise<Box<T>> },
): { result: Promise<Box<T>> };
export async function convert<T extends { id: string }>(
  value: T = { id: "default-secret" } as T,
  mapper: (item: T) => { result: Promise<Box<T>> },
): Promise<{ result: Promise<Box<T>> }> {
  // body-comment-secret { }
  const expression = /[{}]/u;
  const template = \`template-secret \${value.id} { }\`;
  throw new Error("body-string-secret" + expression + template + mapper);
}
`;

  const projection = projectContractDeclaration(
    compiler,
    source,
    "convert",
    "src/convert.ts",
  );

  expect(projection.kind).toBe("function");
  expect(projection.source).toContain("export interface Box<T>");
  expect(projection.source).toContain('export type PublicTag = "public-tag"');
  expect(projection.source).toContain("): { result: Promise<Box<T>> };");
  expect(projection.source).toContain(
    "): Promise<{ result: Promise<Box<T>> }>;",
  );
  expect(projection.source).not.toContain("default-secret");
  expect(projection.source).not.toContain("body-comment-secret");
  expect(projection.source).not.toContain("template-secret");
  expect(projection.source).not.toContain("body-string-secret");
  expect(projection.source).not.toContain("/[{}]/u");
});

test("class projection keeps member TSDoc and signatures but strips executable spans", () => {
  const source = `export interface Settings { readonly name: string }
export type Snapshot<T> = { readonly value: T; readonly map: <U>(fn: (x: T) => U) => U };

/** A stateful store. */
export class Store<T extends { id: string }> {
  static { throw new Error("static-secret"); }
  readonly label: string = "field-secret";
  #token: string = "private-secret";

  /** Construct the store. */
  constructor(
    public readonly settings: Settings = { name: "parameter-secret" },
  ) { /* constructor-secret { } */ }

  /** Read one snapshot. */
  read(value: T): Snapshot<{ item: T }>;
  read(value: T): Snapshot<{ item: T }> {
    return { value: { item: value }, map: () => { throw "method-secret"; } };
  }

  /** Current value. */
  get current(): { item: T } { throw new Error(\`getter-secret \${this.label}\`); }
  set current(value: { item: T }) { throw new Error("setter-secret" + value); }
}
`;

  const projection = projectContractDeclaration(
    compiler,
    source,
    "Store",
    "src/store.ts",
  );

  expect(projection.kind).toBe("class");
  expect(projection.source).toContain("/** Construct the store. */");
  expect(projection.source).toMatch(
    /read\(value: T\): Snapshot<\{ item: T \}>\s*;/u,
  );
  expect(projection.source).toMatch(/get current\(\): \{ item: T \}\s*;/u);
  expect(projection.source).toMatch(/set current\(value: \{ item: T \}\)\s*;/u);
  for (const secret of [
    "static-secret",
    "field-secret",
    "private-secret",
    "parameter-secret",
    "constructor-secret",
    "method-secret",
    "getter-secret",
    "setter-secret",
  ]) {
    expect(projection.source).not.toContain(secret);
  }
});

test("projection removes nested binding defaults and rejects ambiguous inferred defaults", () => {
  const safe = `export function normalize(
  { value = "nested-secret" }: { value?: string } = { value: "outer-secret" },
): string { return value ?? "body-secret"; }
`;
  const projected = projectContractDeclaration(
    compiler,
    safe,
    "normalize",
    "src/normalize.ts",
  ).source;
  expect(projected).toMatch(
    /\{ value\s+\}: \{ value\?: string \}\s*,?\s*\): string;/u,
  );
  expect(projected).not.toContain("secret");

  expect(() =>
    projectContractDeclaration(
      compiler,
      'export function unsafe(value = "secret"): string { return value; }',
      "unsafe",
      "src/unsafe.ts",
    ),
  ).toThrow(/without an explicit type/u);
});

test("projection fails closed for malformed syntax and executable decorators or heritage", () => {
  expect(() =>
    projectContractDeclaration(
      compiler,
      "export function broken(value: string): { ok: true { return value; }",
      "broken",
      "src/broken.ts",
    ),
  ).toThrow(/not valid TypeScript/u);

  expect(() =>
    projectContractDeclaration(
      compiler,
      '@sealed("decorator-secret")\nexport class Unsafe {}',
      "Unsafe",
      "src/unsafe.ts",
    ),
  ).toThrow(/executable decorators/u);

  expect(() =>
    projectContractDeclaration(
      compiler,
      'export class Unsafe extends factory("heritage-secret") {}',
      "Unsafe",
      "src/unsafe.ts",
    ),
  ).toThrow(/executable heritage/u);
});

test("projection fails closed for retained initializers and executable computed names", () => {
  const unsafe = [
    {
      symbol: "read",
      source: `export function read(
  { [deriveKey("binding-secret")]: value }: Record<string, string>,
): string { return value; }`,
      error: /executable computed property name/u,
    },
    {
      symbol: "Store",
      source: `export class Store {
  [deriveKey("member-secret")](): string { return "body"; }
}`,
      error: /executable computed property name/u,
    },
    {
      symbol: "read",
      source: `export type Payload = { value: string = reveal("type-secret") };
export function read(value: Payload): string { return String(value); }`,
      error: /executable initializer/u,
    },
    {
      symbol: "read",
      source: `export interface Payload { value: string = reveal("interface-secret"); }
export function read(value: Payload): string { return String(value); }`,
      error: /executable initializer/u,
    },
    {
      symbol: "read",
      source: `export interface Payload extends deriveType("heritage-secret") {}
export function read(value: Payload): string { return String(value); }`,
      error: /executable heritage expression/u,
    },
  ];

  for (const value of unsafe) {
    expect(() =>
      projectContractDeclaration(
        compiler,
        value.source,
        value.symbol,
        `src/${value.symbol}.ts`,
      ),
    ).toThrow(value.error);
  }
});

test("projection hardening uses APIs available throughout the compiler range", () => {
  const safe = `export function value(input: { ok: true }): { ok: true } {
  return input;
}`;
  const unsafe = `export function value(
  { [deriveKey("secret")]: item }: Record<string, string>,
): string { return item; }`;
  for (const supportedCompiler of [compiler58, compiler]) {
    expect(
      projectContractDeclaration(
        supportedCompiler,
        safe,
        "value",
        "src/value.ts",
      ).source,
    ).toContain("export function value");
    expect(() =>
      projectContractDeclaration(
        supportedCompiler,
        unsafe,
        "value",
        "src/value.ts",
      ),
    ).toThrow(/executable computed property name/u);
  }
});

test("projection returns exact declaration and attached TSDoc ranges", () => {
  const source = `/** Public overload.\n * @jauntContract\n */
export function parse(value: string): { ok: true };
export function parse(value: string): { ok: true } {
  return { ok: value.length > 0 };
}
`;
  const projected = projectContractDeclaration(
    compiler,
    source,
    "parse",
    "src/parse.ts",
  );
  const docsStart = source.indexOf("/** Public overload.");
  const docsEnd = source.indexOf("*/", docsStart) + 2;
  const declarationStart = source.indexOf("export function parse");
  const declarationEnd = source.indexOf(";", declarationStart) + 1;

  expect(projected).toMatchObject({
    docsStart,
    docsEnd,
    declarationStart,
    declarationEnd,
  });
});
