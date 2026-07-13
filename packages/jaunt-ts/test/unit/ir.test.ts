import ts from "@typescript/typescript6";
import { expect, test } from "vitest";
import { serializeType } from "../../src/analyzer/ir.js";
import { renderType, renderTypeImport } from "../../src/analyzer/mirror.js";

test("inline type-only bindings render as one valid type import", () => {
  expect(
    renderTypeImport({
      specifier: "./model.js",
      typeOnly: false,
      runtime: false,
      namedImports: [{ imported: "Foo", local: "Foo", typeOnly: true }],
    }),
  ).toBe('import type { Foo } from "./model.js";');
});

test("inline type literals retain optional nested properties", () => {
  const source = ts.createSourceFile(
    "spec.ts",
    "type Options = { ttlSeconds?: number; nested: { enabled: boolean } };",
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const alias = source.statements.find(ts.isTypeAliasDeclaration)!;
  const ir = serializeType(ts, alias.type);
  expect(ir).toMatchObject({ kind: "object" });
  expect(renderType(ir)).toBe(
    "{ ttlSeconds?: number; nested: { enabled: boolean } }",
  );
});

test("import types canonicalize quote style without losing the target", () => {
  const source = ts.createSourceFile(
    "spec.ts",
    `type Single = import('./types.js').Thing<string>;
type Double = import("./types.js").Thing<string>;`,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const aliases = source.statements.filter(ts.isTypeAliasDeclaration);
  const single = serializeType(ts, aliases[0]!.type);
  const double = serializeType(ts, aliases[1]!.type);

  expect(single).toEqual(double);
  expect(single).toEqual({
    kind: "import",
    text: '"./types.js"',
    name: "Thing",
    typeArguments: [{ kind: "string" }],
  });
});

test("type rendering preserves mutable arrays, literal kinds, and generic callables", () => {
  const source = ts.createSourceFile(
    "spec.ts",
    `type Mutable = string[];
type Flags = true | false | 1n | -2n;
type Generic = <T extends string>(value: T) => T[];
type Mapped<T> = { readonly [K in keyof T]?: T[K] };
type Template<T extends string> = \`prefix-\${T}\`;`,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TS,
  );
  const aliases = source.statements.filter(ts.isTypeAliasDeclaration);
  expect(renderType(serializeType(ts, aliases[0]!.type))).toBe("string[]");
  expect(renderType(serializeType(ts, aliases[1]!.type))).toContain("1n");
  expect(renderType(serializeType(ts, aliases[1]!.type))).toContain("-2n");
  expect(renderType(serializeType(ts, aliases[2]!.type))).toBe(
    "<T extends string>(value: T) => T[]",
  );
  expect(renderType(serializeType(ts, aliases[3]!.type))).toBe(
    "{ readonly [K in keyof T]?: T[K] }",
  );
  expect(renderType(serializeType(ts, aliases[4]!.type))).toBe("`prefix-${T}`");
});
