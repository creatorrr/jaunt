import {
  mkdirSync,
  readFileSync,
  realpathSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import {
  MUTATION_PROTOCOL,
  runMutationProcess,
  type MutationStrengthResult,
} from "../../src/test/mutation.js";
import { runTestRunner } from "../../src/test/runner.js";
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

test("the built mutation coordinator kills useful mutants without writing source", async () => {
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
        },
        include: ["src/contract.ts", "tests/**/*.ts"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "src/contract.ts",
    `/** Return whether a value is strictly positive. @jauntContract */
export function isPositive(value: number): boolean {
  return value > 0;
}
`,
  );
  write(
    workspace.root,
    "tests/contract.derived.test.ts",
    `// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with \`jaunt reconcile\`.
// jaunt:property_scheme=jaunt-ts-property/2

import * as fc from "fast-check";
import { expect, test } from "vitest";
import { isPositive } from "../src/contract.js";

const valueArbitrary: fc.Arbitrary<number> = fc.constant(0);
test("@prop: strict positivity excludes zero", () => {
  fc.assert(
    fc.property(valueArbitrary, (value) => {
      expect(isPositive(value)).toBe(value > 0);
    }),
    { seed: 184493121, numRuns: 10 },
  );
});
`,
  );
  const source = resolve(workspace.root, "src/contract.ts");
  const before = readFileSync(source);
  const base = await runTestRunner({
    root: workspace.root,
    files: ["tests/contract.derived.test.ts"],
    timeoutMs: 5_000,
    redactDerived: true,
    mode: "run",
  });
  expect(base.ok, JSON.stringify(base)).toBe(true);
  expect(base.tests).toEqual([]); // Passing derived cases stay held out.
  const payload = {
    root: workspace.root,
    sourcePath: "src/contract.ts",
    symbol: "isPositive",
    batteryFiles: ["tests/contract.derived.test.ts"],
    overlays: {},
    tsconfigPath: "tsconfig.json",
    compilerModulePath: workspace.compilerModulePath,
    timeoutMs: 5_000,
    globalTimeoutMs: 30_000,
    // The unit matrix covers every operator; one real mutant is enough to prove
    // the disposable runner/process-group integration without loading CI hosts.
    maxMutants: 1,
  };
  const result = await runMutationProcess(
    process.execPath,
    [resolve(packageRoot, "dist/test/mutation.js")],
    {
      cwd: workspace.root,
      timeoutMs: 40_000,
      stdin: JSON.stringify(payload),
    },
  );

  expect(result).toMatchObject({ exitCode: 0, timedOut: false });
  const report = JSON.parse(result.stdout) as MutationStrengthResult;
  expect(report.protocol).toBe(MUTATION_PROTOCOL);
  expect(report.complete, result.stdout).toBe(true);
  expect(report.score.applicable).toBeGreaterThan(0);
  expect(report.score.survived).toBe(0);
  expect(report.score.killed).toBe(report.score.applicable);
  expect(readFileSync(source)).toEqual(before);
}, 45_000);

test("the built mutation coordinator runs through a filesystem alias", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  write(
    workspace.root,
    "src/empty.ts",
    "/** Empty contract with no mutable site. @jauntContract */\nexport class Empty {}\n",
  );
  const aliasRoot = `${workspace.root}-package-alias`;
  roots.push(aliasRoot);
  symlinkSync(
    packageRoot,
    aliasRoot,
    process.platform === "win32" ? "junction" : "dir",
  );
  const result = await runMutationProcess(
    process.execPath,
    [resolve(aliasRoot, "dist/test/mutation.js")],
    {
      cwd: workspace.root,
      timeoutMs: 10_000,
      stdin: JSON.stringify({
        root: workspace.root,
        sourcePath: "src/empty.ts",
        symbol: "Empty",
        batteryFiles: [],
        overlays: {},
        tsconfigPath: "tsconfig.json",
        compilerModulePath: workspace.compilerModulePath,
        timeoutMs: 1_000,
        globalTimeoutMs: 3_000,
        maxMutants: 1,
      }),
    },
  );

  const diagnostic = `${result.stdout}\n${result.stderr}`;
  expect(result.timedOut, diagnostic).toBe(false);
  expect(result.exitCode, diagnostic).toBe(0);
  expect(result.stdout, diagnostic).not.toBe("");
  const report = JSON.parse(result.stdout) as MutationStrengthResult;
  expect(report.protocol).toBe(MUTATION_PROTOCOL);
  expect(report.excluded).toEqual([
    expect.objectContaining({ outcome: "excluded", reason: "no-mutable-site" }),
  ]);
}, 15_000);

test("mutant batteries cannot read or write outside the protected workspace", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const sentinel = `${workspace.root}.sentinel`;
  roots.push(sentinel);
  writeFileSync(sentinel, "outside secret\n");
  write(workspace.root, "pnpm-workspace.yaml", "packages: []\n");
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
        },
        include: ["src/contract.ts", "src/read-outside.ts", "tests/**/*.ts"],
      },
      null,
      2,
    )}\n`,
  );
  write(
    workspace.root,
    "src/contract.ts",
    `/** Return whether a value is positive. @jauntContract */
export function isPositive(value: number): boolean {
  return value > 0;
}
`,
  );
  write(
    workspace.root,
    "src/read-outside.ts",
    `declare const process: {
  execPath: string;
  getBuiltinModule(name: "node:fs"): {
    readFileSync(path: string, encoding: string): string;
    writeFileSync(path: string, content: string): void;
  };
  getBuiltinModule(name: "node:child_process"): {
    spawnSync(command: string, args: readonly string[]): unknown;
  };
};

export function readOutside(path: string): string {
  return process.getBuiltinModule("node:fs").readFileSync(path, "utf8");
}

export function writeOutside(path: string): void {
  process.getBuiltinModule("node:fs").writeFileSync(path, "tampered\\n");
}

export function spawnOutside(): void {
  process
    .getBuiltinModule("node:child_process")
    .spawnSync(process.execPath, ["--version"]);
}
`,
  );
  write(
    workspace.root,
    "tests/contract.derived.test.ts",
    `import { expect, test } from "vitest";
import { readOutside, spawnOutside, writeOutside } from "../src/read-outside.js";

test("the mutation sandbox cannot read or write external files", () => {
  expect(() => readOutside(${JSON.stringify(sentinel)})).toThrow();
  expect(() => writeOutside(${JSON.stringify(sentinel)})).toThrow();
  expect(() => spawnOutside()).toThrow();
});
`,
  );
  const permissionFlag = process.allowedNodeEnvironmentFlags.has("--permission")
    ? "--permission"
    : "--experimental-permission";
  // macOS exposes temporary files under /var while permission checks use the
  // physical /private/var path. Keep the payload, cwd, and grants on one
  // canonical spelling, as the production isolated workspace does.
  const sandboxRoot = realpathSync(workspace.root);
  const sandboxPackageRoot = realpathSync(packageRoot);
  const permissionGuard = resolve(
    sandboxPackageRoot,
    "dist/test/permission_guard.cjs",
  );
  const payload = {
    root: sandboxRoot,
    sourcePath: "src/contract.ts",
    symbol: "isPositive",
    batteryFiles: ["tests/contract.derived.test.ts"],
    overlays: {},
    tsconfigPath: "tsconfig.json",
    compilerModulePath: resolve(
      sandboxRoot,
      "node_modules/typescript/lib/typescript.js",
    ),
    timeoutMs: 5_000,
    globalTimeoutMs: 30_000,
    maxMutants: 1,
    permissionSandbox: true,
  };
  const result = await runMutationProcess(
    process.execPath,
    [
      permissionFlag,
      "--allow-addons",
      "--allow-worker",
      "--allow-child-process",
      `--require=${permissionGuard}`,
      `--allow-fs-read=${sandboxRoot}`,
      `--allow-fs-read=${sandboxPackageRoot}`,
      `--allow-fs-write=${sandboxRoot}`,
      resolve(sandboxPackageRoot, "dist/test/mutation.js"),
    ],
    {
      cwd: sandboxRoot,
      timeoutMs: 40_000,
      stdin: JSON.stringify(payload),
    },
  );

  const diagnostic = `${result.stdout}\n${result.stderr}`;
  expect(result.timedOut, diagnostic).toBe(false);
  expect(result.exitCode, diagnostic).toBe(0);
  const report = JSON.parse(result.stdout) as MutationStrengthResult;
  expect(report.complete, `${result.stdout}\n${result.stderr}`).toBe(true);
  expect(
    report.score.applicable,
    `${result.stdout}\n${result.stderr}`,
  ).toBeGreaterThan(0);
  expect(report.score.killed).toBe(0);
  expect(report.score.survived).toBe(report.score.applicable);
  expect(readFileSync(sentinel, "utf8")).toBe("outside secret\n");
}, 45_000);
