import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import {
  MUTATION_PROTOCOL,
  runMutationProcess,
  type MutationStrengthResult,
} from "../../src/test/mutation.js";
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
    `import { expect, test } from "vitest";
import { isPositive } from "../src/contract.js";
test("positive boundary", () => {
  expect(isPositive(1)).toBe(true);
  expect(isPositive(0)).toBe(false);
  expect(isPositive(-1)).toBe(false);
});
`,
  );
  const source = resolve(workspace.root, "src/contract.ts");
  const before = readFileSync(source);
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
