import { spawn } from "node:child_process";
import {
  existsSync,
  mkdtempSync,
  mkdirSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { tmpdir } from "node:os";
import { afterEach, expect, test } from "vitest";
import { sha256Bytes } from "../../src/analyzer/canonical.js";
import {
  MAX_CAPTURED_STREAM_BYTES,
  readBoundedInput,
  runTestRunner,
} from "../../src/test/runner.js";
import { createFixtureWorkspace, packageRoot } from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

function write(root: string, path: string, content: string): void {
  const target = resolve(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function aliasedWorkspaceRoot(root: string): string {
  const parent = mkdtempSync(resolve(tmpdir(), "jaunt-ts-root-alias-"));
  roots.push(parent);
  const alias = resolve(parent, "workspace");
  symlinkSync(root, alias, "dir");
  return alias;
}

function managedTestSource(tier: "example" | "derived", body: string): string {
  const canonicalBody = `${body.trim()}\n`;
  return `// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with \`jaunt test\`.
// jaunt:tier=${tier}
// jaunt:source=tests/fixture.jaunt-test.ts
// jaunt:body_digest=${sha256Bytes(canonicalBody)}

${canonicalBody}`;
}

test("runner rejects files and overlays outside its workspace", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  await expect(
    runTestRunner({
      root: workspace.root,
      files: ["../secret.test.ts"],
      timeoutMs: 5_000,
      redactDerived: true,
      mode: "typecheck",
      compilerModulePath: workspace.compilerModulePath,
    }),
  ).rejects.toThrow(/escapes workspace root/);
  await expect(
    runTestRunner({
      root: workspace.root,
      files: [],
      projectConfigPaths: ["../secret-tsconfig.json"],
      timeoutMs: 5_000,
      redactDerived: true,
      mode: "run",
      compilerModulePath: workspace.compilerModulePath,
    }),
  ).rejects.toThrow(/escapes workspace root/);
});

test("symlink-installed runner executes its protected CLI entrypoint", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.ts",
    'export * from "./__generated__/index.js";\n',
  );
  write(
    workspace.root,
    "src/slug/__generated__/index.ts",
    "export function slugify(value: string): string { return value.toLowerCase(); }\n",
  );
  const child = spawn(
    process.execPath,
    [resolve(workspace.root, "node_modules/@usejaunt/ts/dist/test/runner.js")],
    {
      cwd: workspace.root,
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  child.stdin.end(
    JSON.stringify({
      root: workspace.root,
      files: ["src/app.ts"],
      tsconfigPath: "tsconfig.json",
      timeoutMs: 5_000,
      redactDerived: true,
      mode: "typecheck",
      compilerModulePath: workspace.compilerModulePath,
    }),
  );
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  const exitCode = await new Promise<number | null>((resolveExit) =>
    child.on("exit", resolveExit),
  );

  expect(exitCode, Buffer.concat(stderr).toString()).toBe(0);
  expect(Buffer.concat(stderr).toString()).toBe("");
  expect(JSON.parse(Buffer.concat(stdout).toString())).toMatchObject({
    ok: true,
    mode: "typecheck",
    diagnostics: [],
  });
});

test("runner typechecks inline test overlays without writing them", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/__generated__/slug.example.test.ts";
  write(
    workspace.root,
    "tsconfig.runner.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        include: ["tests/**/*.ts"],
      },
      null,
      2,
    ),
  );
  const source = managedTestSource(
    "example",
    `
import { expect, test } from "vitest";
test("typed", () => {
  expect(1 + 1).toBe(2);
  expect(Object.keys({ value: 1 })).toEqual(["value"]);
  expect(Reflect.get({ value: 1 }, "value")).toBe(1);
  const values = ["zero", "one"];
  expect(values[1]).toBe("one");
  const counts: Record<string, number> = { value: 1 };
  expect(counts["value"]).toBe(1);
});
`,
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "typecheck",
    tsconfigPath: "tsconfig.runner.json",
    overlays: { [path]: source },
    compilerModulePath: workspace.compilerModulePath,
  });
  expect(result.ok, JSON.stringify(result.diagnostics)).toBe(true);
});

test("generated batteries audit undeclared workspace aliases before execution", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "packages/app/tests/__generated__/alias.example.test.ts";
  write(
    workspace.root,
    "packages/base/package.json",
    JSON.stringify(
      { name: "@fixture/base", private: true, type: "module" },
      null,
      2,
    ),
  );
  const appManifest = (declared: boolean): string =>
    JSON.stringify(
      {
        name: "@fixture/app",
        private: true,
        type: "module",
        devDependencies: {
          vitest: "4.1.10",
          ...(declared ? { "@fixture/base": "workspace:*" } : {}),
        },
      },
      null,
      2,
    );
  write(workspace.root, "packages/app/package.json", appManifest(false));
  write(
    workspace.root,
    "packages/base/src/helper.ts",
    "export const helper = (value: string): string => value.trim();\n",
  );
  write(
    workspace.root,
    "packages/app/tsconfig.test.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          baseUrl: "../..",
          paths: { "@workspace/base": ["packages/base/src/helper.ts"] },
          ignoreDeprecations: "6.0",
          types: [],
        },
        include: ["tests/**/*.ts", "../base/src/**/*.ts"],
      },
      null,
      2,
    ),
  );
  const bodies = [
    `import { helper } from "@workspace/base";
import { expect, test } from "vitest";
test("alias", () => expect(helper(" x ")).toBe("x"));`,
    `export { helper } from "@workspace/base";
import { expect, test } from "vitest";
test("export", () => expect(true).toBe(true));`,
    `void import("@workspace/base");
import { expect, test } from "vitest";
test("dynamic", () => expect(true).toBe(true));`,
    `declare const require: any;
const helper = require("@workspace/base").helper;
import { expect, test } from "vitest";
test("require", () => expect(helper(" x ")).toBe("x"));`,
    `import helperModule = require("@workspace/base");
import { expect, test } from "vitest";
test("import equals", () => expect(helperModule.helper(" x ")).toBe("x"));`,
  ];
  for (const body of bodies) {
    const result = await runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: false,
      mode: "typecheck",
      tsconfigPath: "packages/app/tsconfig.test.json",
      projectConfigPaths: ["packages/app/tsconfig.test.json"],
      overlays: { [path]: managedTestSource("example", body) },
      compilerModulePath: workspace.compilerModulePath,
    });
    expect(result.ok).toBe(false);
    expect(result.diagnostics).toContainEqual(
      expect.objectContaining({
        code: "JAUNT_TS_UNDECLARED_PACKAGE",
        path,
      }),
    );
    expect(existsSync(resolve(workspace.root, path))).toBe(false);
  }

  write(workspace.root, "packages/app/package.json", appManifest(true));
  const allowed = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "typecheck",
    tsconfigPath: "packages/app/tsconfig.test.json",
    projectConfigPaths: ["packages/app/tsconfig.test.json"],
    overlays: { [path]: managedTestSource("example", bodies[0]!) },
    compilerModulePath: workspace.compilerModulePath,
  });
  expect(allowed.ok, JSON.stringify(allowed.diagnostics)).toBe(true);
  expect(existsSync(resolve(workspace.root, path))).toBe(false);
});

test("generated batteries reject Jaunt tooling even when the test owner declares it", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/__generated__/tooling.example.test.ts";
  write(
    workspace.root,
    "package.json",
    JSON.stringify(
      {
        name: "fixture",
        private: true,
        type: "module",
        devDependencies: {
          "@usejaunt/ts": "0.1.0-alpha.0",
          typescript: "6.0.2",
          vitest: "4.1.10",
        },
      },
      null,
      2,
    ),
  );
  write(
    workspace.root,
    "tsconfig.runner.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        include: ["tests/**/*.ts"],
      },
      null,
      2,
    ),
  );
  const toolingBodies = [
    `import * as tooling from "@usejaunt/ts";
	import { expect, test } from "vitest";
	test("tooling", () => expect(tooling).toBeDefined());`,
    `type Tooling = import("@usejaunt/ts").MagicOptions;
import { expect, test } from "vitest";
test("tooling type", () => expect(true).toBe(true));
void (0 as unknown as Tooling);`,
    `/// <reference types="@usejaunt/ts" />
import { expect, test } from "vitest";
test("tooling reference", () => expect(true).toBe(true));`,
  ];
  for (const body of toolingBodies) {
    const result = await runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: false,
      mode: "typecheck",
      tsconfigPath: "tsconfig.runner.json",
      overlays: { [path]: managedTestSource("example", body) },
      compilerModulePath: workspace.compilerModulePath,
    });
    expect(result.ok).toBe(false);
    expect(result.diagnostics).toContainEqual(
      expect.objectContaining({
        code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
        path,
      }),
    );
  }

  const directives = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "typecheck",
    tsconfigPath: "tsconfig.runner.json",
    overlays: {
      [path]: managedTestSource(
        "example",
        `/// <reference path="../../src/private.jaunt.ts" />
/// <reference lib="dom" />
/// <reference no-default-lib="true" />
/// <amd-dependency path="legacy-loader" />
/// <amd-module name="legacy-tests" />
///\u00a0<Reference path="../../src/private.jaunt.ts" />
\u200b/// <AMD-MODULE name="mixed-case-tests" />
/*lead*/ /// <Reference path="../../src/private.jaunt.ts" />
import { expect, test } from "vitest";
test("directives", () => expect(true).toBe(true));`,
      ),
    },
    compilerModulePath: workspace.compilerModulePath,
  });
  expect(directives.ok).toBe(false);
  expect(directives.diagnostics).toContainEqual(
    expect.objectContaining({
      code: "JAUNT_TS_TEST_REFERENCE_DIRECTIVE",
      path,
    }),
  );
  expect(existsSync(resolve(workspace.root, path))).toBe(false);
});

test("generated batteries audit package imports aliases by logical target", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/__generated__/aliases.example.test.ts";
  write(
    workspace.root,
    "package.json",
    JSON.stringify(
      {
        name: "fixture",
        private: true,
        type: "module",
        imports: {
          "#tooling": "@usejaunt/ts/spec",
          "#external": "not-declared-package",
          "#internal": "./tests/support.ts",
        },
        devDependencies: {
          "@usejaunt/ts": "0.1.0-alpha.0",
          typescript: "6.0.2",
          vitest: "4.1.10",
        },
      },
      null,
      2,
    ),
  );
  write(
    workspace.root,
    "tsconfig.runner.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        include: ["tests/**/*.ts"],
      },
      null,
      2,
    ),
  );
  write(
    workspace.root,
    "tests/support.ts",
    'export const supported = "yes" as const;\n',
  );
  const check = (body: string) =>
    runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: false,
      mode: "typecheck",
      tsconfigPath: "tsconfig.runner.json",
      overlays: { [path]: managedTestSource("example", body) },
      compilerModulePath: workspace.compilerModulePath,
    });

  const tooling = await check(`import * as tooling from "#tooling";
import { expect, test } from "vitest";
test("tooling", () => expect(tooling).toBeDefined());`);
  expect(tooling.ok).toBe(false);
  expect(tooling.diagnostics).toContainEqual(
    expect.objectContaining({ code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT", path }),
  );

  const external = await check(`import value from "#external";
import { expect, test } from "vitest";
test("external", () => expect(value).toBeDefined());`);
  expect(external.ok).toBe(false);
  expect(external.diagnostics).toContainEqual(
    expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE", path }),
  );

  const internal = await check(`import { supported } from "#internal";
import { expect, test } from "vitest";
test("internal", () => expect(supported).toBe("yes"));`);
  expect(internal.ok, JSON.stringify(internal.diagnostics)).toBe(true);
  expect(existsSync(resolve(workspace.root, path))).toBe(false);
});

test.each([
  [
    "resolved path alias",
    'import { hidden } from "@private/value";\nvoid hidden;\n',
    "JAUNT_TS_TEST_PRIVATE_IMPORT",
  ],
  [
    "CommonJS call after division",
    'declare const require: any;\nconst ratio = ({} as any) / 2;\nconst hidden = require("../src/machine/value.js");\nvoid ratio; void hidden;\n',
    "JAUNT_TS_TEST_PRIVATE_IMPORT",
  ],
  [
    "optional CommonJS call",
    'declare const require: any;\nconst hidden = require?.("../src/machine/value.js");\nvoid hidden;\n',
    "JAUNT_TS_TEST_PRIVATE_IMPORT",
  ],
  [
    "parenthesized non-null CommonJS call",
    'declare const require: any;\nconst hidden = (require!)("../src/machine/value.js");\nvoid hidden;\n',
    "JAUNT_TS_TEST_PRIVATE_IMPORT",
  ],
  [
    "computed module.require",
    'declare const module: any;\nmodule["require"]("../src/machine/value.js");\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "require.call",
    'declare const require: any;\nrequire.call(null, "../src/machine/value.js");\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "aliased require",
    'declare const require: any;\nconst load = require;\nload("../src/machine/value.js");\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "createRequire loader",
    'import { createRequire } from "node:module";\nconst load = createRequire(import.meta.url);\nload("../src/machine/value.js");\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "nonliteral require",
    'declare const require: any;\nconst path = "../src/machine/value.js";\nrequire(path);\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "nonliteral dynamic import",
    'const path = "../src/machine/value.js";\nvoid import(path);\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  ["eval", 'eval("void 0");\n', "JAUNT_TS_TEST_DYNAMIC_LOADER"],
  [
    "Function constructor",
    'new Function("return 0")();\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "worker permission escape",
    'new Worker(new URL("data:text/javascript,export{}"), { execArgv: [] });\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "process builtin escape",
    'process.getBuiltinModule("node:fs");\n',
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "reflective global loader escape",
    `const proc = Reflect.get(globalThis, "pro" + "cess");
const getBuiltin = Reflect.get(proc, ["getBuiltin", "Module"].join(""));
const moduleApi = Reflect.apply(getBuiltin, proc, ["node:" + "module"]);
const create = Reflect.get(moduleApi, \`create\${"Require"}\`);
const load = Reflect.apply(create, moduleApi, [import.meta.url]);
Reflect.apply(load, undefined, ["totally-undeclared-package"]);
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "computed constructor escape",
    `const makeFunction = (() => undefined)[["con", "structor"].join("") as "constructor"];
new (makeFunction as unknown as { new (body: string): () => unknown })("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "const-bound constructor escape",
    `const key: string = "constructor";
const makeFunction = (() => undefined)[key];
new (makeFunction as unknown as { new (body: string): () => unknown })("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "runtime-computed constructor escape",
    `const key = "constructor".slice(0) as "constructor";
const makeFunction = (() => undefined)[key];
new (makeFunction as unknown as { new (body: string): () => unknown })("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "opaque computed constructor escape",
    `const identity = (value: string): string => value;
const key = identity("constructor");
const disguised = (() => undefined) as unknown as Record<string, unknown>;
const makeFunction = disguised[key];
new (makeFunction as { new (body: string): () => unknown })("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "reflective computed constructor escape",
    `const identity = (value: string): string => value;
const makeFunction = Reflect.get(() => undefined, identity("constructor"));
new (makeFunction as { new (body: string): () => unknown })("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "type-laundered computed constructor escape",
    `const identity = (value: string): string => value;
const key = identity("constructor") as "value";
const disguised = (() => undefined) as unknown as Record<"value", number>;
const recovered = disguised[key] as unknown as (body: string) => () => unknown;
void recovered("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "type-laundered reflective constructor escape",
    `const key = "constructor".slice(0) as "value";
const recovered = Reflect.get(() => undefined, key) as (body: string) => () => unknown;
void recovered("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "number-laundered computed constructor escape",
    `const identity = (value: string): string => value;
const key = identity("constructor") as unknown as number;
const disguised = (() => undefined) as unknown as Record<number, number>;
const recovered = disguised[key] as unknown as (body: string) => () => unknown;
void recovered("return globalThis")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "prototype descriptor constructor escape",
    `const descriptor = Object.getOwnPropertyDescriptor(
  Object.getPrototypeOf(() => undefined),
  "constructor",
);
const makeFunction = descriptor?.value as (body: string) => () => unknown;
void makeFunction("return process")();
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "computed namespace createRequire escape",
    `import * as moduleApi from "node:module";
const key = ["create", "Require"].join("");
const load = (moduleApi as Record<string, (...args: string[]) => unknown>)[key]!;
void load(import.meta.url);
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
  [
    "proxy global escape",
    `const root = new Proxy(globalThis, {});
void Reflect.get(root, ["pro", "cess"].join(""));
`,
    "JAUNT_TS_TEST_DYNAMIC_LOADER",
  ],
])(
  "typecheck rejects generated-private imports through %s",
  async (_label, source, expectedCode) => {
    const workspace = createFixtureWorkspace();
    roots.push(workspace.root);
    const path = "tests/machine-escape.example.test.ts";
    write(workspace.root, "src/machine/value.ts", "export const hidden = 1;\n");
    write(
      workspace.root,
      "tsconfig.runner.json",
      JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            types: [],
            baseUrl: ".",
            paths: { "@private/*": ["src/machine/*"] },
          },
          include: ["tests/**/*.ts"],
        },
        null,
        2,
      ),
    );
    const result = await runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: true,
      mode: "typecheck",
      tsconfigPath: "tsconfig.runner.json",
      overlays: { [path]: source },
      compilerModulePath: workspace.compilerModulePath,
      generatedDir: "machine",
    });
    expect(result.ok).toBe(false);
    expect(result.diagnostics).toContainEqual(
      expect.objectContaining({ code: expectedCode, path }),
    );
  },
);

test("typecheck accepts same-file const string keys on ordinary records", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/record-key.example.test.ts";
  write(
    workspace.root,
    "tsconfig.runner.json",
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
        noEmit: true,
        types: [],
      },
      include: ["tests/**/*.ts"],
    }),
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "typecheck",
    tsconfigPath: "tsconfig.runner.json",
    overlays: {
      [path]: managedTestSource(
        "example",
        `const rolesKey: string = "x-hasura-allowed-roles";
const malformedRolesToken: Record<string, unknown> = { [rolesKey]: "admin" };
const role = malformedRolesToken[rolesKey];
const { [rolesKey]: destructuredRole } = malformedRolesToken;
void role;
void destructuredRole;
`,
      ),
    },
    compilerModulePath: workspace.compilerModulePath,
  });

  expect(result.ok, JSON.stringify(result.diagnostics)).toBe(true);
  expect(result.diagnostics).toEqual([]);
});

test.each(["@typescript/typescript58", "@typescript/typescript6"] as const)(
  "typecheck resolves the referenced same-file const under %s",
  async (compilerPackage) => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    const path = "tests/record-key.example.test.ts";
    write(
      workspace.root,
      "tsconfig.runner.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        include: ["tests/**/*.ts"],
      }),
    );
    const result = await runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: false,
      mode: "typecheck",
      tsconfigPath: "tsconfig.runner.json",
      overlays: {
        [path]: managedTestSource(
          "example",
          `const rolesKey: string = "x-hasura-allowed-roles";
function unrelatedBinding(): string {
  const rolesKey: string = "constructor";
  return rolesKey;
}
const malformedRolesToken: Record<string, unknown> = { [rolesKey]: "admin" };
const role = malformedRolesToken[rolesKey];
void unrelatedBinding;
void role;
`,
        ),
      },
      compilerModulePath: workspace.compilerModulePath,
    });

    expect(result.ok, JSON.stringify(result.diagnostics)).toBe(true);
    expect(result.diagnostics).toEqual([]);
  },
);

test.each(["@typescript/typescript58", "@typescript/typescript6"] as const)(
  "typecheck rejects a const literal from an out-of-scope shadow under %s",
  async (compilerPackage) => {
    const workspace = createFixtureWorkspace({ compilerPackage });
    roots.push(workspace.root);
    const path = "tests/record-key.example.test.ts";
    write(
      workspace.root,
      "tests/globals.d.ts",
      "declare const rolesKey: string;\n",
    );
    write(
      workspace.root,
      "tsconfig.runner.json",
      JSON.stringify({
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        include: ["tests/**/*.ts"],
      }),
    );
    const result = await runTestRunner({
      root: workspace.root,
      files: [path],
      timeoutMs: 5_000,
      redactDerived: false,
      mode: "typecheck",
      tsconfigPath: "tsconfig.runner.json",
      overlays: {
        [path]: managedTestSource(
          "example",
          `export {};
{
  const rolesKey: string = "x-hasura-allowed-roles";
  void rolesKey;
}
const malformedRolesToken: Record<string, unknown> = {};
const role = malformedRolesToken[rolesKey];
void role;
`,
        ),
      },
      compilerModulePath: workspace.compilerModulePath,
    });

    expect(result.ok).toBe(false);
    expect(result.diagnostics).toEqual([
      expect.objectContaining({
        code: "JAUNT_TS_TEST_DYNAMIC_LOADER",
        path,
      }),
    ]);
  },
);

test("redacted typecheck diagnostics expose structure but not literal detail", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/__generated__/type-error.derived.test.ts";
  write(
    workspace.root,
    "tsconfig.runner.json",
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
        noEmit: true,
        types: [],
      },
      include: ["tests/**/*.ts"],
    }),
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "typecheck",
    tsconfigPath: "tsconfig.runner.json",
    overlays: {
      [path]: 'const value: "safe" = "TYPECHECK-SENTINEL";\n',
    },
    compilerModulePath: workspace.compilerModulePath,
  });
  expect(result.ok).toBe(false);
  expect(result.diagnostics).toContainEqual(
    expect.objectContaining({ code: "TS2322", path }),
  );
  expect(JSON.stringify(result)).not.toContain("TYPECHECK-SENTINEL");
});

test("runner proves normal JavaScript and declaration emit without writing outputs", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "tsconfig.emit.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          declaration: true,
          rootDir: "src",
          outDir: "dist",
          types: [],
        },
        include: ["src/**/*.ts"],
        exclude: ["src/**/*.jaunt.ts"],
      },
      null,
      2,
    ),
  );
  write(
    workspace.root,
    "src/slug/index.ts",
    'export * from "./__generated__/index.js";\n',
  );
  write(
    workspace.root,
    "src/slug/__generated__/index.ts",
    "// jaunt:generated\nexport const slugify = (): string => 'stale';\n",
  );
  write(
    workspace.root,
    "src/slug/__generated__/index.api.ts",
    "// jaunt:api-mirror\nexport declare function slugify(value: string): string;\n",
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: ["src/slug/index.ts"],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "typecheck",
    tsconfigPath: "tsconfig.emit.json",
    normalEmit: true,
    declarationEmit: true,
    deletedFiles: [
      "src/slug/index.jaunt.ts",
      "src/slug/__generated__/index.ts",
      "src/slug/__generated__/index.api.ts",
    ],
    packageRoot: ".",
    overlays: {
      "src/slug/index.ts":
        "export function slugify(value: string): string { return value.toLowerCase(); }\n",
    },
    compilerModulePath: workspace.compilerModulePath,
  });

  expect(result.ok, JSON.stringify(result.diagnostics)).toBe(true);
  expect(result.emittedJavaScript).toContain("dist/slug/index.js");
  expect(result.emittedDeclarations).toContain("dist/slug/index.d.ts");
  expect(existsSync(resolve(workspace.root, "dist"))).toBe(false);
});

test("normal emit rejects unsafe package output and Jaunt provenance", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src/app.ts"));
  write(
    workspace.root,
    "tsconfig.emit.json",
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
        declaration: true,
        rootDir: "src",
        outDir: "dist",
        types: [],
      },
      include: ["src/**/*.ts"],
      exclude: ["src/**/*.jaunt.ts"],
    }),
  );
  write(workspace.root, "src/value.ts", "export const value = 1;\n");
  mkdirSync(resolve(workspace.root, "packages/owner"), { recursive: true });
  const base = {
    root: workspace.root,
    files: ["src/value.ts"],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "typecheck" as const,
    tsconfigPath: "tsconfig.emit.json",
    normalEmit: true,
    declarationEmit: true,
    compilerModulePath: workspace.compilerModulePath,
  };
  const escaped = await runTestRunner({
    ...base,
    packageRoot: "packages/owner",
  });
  expect(escaped.ok).toBe(false);
  expect(escaped.diagnostics).toContainEqual(
    expect.objectContaining({ code: "JAUNT_TS_EJECT_PACKAGE_ESCAPE" }),
  );

  const provenance = await runTestRunner({
    ...base,
    packageRoot: ".",
    overlays: {
      "src/value.ts": "// jaunt:generated\nexport const value = 1;\n",
    },
  });
  expect(provenance.ok).toBe(false);
  expect(provenance.diagnostics).toContainEqual(
    expect.objectContaining({ code: "JAUNT_TS_EJECT_UNSAFE_OUTPUT" }),
  );
});

test("runner executes a new inline test overlay without leaving it on disk", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const root = aliasedWorkspaceRoot(workspace.root);
  const path = "tests/nested/__generated__/overlay.example.test.ts";
  const result = await runTestRunner({
    root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "run",
    overlays: {
      [path]: managedTestSource(
        "example",
        `
import { expect, test } from "vitest";
test("overlay", () => expect(6 * 7).toBe(42));
`,
      ),
    },
  });

  expect(result.ok, JSON.stringify(result)).toBe(true);
  expect(result.tests).toContainEqual(
    expect.objectContaining({ status: "passed", tier: "example" }),
  );
  expect(existsSync(resolve(root, path))).toBe(false);
  expect(existsSync(resolve(root, "tests/nested"))).toBe(false);
});

test("permission sandbox forces nested workers to retain filesystem restrictions", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const outside = mkdtempSync(resolve(tmpdir(), "jaunt-ts-secret-"));
  roots.push(outside);
  const secret = resolve(outside, "derived-source.txt");
  writeFileSync(secret, "NESTED-WORKER-HELD-OUT-SENTINEL\n");
  write(workspace.root, "pnpm-workspace.yaml", "packages: []\n");
  const path = "tests/__generated__/worker.example.test.ts";
  const permissionFlag = process.allowedNodeEnvironmentFlags.has("--permission")
    ? "--permission"
    : "--experimental-permission";
  const disablePermissionFlag =
    permissionFlag === "--permission"
      ? "--no-permission"
      : "--no-experimental-permission";
  write(
    workspace.root,
    path,
    managedTestSource(
      "example",
      `
import { EventEmitter } from "node:events";
import { SHARE_ENV, Worker } from "node:worker_threads";
import { expect, test } from "vitest";
test("worker stays restricted", async () => {
  const code = String.raw\`const { parentPort } = require("node:worker_threads");
    const fs = require("node:fs");
    try {
      fs.readFileSync(${JSON.stringify(secret)}, "utf8");
      parentPort.postMessage({ code: "READ_SUCCEEDED" });
    } catch (error) {
      parentPort.postMessage({ code: error.code, permission: error.permission });
    }\`;
  expect(
    globalThis[Symbol.for("@usejaunt/ts/permission-guard-installed")],
  ).toBe(true);
  expect(process.execArgv).toEqual([]);
  expect(process.env.NODE_OPTIONS).toContain(${JSON.stringify(permissionFlag)});
  expect(process.env.NODE_OPTIONS).not.toContain("--allow-worker");
  expect(process.env.NODE_OPTIONS).not.toContain(${JSON.stringify(disablePermissionFlag)});
  const priorNodeOptions = process.env.NODE_OPTIONS;
  process.env.NODE_OPTIONS = ${JSON.stringify(disablePermissionFlag)};
  try {
    expect(Object.getPrototypeOf(Worker)).toBe(EventEmitter);
    expect(Worker.listenerCount).toBe(EventEmitter.listenerCount);
    expect(Worker.getEventListeners).toBe(EventEmitter.getEventListeners);
    expect(Worker.prototype.constructor).toBe(Worker);
    const attempts = [
      [Worker, { execArgv: [], env: { NODE_OPTIONS: ${JSON.stringify(disablePermissionFlag)} } }],
      [Worker, { execArgv: [${JSON.stringify(disablePermissionFlag)}], env: { NODE_OPTIONS: ${JSON.stringify(disablePermissionFlag)} } }],
      [Worker, { execArgv: [], env: SHARE_ENV }],
      [Worker.prototype.constructor, { execArgv: [${JSON.stringify(disablePermissionFlag)}], env: SHARE_ENV }],
    ];
    const outcomes = await Promise.all(attempts.map(([WorkerConstructor, workerOptions]) => {
      try {
        const worker = new WorkerConstructor(code, { eval: true, ...workerOptions });
        return new Promise((resolve) => {
          worker.once("message", resolve);
          worker.once("error", (error) => resolve({
            code: error.code,
            permission: error.permission,
          }));
        });
      } catch (error) {
        return { code: error.code, permission: error.permission };
      }
    }));
    expect(outcomes).toEqual(Array.from({ length: attempts.length }, () => ({
      code: "ERR_ACCESS_DENIED",
      permission: "WorkerThreads",
    })));
  } finally {
    if (priorNodeOptions === undefined) delete process.env.NODE_OPTIONS;
    else process.env.NODE_OPTIONS = priorNodeOptions;
  }
});
`,
    ),
  );
  const sandboxRoot = realpathSync(workspace.root);
  const sandboxPackageRoot = realpathSync(packageRoot);
  const permissionGuard = resolve(
    sandboxPackageRoot,
    "dist/test/permission_guard.cjs",
  );
  const child = spawn(
    process.execPath,
    [
      permissionFlag,
      "--allow-addons",
      "--allow-worker",
      `--require=${permissionGuard}`,
      `--allow-fs-read=${sandboxRoot}`,
      `--allow-fs-read=${sandboxPackageRoot}`,
      `--allow-fs-write=${sandboxRoot}`,
      resolve(sandboxPackageRoot, "dist/test/runner.js"),
    ],
    {
      cwd: sandboxRoot,
      env: process.env,
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  child.stdin.end(
    JSON.stringify({
      root: sandboxRoot,
      files: [path],
      timeoutMs: 10_000,
      // This fixture contains no held-out battery. Keep infrastructure errors
      // visible so platform permission regressions name the denied resource.
      redactDerived: false,
      mode: "run",
      tier: "example",
      permissionSandbox: true,
      // The configured component need not exist at the workspace root; keep
      // that physical containment probe covered by the permission-model test.
      generatedDir: "__generated__",
    }),
  );
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  const exitCode = await new Promise<number | null>((resolveExit) =>
    child.on("exit", resolveExit),
  );
  const result = JSON.parse(Buffer.concat(stdout).toString());
  const failureContext = [
    Buffer.concat(stderr).toString().trim(),
    JSON.stringify(result),
  ]
    .filter(Boolean)
    .join("\n");

  expect(exitCode, failureContext).toBe(0);
  expect(result, failureContext).toMatchObject({ ok: true, mode: "run" });
  expect(JSON.stringify(result)).not.toContain(
    "NESTED-WORKER-HELD-OUT-SENTINEL",
  );
}, 20_000);

test("runner does not trust an example tier marker outside managed provenance", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "tests/__generated__/spoof.example.test.ts";
  write(
    workspace.root,
    path,
    `import { test } from "vitest";
test("held out", () => { throw new Error("TIER-SPOOF-SENTINEL"); });
// jaunt:tier=example
`,
  );

  const result = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
  });

  expect(result.ok).toBe(false);
  expect(result.tests).toHaveLength(1);
  expect(result.tests[0]).toMatchObject({ category: "runtime" });
  expect(Object.keys(result.tests[0]!).sort()).toEqual(["caseId", "category"]);
  expect(JSON.stringify(result)).not.toContain("TIER-SPOOF-SENTINEL");
});

test("runner refuses to execute mixed tiers in one filesystem", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "tests/__generated__/pass.example.test.ts",
    managedTestSource(
      "example",
      `
import { expect, test } from "vitest";
test("passes", () => expect(2 + 2).toBe(4));
`,
    ),
  );
  write(
    workspace.root,
    "tests/__generated__/secret.derived.test.ts",
    `// jaunt:tier=derived
import { expect, test } from "vitest";
test("hidden", () => { console.error("SENTINEL-SECRET"); expect("SENTINEL-SECRET").toBe("safe"); });
`,
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [
      "tests/__generated__/pass.example.test.ts",
      "tests/__generated__/secret.derived.test.ts",
    ],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
  });
  expect(result.ok).toBe(false);
  expect(result.tests).toEqual([
    { caseId: "opaque-runner-failure", category: "runner" },
  ]);
  expect(JSON.stringify(result)).not.toContain("SENTINEL-SECRET");
});

test("example execution cannot read a sibling derived battery", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const sentinel = "HELD_OUT_EXPECTATION_7af93";
  write(
    workspace.root,
    "tests/__generated__/reader.example.test.ts",
    managedTestSource(
      "example",
      `
import { readFileSync } from "node:fs";
import { test } from "vitest";
test("cannot exfiltrate", () => {
  throw new Error(readFileSync(new URL("./secret.derived.test.ts", import.meta.url), "utf8"));
});
`,
    ),
  );
  write(
    workspace.root,
    "tests/__generated__/secret.derived.test.ts",
    `import { test } from "vitest"; test(${JSON.stringify(sentinel)}, () => {});\n`,
  );

  const result = await runTestRunner({
    root: workspace.root,
    files: ["tests/__generated__/reader.example.test.ts"],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
    tier: "example",
  });

  expect(result.ok).toBe(false);
  expect(JSON.stringify(result)).not.toContain(sentinel);
  expect(result.tests).toEqual([
    { caseId: "opaque-runner-failure", category: "runner" },
  ]);
});

test("runner redacts aggregate, diff, snapshot, warning, setup, and teardown surfaces", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "tests/__generated__/surfaces.derived.test.ts",
    `// jaunt:tier=derived
import { afterAll, beforeAll, expect, test } from "vitest";
beforeAll(() => { console.warn("WARNING-SENTINEL"); });
afterAll(() => { console.error("TEARDOWN-OUTPUT-SENTINEL"); });
test("hidden surfaces", () => {
  const cause = new Error("CAUSE-SENTINEL");
  const error = new AggregateError([cause], "MESSAGE-SENTINEL", { cause });
  error.stack = "STACK-SENTINEL";
  Object.assign(error, { diff: "DIFF-SENTINEL", snapshot: "SNAPSHOT-SENTINEL" });
  process.stdout.write("STDOUT-SENTINEL");
  process.stderr.write("STDERR-SENTINEL");
  throw error;
});
`,
  );
  write(
    workspace.root,
    "tests/__generated__/setup.derived.test.ts",
    `// jaunt:tier=derived
import { beforeAll, test } from "vitest";
beforeAll(() => { throw new Error("SETUP-SENTINEL"); });
test("never exposes setup", () => {});
`,
  );
  write(
    workspace.root,
    "tests/__generated__/teardown.derived.test.ts",
    `// jaunt:tier=derived
import { afterAll, test } from "vitest";
afterAll(() => { throw new Error("TEARDOWN-SENTINEL"); });
test("never exposes teardown", () => {});
`,
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [
      "tests/__generated__/surfaces.derived.test.ts",
      "tests/__generated__/setup.derived.test.ts",
      "tests/__generated__/teardown.derived.test.ts",
    ],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
  });
  const rendered = JSON.stringify(result);
  expect(result.ok).toBe(false);
  for (const sentinel of [
    "WARNING-SENTINEL",
    "TEARDOWN-OUTPUT-SENTINEL",
    "MESSAGE-SENTINEL",
    "STACK-SENTINEL",
    "DIFF-SENTINEL",
    "SNAPSHOT-SENTINEL",
    "CAUSE-SENTINEL",
    "STDOUT-SENTINEL",
    "STDERR-SENTINEL",
    "SETUP-SENTINEL",
    "TEARDOWN-SENTINEL",
  ]) {
    expect(rendered).not.toContain(sentinel);
  }
});

test("redacted config failure returns a minimal opaque fallback", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "vitest.secret.config.mjs",
    `throw new Error("CONFIG-SENTINEL");\n`,
  );
  write(
    workspace.root,
    "tests/__generated__/config.derived.test.ts",
    `// jaunt:tier=derived
import { test } from "vitest";
test("not collected", () => {});
`,
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: ["tests/__generated__/config.derived.test.ts"],
    vitestConfigPath: "vitest.secret.config.mjs",
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
  });
  expect(result).toMatchObject({
    ok: false,
    tests: [{ caseId: "opaque-runner-failure", category: "runner" }],
    captured: { stdout: "", stderr: "" },
  });
  expect(Object.keys(result.tests[0]!).sort()).toEqual(["caseId", "category"]);
  expect(JSON.stringify(result)).not.toContain("CONFIG-SENTINEL");
});

test("runner resolves package aliases through the owning referenced source project", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const root = aliasedWorkspaceRoot(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
  write(
    workspace.root,
    "tsconfig.test.json",
    JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          types: [],
        },
        references: [{ path: "./packages/app" }],
        include: ["tests/**/*.ts"],
      },
      null,
      2,
    ),
  );
  const project = (extra: Record<string, unknown> = {}): string =>
    JSON.stringify(
      {
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
          ...(extra.compilerOptions as Record<string, unknown> | undefined),
        },
        include: ["src/**/*.ts"],
        ...(Array.isArray(extra.references)
          ? { references: extra.references }
          : {}),
      },
      null,
      2,
    );
  write(workspace.root, "packages/core/tsconfig.json", project());
  write(
    workspace.root,
    "packages/app/tsconfig.json",
    project({
      compilerOptions: {
        baseUrl: ".",
        paths: { "@fixture/core/*": ["../core/src/*"] },
      },
      references: [{ path: "../core" }],
    }),
  );
  write(
    workspace.root,
    "packages/core/package.json",
    JSON.stringify({
      name: "@fixture/core",
      private: true,
      type: "module",
      exports: { "./*": "./dist/*" },
    }),
  );
  write(
    workspace.root,
    "packages/core/src/normalize/index.ts",
    'export function normalize(): string { return "source"; }\n',
  );
  write(
    workspace.root,
    "packages/core/dist/normalize/index.js",
    'export function normalize() { throw new Error("stale dist placeholder"); }\n',
  );
  write(
    workspace.root,
    "packages/app/src/feature/__generated__/index.ts",
    `import { normalize } from "@fixture/core/normalize/index.js";
export function feature(): string { return normalize(); }
`,
  );
  write(
    workspace.root,
    "packages/app/src/feature/index.ts",
    'export * from "./__generated__/index.js";\n',
  );
  write(
    workspace.root,
    "tests/__generated__/project.example.test.ts",
    managedTestSource(
      "example",
      `
import { expect, test } from "vitest";
import { feature } from "../../packages/app/src/feature/index.js";
test("uses referenced source", () => expect(feature()).toBe("source"));
`,
    ),
  );
  mkdirSync(resolve(workspace.root, "node_modules/@fixture"), {
    recursive: true,
  });
  symlinkSync(
    resolve(workspace.root, "packages/core"),
    resolve(workspace.root, "node_modules/@fixture/core"),
    "dir",
  );

  const result = await runTestRunner({
    root,
    files: ["tests/__generated__/project.example.test.ts"],
    timeoutMs: 15_000,
    redactDerived: false,
    mode: "run",
    tsconfigPath: "tsconfig.test.json",
    projectConfigPaths: [
      "tsconfig.test.json",
      "packages/app/tsconfig.json",
      "packages/core/tsconfig.json",
    ],
    compilerModulePath: resolve(
      root,
      "node_modules/typescript/lib/typescript.js",
    ),
  });

  expect(result.ok, JSON.stringify(result)).toBe(true);
  expect(result.tests).toContainEqual(
    expect.objectContaining({ status: "passed", tier: "example" }),
  );
}, 20_000);

test("explicit unredacted mode exposes derived diagnostics and captured output", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "tests/__generated__/secret.derived.test.ts",
    `// jaunt:tier=derived
import { expect, test } from "vitest";
test("hidden", () => { console.error("UNREDACTED-SENTINEL"); expect("UNREDACTED-SENTINEL").toBe("safe"); });
`,
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: ["tests/__generated__/secret.derived.test.ts"],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "run",
  });
  expect(result.ok).toBe(false);
  expect(JSON.stringify(result.tests)).toContain("UNREDACTED-SENTINEL");
  expect(result.captured.stderr).toContain("UNREDACTED-SENTINEL");
});

test("captured streams are bounded with a deterministic warning", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "tests/__generated__/output.example.test.ts",
    managedTestSource(
      "example",
      `
import { test } from "vitest";
test("bounded output", () => { console.log("X".repeat(${MAX_CAPTURED_STREAM_BYTES + 1024})); });
`,
    ),
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: ["tests/__generated__/output.example.test.ts"],
    timeoutMs: 5_000,
    redactDerived: false,
    mode: "run",
  });
  expect(result.ok).toBe(true);
  expect(result.captured.stdout.length).toBeLessThan(
    MAX_CAPTURED_STREAM_BYTES + 100,
  );
  expect(result.captured.stdout).toContain(
    "[jaunt: captured output truncated]",
  );
  expect(result.diagnostics).toContainEqual(
    expect.objectContaining({
      code: "JAUNT_TS_RUNNER_OUTPUT_TRUNCATED",
      severity: "warning",
    }),
  );
});

test("runner input is rejected before buffering beyond its cap", async () => {
  async function* chunks(): AsyncIterable<Uint8Array> {
    yield Buffer.from("1234");
    yield Buffer.from("5678");
  }
  await expect(readBoundedInput(chunks(), 7)).rejects.toThrow(
    "runner input exceeds 7 bytes",
  );
});

test("typecheck mode can prove declaration emit without writing files", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const path = "src/ejected.ts";
  write(
    workspace.root,
    "tsconfig.emit.json",
    JSON.stringify({
      compilerOptions: {
        target: "ES2022",
        module: "NodeNext",
        moduleResolution: "NodeNext",
        strict: true,
      },
      files: [path],
    }),
  );
  const result = await runTestRunner({
    root: workspace.root,
    files: [path],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "typecheck",
    declarationEmit: true,
    tsconfigPath: "tsconfig.emit.json",
    overlays: { [path]: "export function answer(): number { return 42; }\n" },
    compilerModulePath: workspace.compilerModulePath,
  });
  expect(result.ok, JSON.stringify(result.diagnostics)).toBe(true);
  expect(result.emittedDeclarations).toContain("src/ejected.d.ts");
  expect(existsSync(resolve(workspace.root, "src/ejected.d.ts"))).toBe(false);
});
