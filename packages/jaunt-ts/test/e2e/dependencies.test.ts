import { mkdirSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
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

function baseSpec(docs = "Normalize a string.", parameter = "string"): string {
  return `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** ${docs} */
export function base(value: ${parameter}): string { return jaunt.magic(); }
`;
}

function consumerSpec(
  dependency = "base",
  imported = "base",
  source = "../base/index.jaunt.js",
): string {
  return `import * as jaunt from "@usejaunt/ts/spec";
import { ${imported} } from "${source}";
jaunt.magicModule();
/** Create a slug. */
export function slugify(title: string): string {
  return jaunt.magic({ deps: [${dependency}] });
}
`;
}

async function createSession(
  workspace: FixtureWorkspace,
  projects: readonly string[] = ["tsconfig.json"],
  sourceRoots: readonly string[] = ["src"],
): Promise<AnalyzerSession> {
  return AnalyzerSession.create({
    root: workspace.root,
    projects,
    testProjects: [],
    sourceRoots,
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: workspace.compilerModulePath,
    clientVersion: "test",
    toolVersion: "test",
  });
}

test("same-project aliases resolve to stable dependency IDs without executing specs", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(workspace.root, "src/base/index.jaunt.ts", baseSpec());
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    consumerSpec("normalize", "base as normalize").replace(
      "jaunt.magicModule();",
      `import { base as availableToTheSpec } from "../base/index.jaunt.js";
jaunt.magicModule();`,
    ),
  );

  const session = await createSession(workspace);
  const workspaceAnalysis = session.analyzeWorkspace();
  expect(
    workspaceAnalysis.diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  const consumer = session
    .analyzeContracts()
    .modules.find((module) => module.moduleId === "ts:src/slug/index")!;
  expect(consumer.dependencies).toEqual(["ts:src/base/index#base"]);
  // Static value imports may provide authoring context without becoming an
  // implementation dependency. Candidate facade imports are checked later.
  expect(consumer.dependencies).not.toContain("availableToTheSpec");
  expect(consumer.symbols[0]!.options.deps).toEqual(["ts:src/base/index#base"]);
  expect(
    session
      .analyzeWorkspace({ moduleIds: ["ts:src/slug/index"] })
      .specs.map((spec) => spec.moduleId),
  ).toEqual(["ts:src/base/index", "ts:src/slug/index"]);
  expect(
    session
      .analyzeContracts({ moduleIds: ["ts:src/slug/index"] })
      .modules.map((module) => module.moduleId),
  ).toEqual(["ts:src/base/index", "ts:src/slug/index"]);
});

test("ordinary co-located tests use test provenance and retain import boundaries", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/native.test.ts",
    'import { describe } from "vitest";\nimport "./slug/index.jaunt.js";\ndescribe("native", () => {});\n',
  );

  const session = await createSession(workspace);

  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter(
        (item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE",
      ),
  ).toEqual([]);
  expect(session.analyzeWorkspace().diagnostics).toContainEqual(
    expect.objectContaining({
      code: "JAUNT_TS_RUNTIME_SPEC_IMPORT",
      path: "src/native.test.ts",
    }),
  );
});

test("same-package tsconfig path aliases are local provenance", async () => {
  const workspace = createFixtureWorkspace();
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
          baseUrl: ".",
          paths: { "@/*": ["src/*"] },
          ignoreDeprecations: "6.0",
          types: [],
        },
        include: ["src/**/*.ts"],
        exclude: ["src/**/*.jaunt.ts", "src/**/__generated__/**"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "src/helper.ts",
    "export const helper = (value: string): string => value.trim();\n",
  );
  write(
    workspace.root,
    "src/alias-consumer.ts",
    'import { helper } from "@/helper";\nexport const value = helper(" x ");\n',
  );

  const session = await createSession(workspace);
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.filter(
        (item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE",
      ),
  ).toEqual([]);
});

test("repeated provenance failures collapse with an occurrence count", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  for (const name of ["one", "two"]) {
    write(
      workspace.root,
      `src/${name}.ts`,
      'import missing from "undeclared-package";\nvoid missing;\n',
    );
  }

  const session = await createSession(workspace);
  const diagnostics = session
    .analyzeWorkspace()
    .diagnostics.filter((item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE");
  expect(diagnostics).toHaveLength(1);
  expect(diagnostics[0]!.message).toContain("2 occurrences");
});

test("targeted diagnostics keep the selected module's resolved import closure", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import type { Helper } from "../shared/helper.js";
jaunt.magicModule();
/** Normalize a helper value. */
export function base(value: Helper): string { return jaunt.magic(); }
`,
  );
  write(
    workspace.root,
    "src/shared/helper.ts",
    'import value from "selected-undeclared";\nvoid value;\nexport type Helper = string;\n',
  );
  write(
    workspace.root,
    "src/unrelated.ts",
    'import value from "unrelated-undeclared";\nvoid value;\n',
  );

  const session = await createSession(workspace);
  const targeted = session.analyzeWorkspace({
    moduleIds: ["ts:src/slug/index"],
  });
  const messages = targeted.diagnostics.map((item) => item.message);
  expect(
    messages.some((message) => message.includes("selected-undeclared")),
  ).toBe(true);
  expect(
    messages.some((message) => message.includes("unrelated-undeclared")),
  ).toBe(false);
});

test("dependency resolution rejects unknown and ambiguous identifiers", async () => {
  const unknownWorkspace = createFixtureWorkspace();
  roots.push(unknownWorkspace.root);
  write(unknownWorkspace.root, "src/base/index.jaunt.ts", baseSpec());
  write(
    unknownWorkspace.root,
    "src/slug/index.jaunt.ts",
    consumerSpec("missing", "base as missing"),
  );
  const unknown = await createSession(unknownWorkspace);
  expect(
    unknown
      .analyzeWorkspace()
      .diagnostics.some((item) => item.code === "JAUNT_TS_DEPENDENCY_UNKNOWN"),
  ).toBe(false);

  write(
    unknownWorkspace.root,
    "src/slug/index.jaunt.ts",
    consumerSpec("missing", "missing"),
  );
  const missingExport = await createSession(unknownWorkspace);
  expect(
    missingExport
      .analyzeWorkspace()
      .diagnostics.some((item) => item.code === "JAUNT_TS_DEPENDENCY_UNKNOWN"),
  ).toBe(true);

  const ambiguousWorkspace = createFixtureWorkspace();
  roots.push(ambiguousWorkspace.root);
  write(ambiguousWorkspace.root, "src/base/index.jaunt.ts", baseSpec());
  write(
    ambiguousWorkspace.root,
    "src/other/index.jaunt.ts",
    baseSpec().replaceAll("base", "other"),
  );
  write(
    ambiguousWorkspace.root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import { base as dependency } from "../base/index.jaunt.js";
import { other as dependency } from "../other/index.jaunt.js";
jaunt.magicModule();
/** Create a slug. */
export function slugify(title: string): string {
  return jaunt.magic({ deps: [dependency] });
}
`,
  );
  const ambiguous = await createSession(ambiguousWorkspace);
  expect(
    ambiguous
      .analyzeWorkspace()
      .diagnostics.some(
        (item) => item.code === "JAUNT_TS_DEPENDENCY_AMBIGUOUS",
      ),
  ).toBe(true);
});

test("dependency resolution rejects cycles", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/base/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
import { slugify } from "../slug/index.jaunt.js";
jaunt.magicModule();
/** Normalize a value. */
export function base(value: string): string {
  return jaunt.magic({ deps: [slugify] });
}
`,
  );
  write(workspace.root, "src/slug/index.jaunt.ts", consumerSpec());

  const session = await createSession(workspace);
  const cycles = session
    .analyzeWorkspace()
    .diagnostics.filter((item) => item.code === "JAUNT_TS_DEPENDENCY_CYCLE");
  expect(cycles).toHaveLength(1);
  expect(cycles[0]!.message).toContain("ts:src/base/index#base");
  expect(cycles[0]!.message).toContain("ts:src/slug/index#slugify");
});

test("dependency API changes stale consumers while generated body edits do not", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(workspace.root, "src/base/index.jaunt.ts", baseSpec());
  write(workspace.root, "src/slug/index.jaunt.ts", consumerSpec());

  const initial = await createSession(workspace);
  const initialModules = initial.analyzeContracts().modules;
  const initialBase = initialModules.find(
    (module) => module.moduleId === "ts:src/base/index",
  )!;
  const initialConsumer = initialModules.find(
    (module) => module.moduleId === "ts:src/slug/index",
  )!;

  write(
    workspace.root,
    "src/base/__generated__/index.ts",
    `// jaunt:state=built
export function __jaunt_impl_base(value: string): string {
  return value.trim();
}
`,
  );
  const bodyOnly = await createSession(workspace);
  const bodyOnlyConsumer = bodyOnly
    .analyzeContracts()
    .modules.find((module) => module.moduleId === "ts:src/slug/index")!;
  expect(bodyOnlyConsumer.structuralDigest).toBe(
    initialConsumer.structuralDigest,
  );
  expect(bodyOnlyConsumer.apiDigest).toBe(initialConsumer.apiDigest);

  // TSDoc is skipped by the imported-source scanner, so this assertion
  // specifically proves that the producer module API digest feeds the consumer.
  write(
    workspace.root,
    "src/base/index.jaunt.ts",
    baseSpec("Normalize a string and reject empty input."),
  );
  const proseEdit = await createSession(workspace);
  const proseModules = proseEdit.analyzeContracts().modules;
  const proseBase = proseModules.find(
    (module) => module.moduleId === "ts:src/base/index",
  )!;
  const proseConsumer = proseModules.find(
    (module) => module.moduleId === "ts:src/slug/index",
  )!;
  expect(proseBase.apiDigest).not.toBe(initialBase.apiDigest);
  expect(proseConsumer.structuralDigest).not.toBe(
    initialConsumer.structuralDigest,
  );
  expect(proseConsumer.apiDigest).not.toBe(initialConsumer.apiDigest);
});

test("dependencies cannot cross configured production projects", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
  for (const name of ["a", "b"]) {
    write(
      workspace.root,
      `packages/${name}/tsconfig.json`,
      `${JSON.stringify(
        {
          compilerOptions: {
            target: "ES2022",
            module: "NodeNext",
            moduleResolution: "NodeNext",
            strict: true,
            noEmit: true,
            types: [],
          },
          include: ["src/**/*.ts"],
          exclude: ["src/**/*.jaunt.ts", "src/**/__generated__/**"],
        },
        null,
        2,
      )}\n`,
    );
  }
  write(workspace.root, "packages/b/src/index.jaunt.ts", baseSpec());
  write(
    workspace.root,
    "packages/a/src/index.jaunt.ts",
    consumerSpec("base", "base", "../../b/src/index.jaunt.js"),
  );

  const session = await createSession(
    workspace,
    ["packages/a/tsconfig.json", "packages/b/tsconfig.json"],
    ["packages"],
  );
  expect(
    session
      .analyzeWorkspace()
      .diagnostics.some(
        (item) => item.code === "JAUNT_TS_DEPENDENCY_CROSS_PROJECT",
      ),
  ).toBe(true);
});

test("workspace path aliases require the importing package to declare the resolved sibling", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
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
          baseUrl: ".",
          paths: {
            "@workspace/base": ["packages/base/src/index.jaunt.ts"],
          },
          ignoreDeprecations: "6.0",
          types: [],
        },
        include: ["packages/**/*.ts"],
        exclude: ["packages/**/*.jaunt.ts", "packages/**/__generated__/**"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/base/package.json",
    `${JSON.stringify(
      { name: "@fixture/base", private: true, type: "module" },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/app/package.json",
    `${JSON.stringify(
      { name: "@fixture/app", private: true, type: "module" },
      null,
      2,
    )}\n`,
  );
  write(workspace.root, "packages/base/src/index.jaunt.ts", baseSpec());
  write(
    workspace.root,
    "packages/app/src/index.jaunt.ts",
    consumerSpec("base", "base", "@workspace/base"),
  );

  const undeclared = await createSession(
    workspace,
    ["tsconfig.json"],
    ["packages"],
  );
  const provenance = undeclared
    .analyzeWorkspace()
    .diagnostics.filter((item) => item.code === "JAUNT_TS_UNDECLARED_PACKAGE");
  expect(provenance).toHaveLength(1);
  expect(provenance[0]!.message).toContain('"@fixture/base"');
  expect(provenance[0]!.path).toBe("packages/app/src/index.jaunt.ts");

  write(
    workspace.root,
    "packages/app/package.json",
    `${JSON.stringify(
      {
        name: "@fixture/app",
        private: true,
        type: "module",
        dependencies: { "@fixture/base": "workspace:*" },
      },
      null,
      2,
    )}\n`,
  );
  const declared = await createSession(
    workspace,
    ["tsconfig.json"],
    ["packages"],
  );
  expect(
    declared
      .analyzeWorkspace()
      .diagnostics.filter((item) => item.severity === "error"),
  ).toEqual([]);
  expect(
    declared
      .analyzeContracts()
      .modules.find((module) => module.moduleId === "ts:packages/app/src/index")
      ?.dependencies,
  ).toEqual(["ts:packages/base/src/index#base"]);
});

test("generated candidates audit workspace aliases and relative cross-package imports", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  rmSync(resolve(workspace.root, "src"), { recursive: true, force: true });
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
          baseUrl: ".",
          paths: {
            "@workspace/base": ["packages/base/src/helper.ts"],
            "@fixture/base": ["packages/base/src/helper.ts"],
          },
          ignoreDeprecations: "6.0",
          types: [],
        },
        include: ["packages/**/*.ts"],
        exclude: ["packages/**/*.jaunt.ts", "packages/**/__generated__/**"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "packages/base/package.json",
    `${JSON.stringify(
      { name: "@fixture/base", private: true, type: "module" },
      null,
      2,
    )}\n`,
  );
  const appManifest = (declared: boolean): string =>
    `${JSON.stringify(
      {
        name: "@fixture/app",
        private: true,
        type: "module",
        imports: { "#base": "@fixture/base" },
        ...(declared
          ? { dependencies: { "@fixture/base": "workspace:*" } }
          : {}),
      },
      null,
      2,
    )}\n`;
  write(workspace.root, "packages/app/package.json", appManifest(false));
  write(
    workspace.root,
    "packages/base/src/helper.ts",
    "export const helper = (value: string): string => value.trim();\n",
  );
  write(
    workspace.root,
    "packages/app/src/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Normalize one value. */
export function value(input: string): string { return jaunt.magic(); }
`,
  );

  const candidates = [
    'import { helper } from "@workspace/base";',
    'import { helper } from "#base";',
    'import { helper } from "../../../base/src/helper.js";',
  ];
  const validate = async (declared: boolean) => {
    write(workspace.root, "packages/app/package.json", appManifest(declared));
    const session = await createSession(
      workspace,
      ["tsconfig.json"],
      ["packages"],
    );
    expect(
      session
        .analyzeWorkspace()
        .diagnostics.filter((item) => item.severity === "error"),
    ).toEqual([]);
    const contract = session
      .analyzeContracts()
      .modules.find(
        (module) => module.moduleId === "ts:packages/app/src/index",
      )!;
    const metadata = session.metadata();
    return candidates.map((importSource) =>
      session.validateOverlay({
        sessionId: metadata.sessionId,
        expectedEpoch: metadata.epoch,
        expectedSnapshot: metadata.snapshot,
        candidates: {
          [contract.moduleId]: `${importSource}
const __jaunt_impl_value = (input: string): string => helper(input);`,
        },
      }),
    );
  };

  for (const result of await validate(false)) {
    expect(result.valid).toBe(false);
    expect(result.artifacts).toEqual([]);
    expect(result.diagnostics).toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
    );
  }
  for (const result of await validate(true)) {
    expect(result.valid, JSON.stringify(result.diagnostics)).toBe(true);
    expect(result.diagnostics).not.toContainEqual(
      expect.objectContaining({ code: "JAUNT_TS_UNDECLARED_PACKAGE" }),
    );
  }
}, 30_000);
