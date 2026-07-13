import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import compiler from "@typescript/typescript6";
import { afterEach, expect, test } from "vitest";
import {
  generateMutationCases,
  runMutationProcess,
  runMutationStrength,
  type MutationExecutor,
  type MutationRunResult,
  type MutationStrengthInput,
} from "../../src/test/mutation.js";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

const CONTRACT = `/** @jauntContract */
export function clamp(value: number): number {
  if (value < 0) throw new RangeError("negative");
  if (value === 10) return 9;
  const enabled = true;
  return enabled ? value : 0;
}
`;

function setup(source = CONTRACT): MutationStrengthInput {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const sourcePath = "src/contract.ts";
  const batteryPath = "tests/contract.test.ts";
  for (const [path, content] of [
    [sourcePath, source],
    [batteryPath, "export {};\n"],
  ] as const) {
    const absolute = resolve(workspace.root, path);
    mkdirSync(dirname(absolute), { recursive: true });
    writeFileSync(absolute, content);
  }
  return {
    root: workspace.root,
    sourcePath,
    symbol: source.includes("class Empty") ? "Empty" : "clamp",
    batteryFiles: [batteryPath],
    overlays: {},
    tsconfigPath: "tsconfig.json",
    compilerModulePath: workspace.compilerModulePath,
    timeoutMs: 1_000,
    globalTimeoutMs: 10_000,
  };
}

function processResult(value: {
  compiled?: boolean;
  killed?: boolean;
  timedOut?: boolean;
}): MutationRunResult {
  return {
    exitCode: value.timedOut ? null : 0,
    timedOut: value.timedOut ?? false,
    stdout: JSON.stringify({
      compiled: value.compiled ?? true,
      killed: value.killed ?? true,
    }),
    stderr: "",
    outputTruncated: false,
  };
}

test("mutation cases cover stable useful operators without changing source", () => {
  const before = CONTRACT;
  const cases = generateMutationCases(
    compiler,
    "src/contract.ts",
    before,
    "clamp",
  );
  expect(new Set(cases.map((item) => item.kind))).toEqual(
    new Set(["return", "boolean", "comparison", "throw", "constant"]),
  );
  const ids = cases.map((item) => item.id);
  expect(ids).toEqual(ids.slice().sort());
  expect(CONTRACT).toBe(before);
});

test("mutation cases exclude private Error messages but retain the throw mutant", () => {
  const source = `/** @jauntContract */
export function parse(value: string): string {
  if (value === "") throw new TypeError("private wording");
  return value;
}
`;
  const cases = generateMutationCases(
    compiler,
    "src/contract.ts",
    source,
    "parse",
  );

  expect(cases.some((item) => item.kind === "throw")).toBe(true);
  expect(
    cases.some(
      (item) =>
        item.kind === "constant" && item.source.includes('TypeError("")'),
    ),
  ).toBe(false);
  expect(
    cases.some(
      (item) =>
        item.kind === "constant" &&
        item.source.includes('value === "__jaunt_mutant__"'),
    ),
  ).toBe(true);
});

test("strength reports killed and survived compiling mutants", async () => {
  const input = setup();
  const killed = await runMutationStrength(input, async () =>
    processResult({ killed: true }),
  );
  expect(killed.score).toMatchObject({
    killed: killed.score.applicable,
    survived: 0,
  });

  let calls = 0;
  const survivor: MutationExecutor = async () =>
    processResult({ killed: calls++ !== 0 });
  const weak = await runMutationStrength(input, survivor);
  expect(weak.survived).toHaveLength(1);
  expect(weak.survived[0]).toMatchObject({ outcome: "survived" });
  expect(weak.score.applicable).toBe(weak.score.killed + 1);
});

test("non-compiling mutants are strength-excluded from the denominator", async () => {
  const report = await runMutationStrength(setup(), async () =>
    processResult({ compiled: false, killed: false }),
  );
  expect(report.score.applicable).toBe(0);
  expect(report.score.excluded).toBeGreaterThan(0);
  expect(
    report.excluded.every((item) => item.reason === "did-not-compile"),
  ).toBe(true);
});

test("a timed-out mutant is killed and the committed source stays byte-identical", async () => {
  const input = setup();
  const source = resolve(input.root, input.sourcePath);
  const before = readFileSync(source);
  let calls = 0;
  const timeoutOnce: MutationExecutor = async () =>
    calls++ === 0
      ? processResult({ timedOut: true })
      : processResult({ killed: true });
  const report = await runMutationStrength(input, timeoutOnce);
  expect(report.killed.some((item) => item.reason === "timeout")).toBe(true);
  expect(readFileSync(source)).toEqual(before);
});

test("a global deadline marks the strength run incomplete", async () => {
  const input = { ...setup(), globalTimeoutMs: 10 };
  const report = await runMutationStrength(input, async () =>
    processResult({ timedOut: true }),
  );
  expect(report.complete).toBe(false);
  expect(report.killed).toHaveLength(0);
  expect(report.excluded.length).toBeGreaterThan(0);
});

test("targets with no safe mutable site are strength-excluded", async () => {
  const input = setup("/** @jauntContract */\nexport class Empty {}\n");
  const report = await runMutationStrength(input, async () => {
    throw new Error("no executor call expected");
  });
  expect(report.score).toEqual({
    killed: 0,
    applicable: 0,
    survived: 0,
    excluded: 1,
    ratio: null,
  });
  expect(report.excluded[0]?.reason).toBe("no-mutable-site");
});

test("class contracts mutate method behavior within the selected class", () => {
  const source = `/** @jauntContract */
export class Limit {
  reached(value: number): boolean { return value >= 3; }
}
`;
  const cases = generateMutationCases(
    compiler,
    "src/contract.ts",
    source,
    "Limit",
  );
  expect(new Set(cases.map((item) => item.kind))).toEqual(
    new Set(["return", "comparison", "constant"]),
  );
});

test("the process boundary permits bounded slow mutants and terminates runaways", async () => {
  const slow = await runMutationProcess(
    process.execPath,
    ["-e", 'setTimeout(() => process.stdout.write("valid"), 5_100)'],
    { cwd: process.cwd(), timeoutMs: 15_000 },
  );
  expect(slow).toMatchObject({ exitCode: 0, timedOut: false, stdout: "valid" });

  const runaway = await runMutationProcess(
    process.execPath,
    ["-e", "setInterval(() => {}, 1000)"],
    { cwd: process.cwd(), timeoutMs: 50 },
  );
  expect(runaway.timedOut).toBe(true);
}, 20_000);
