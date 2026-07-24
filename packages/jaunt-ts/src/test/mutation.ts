#!/usr/bin/env node
import { spawn } from "node:child_process";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type ts from "@typescript/typescript6";
import {
  assertLexicallyWithinRoot,
  assertWithinRoot,
} from "../analyzer/artifacts.js";
import { runTestRunner, type TestRunnerInput } from "./runner.js";

export const MUTATION_PROTOCOL = "jaunt-ts-mutation/1" as const;
export const DEFAULT_MAX_MUTANTS = 24;
export const MAX_MUTATION_INPUT_BYTES = 16 * 1024 * 1024;
const MAX_PROCESS_OUTPUT_BYTES = 4 * 1024 * 1024;
const activeMutationProcesses = new Set<ReturnType<typeof spawn>>();

export type MutationKind =
  "return" | "boolean" | "comparison" | "throw" | "constant";

export interface MutationCase {
  readonly id: string;
  readonly kind: MutationKind;
  readonly line: number;
  readonly column: number;
  readonly description: string;
  readonly source: string;
}

export interface MutationRecord {
  readonly id: string;
  readonly kind: MutationKind | "unsupported";
  readonly line: number;
  readonly column: number;
  readonly description: string;
  readonly outcome: "killed" | "survived" | "excluded";
  readonly reason?:
    | "test-failed"
    | "timeout"
    | "did-not-compile"
    | "runner-error"
    | "no-mutable-site";
}

export interface MutationStrengthInput {
  readonly root: string;
  readonly sourcePath: string;
  readonly symbol: string;
  readonly batteryFiles: readonly string[];
  readonly overlays?: Readonly<Record<string, string>>;
  readonly tsconfigPath: string;
  readonly vitestConfigPath?: string;
  readonly compilerModulePath: string;
  readonly timeoutMs: number;
  readonly globalTimeoutMs: number;
  readonly maxMutants?: number;
  readonly permissionSandbox?: boolean;
}

export interface MutationStrengthResult {
  readonly protocol: typeof MUTATION_PROTOCOL;
  readonly sourcePath: string;
  readonly symbol: string;
  readonly concurrency: 1;
  readonly complete: boolean;
  readonly killed: readonly MutationRecord[];
  readonly survived: readonly MutationRecord[];
  readonly excluded: readonly MutationRecord[];
  readonly score: {
    readonly killed: number;
    readonly applicable: number;
    readonly survived: number;
    readonly excluded: number;
    readonly ratio: number | null;
  };
}

export interface MutationRunResult {
  readonly exitCode: number | null;
  readonly timedOut: boolean;
  readonly stdout: string;
  readonly stderr: string;
  readonly outputTruncated: boolean;
}

interface MutationEdit {
  readonly kind: MutationKind;
  readonly start: number;
  readonly end: number;
  readonly replacement: string;
  readonly description: string;
}

interface SingleMutantInput {
  readonly runner: Omit<TestRunnerInput, "mode">;
}

interface SingleMutantOutput {
  readonly compiled: boolean;
  readonly killed: boolean;
}

export type MutationExecutor = (
  mutation: MutationCase,
  input: MutationStrengthInput,
  timeoutMs: number,
) => Promise<MutationRunResult>;

function appendBounded(
  chunks: Buffer[],
  chunk: Buffer,
  state: { bytes: number; truncated: boolean },
): void {
  const remaining = Math.max(0, MAX_PROCESS_OUTPUT_BYTES - state.bytes);
  if (remaining > 0) chunks.push(chunk.subarray(0, remaining));
  state.bytes += Math.min(remaining, chunk.byteLength);
  state.truncated ||= chunk.byteLength > remaining;
}

function killProcessGroup(child: ReturnType<typeof spawn>): void {
  if (child.pid === undefined || child.exitCode !== null) return;
  try {
    if (process.platform === "win32") {
      const killer = spawn(
        "taskkill",
        ["/PID", String(child.pid), "/T", "/F"],
        { stdio: "ignore", windowsHide: true },
      );
      killer.once("error", () => child.kill("SIGKILL"));
    } else {
      process.kill(-child.pid, "SIGKILL");
    }
  } catch {
    try {
      child.kill("SIGKILL");
    } catch {
      // The child already exited between the status check and signal.
    }
  }
}

export async function runMutationProcess(
  command: string,
  args: readonly string[],
  options: {
    readonly cwd: string;
    readonly timeoutMs: number;
    readonly stdin?: string;
  },
): Promise<MutationRunResult> {
  const child = spawn(command, [...args], {
    cwd: options.cwd,
    detached: process.platform !== "win32",
    stdio: ["pipe", "pipe", "pipe"],
    env: { ...process.env, CI: "1" },
  });
  activeMutationProcesses.add(child);
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  const stdoutState = { bytes: 0, truncated: false };
  const stderrState = { bytes: 0, truncated: false };
  child.stdout.on("data", (chunk: Buffer) =>
    appendBounded(stdout, chunk, stdoutState),
  );
  child.stderr.on("data", (chunk: Buffer) =>
    appendBounded(stderr, chunk, stderrState),
  );
  child.stdin.end(options.stdin ?? "");

  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    killProcessGroup(child);
  }, options.timeoutMs);
  const exitCode = await new Promise<number | null>((done) => {
    let settled = false;
    const finish = (code: number | null): void => {
      if (settled) return;
      settled = true;
      done(code);
    };
    child.once("exit", finish);
    child.once("error", () => finish(null));
  });
  clearTimeout(timer);
  activeMutationProcesses.delete(child);
  return {
    exitCode,
    timedOut,
    stdout: Buffer.concat(stdout).toString(),
    stderr: Buffer.concat(stderr).toString(),
    outputTruncated: stdoutState.truncated || stderrState.truncated,
  };
}

async function compilerAt(
  path: string,
): Promise<typeof import("@typescript/typescript6")> {
  const imported = (await import(pathToFileURL(resolve(path)).href)) as Record<
    string,
    unknown
  >;
  const compiler = (imported.default ?? imported) as Partial<
    typeof import("@typescript/typescript6")
  >;
  if (!compiler.createSourceFile || !compiler.ScriptTarget)
    throw new Error("compilerModulePath has no compatible TypeScript API");
  return compiler as typeof import("@typescript/typescript6");
}

function parseInput(value: unknown): MutationStrengthInput {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("mutation input must be an object");
  const input = value as Record<string, unknown>;
  for (const key of [
    "root",
    "sourcePath",
    "symbol",
    "tsconfigPath",
    "compilerModulePath",
  ]) {
    if (typeof input[key] !== "string")
      throw new Error(`${key} must be a string`);
  }
  if (
    !Array.isArray(input.batteryFiles) ||
    input.batteryFiles.some((file) => typeof file !== "string")
  ) {
    throw new Error("batteryFiles must be an array of strings");
  }
  for (const key of ["timeoutMs", "globalTimeoutMs"]) {
    if (
      typeof input[key] !== "number" ||
      !Number.isInteger(input[key]) ||
      input[key] < 1
    ) {
      throw new Error(`${key} must be a positive integer`);
    }
  }
  if (
    input.maxMutants !== undefined &&
    (typeof input.maxMutants !== "number" ||
      !Number.isInteger(input.maxMutants) ||
      input.maxMutants < 1 ||
      input.maxMutants > 256)
  ) {
    throw new Error("maxMutants must be an integer between 1 and 256");
  }
  if (
    input.overlays !== undefined &&
    (!input.overlays ||
      typeof input.overlays !== "object" ||
      Array.isArray(input.overlays) ||
      Object.values(input.overlays).some(
        (source) => typeof source !== "string",
      ))
  ) {
    throw new Error("overlays must map paths to strings");
  }
  if (
    input.vitestConfigPath !== undefined &&
    typeof input.vitestConfigPath !== "string"
  ) {
    throw new Error("vitestConfigPath must be a string");
  }
  if (
    input.permissionSandbox !== undefined &&
    typeof input.permissionSandbox !== "boolean"
  ) {
    throw new Error("permissionSandbox must be boolean");
  }

  const root = resolve(input.root as string);
  const paths = [
    input.sourcePath as string,
    input.tsconfigPath as string,
    ...(input.batteryFiles as string[]),
    ...Object.keys(
      (input.overlays as Record<string, string> | undefined) ?? {},
    ),
  ];
  if (typeof input.vitestConfigPath === "string")
    paths.push(input.vitestConfigPath);
  for (const path of paths) assertWithinRoot(root, resolve(root, path));
  assertLexicallyWithinRoot(root, resolve(input.compilerModulePath as string));
  return {
    root,
    sourcePath: input.sourcePath as string,
    symbol: input.symbol as string,
    batteryFiles: input.batteryFiles as string[],
    overlays: (input.overlays as Record<string, string> | undefined) ?? {},
    tsconfigPath: input.tsconfigPath as string,
    ...(typeof input.vitestConfigPath === "string"
      ? { vitestConfigPath: input.vitestConfigPath }
      : {}),
    compilerModulePath: input.compilerModulePath as string,
    timeoutMs: input.timeoutMs as number,
    globalTimeoutMs: input.globalTimeoutMs as number,
    ...(typeof input.maxMutants === "number"
      ? { maxMutants: input.maxMutants }
      : {}),
    ...(input.permissionSandbox === true ? { permissionSandbox: true } : {}),
  };
}

function mutantPermissionArgs(input: MutationStrengthInput): string[] {
  if (!input.permissionSandbox) return [];
  const args = process.execArgv.filter(
    (value) =>
      value === "--permission" ||
      value === "--experimental-permission" ||
      value === "--allow-addons" ||
      value === "--allow-worker" ||
      value.startsWith("--allow-fs-read=") ||
      value.startsWith("--allow-fs-write=") ||
      value.startsWith("--require="),
  );
  if (
    !args.some(
      (value) =>
        value === "--permission" || value === "--experimental-permission",
    ) ||
    !args.some((value) => value.startsWith("--allow-fs-read=")) ||
    !args.some((value) => value.startsWith("--allow-fs-write=")) ||
    !args.some((value) => value.startsWith("--require="))
  ) {
    throw new Error("mutation permission sandbox is incomplete");
  }
  // The trusted coordinator receives --allow-child-process so it can schedule
  // mutants. Generated batteries never inherit that escape hatch.
  return args;
}

function mutationRoot(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  symbol: string,
): ts.Node | undefined {
  const declarations = sourceFile.statements.filter(
    (statement): statement is ts.FunctionDeclaration | ts.ClassDeclaration =>
      (compiler.isFunctionDeclaration(statement) ||
        compiler.isClassDeclaration(statement)) &&
      statement.name?.text === symbol,
  );
  const concreteFunctions = declarations.filter(
    (declaration): declaration is ts.FunctionDeclaration =>
      compiler.isFunctionDeclaration(declaration) &&
      declaration.body !== undefined,
  );
  const classes = declarations.filter(compiler.isClassDeclaration);
  if (classes.length === 1 && concreteFunctions.length === 0) return classes[0];
  if (classes.length === 0 && concreteFunctions.length === 1)
    return concreteFunctions[0]!.body;
  return undefined;
}

function replacementForComparison(
  compiler: typeof import("@typescript/typescript6"),
  kind: ts.SyntaxKind,
): string | undefined {
  if (kind === compiler.SyntaxKind.LessThanToken) return "<=";
  if (kind === compiler.SyntaxKind.LessThanEqualsToken) return "<";
  if (kind === compiler.SyntaxKind.GreaterThanToken) return ">=";
  if (kind === compiler.SyntaxKind.GreaterThanEqualsToken) return ">";
  if (kind === compiler.SyntaxKind.EqualsEqualsToken) return "!==";
  if (kind === compiler.SyntaxKind.ExclamationEqualsToken) return "===";
  if (kind === compiler.SyntaxKind.EqualsEqualsEqualsToken) return "!==";
  if (kind === compiler.SyntaxKind.ExclamationEqualsEqualsToken) return "===";
  return undefined;
}

const BUILTIN_ERROR_CONSTRUCTORS = new Set([
  "Error",
  "EvalError",
  "RangeError",
  "ReferenceError",
  "SyntaxError",
  "TypeError",
  "URIError",
]);

function isPrivateErrorMessage(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.StringLiteral,
): boolean {
  const parent = node.parent;
  if (!compiler.isNewExpression(parent) || !parent.arguments) return false;
  const constructor = compiler.isIdentifier(parent.expression)
    ? parent.expression.text
    : undefined;
  const argument = parent.arguments.indexOf(node);
  if (constructor === "AggregateError") return argument === 1;
  return (
    constructor !== undefined &&
    BUILTIN_ERROR_CONSTRUCTORS.has(constructor) &&
    argument === 0
  );
}

/** Build a stable, bounded set of single-site mutants for one contract target. */
export function generateMutationCases(
  compiler: typeof import("@typescript/typescript6"),
  sourcePath: string,
  source: string,
  symbol: string,
  maxMutants = DEFAULT_MAX_MUTANTS,
): readonly MutationCase[] {
  const scriptKind = sourcePath.endsWith(".tsx")
    ? compiler.ScriptKind.TSX
    : compiler.ScriptKind.TS;
  const sourceFile = compiler.createSourceFile(
    sourcePath,
    source,
    compiler.ScriptTarget.Latest,
    true,
    scriptKind,
  );
  const root = mutationRoot(compiler, sourceFile, symbol);
  if (!root) return [];
  const edits: MutationEdit[] = [];
  const add = (
    kind: MutationKind,
    node: ts.Node,
    replacement: string,
    description: string,
  ): void => {
    const start = node.getStart(sourceFile);
    const end = node.getEnd();
    if (start < 0 || end <= start || source.slice(start, end) === replacement)
      return;
    edits.push({ kind, start, end, replacement, description });
  };

  const visit = (node: ts.Node): void => {
    if (compiler.isReturnStatement(node) && node.expression) {
      add(
        "return",
        node,
        "return (undefined as never);",
        "replace a returned value",
      );
    }
    if (compiler.isThrowStatement(node)) {
      add(
        "throw",
        node,
        "return (undefined as never);",
        "remove an expected throw",
      );
    }
    if (node.kind === compiler.SyntaxKind.TrueKeyword) {
      add("boolean", node, "false", "negate a boolean constant");
    } else if (node.kind === compiler.SyntaxKind.FalseKeyword) {
      add("boolean", node, "true", "negate a boolean constant");
    } else if (compiler.isNumericLiteral(node)) {
      add(
        "constant",
        node,
        Number(node.text) === 0 ? "1" : "0",
        "replace a numeric constant",
      );
    } else if (
      compiler.isStringLiteral(node) &&
      !isPrivateErrorMessage(compiler, node)
    ) {
      add(
        "constant",
        node,
        node.text.length === 0 ? '"__jaunt_mutant__"' : '""',
        "replace a string constant",
      );
    } else if (compiler.isBinaryExpression(node)) {
      const replacement = replacementForComparison(
        compiler,
        node.operatorToken.kind,
      );
      if (replacement) {
        add(
          "comparison",
          node.operatorToken,
          replacement,
          "change a comparison boundary",
        );
      }
    }
    compiler.forEachChild(node, visit);
  };
  visit(root);

  const deduplicated = new Map<string, MutationEdit>();
  for (const edit of edits) {
    deduplicated.set(
      `${edit.kind}:${edit.start}:${edit.end}:${edit.replacement}`,
      edit,
    );
  }
  return [...deduplicated.values()]
    .sort(
      (left, right) =>
        left.start - right.start ||
        left.end - right.end ||
        left.kind.localeCompare(right.kind) ||
        left.replacement.localeCompare(right.replacement),
    )
    .slice(0, maxMutants)
    .map((edit, index) => {
      const position = sourceFile.getLineAndCharacterOfPosition(edit.start);
      return {
        id: `${String(index + 1).padStart(3, "0")}:${edit.kind}:${position.line + 1}:${position.character + 1}`,
        kind: edit.kind,
        line: position.line + 1,
        column: position.character + 1,
        description: edit.description,
        source:
          source.slice(0, edit.start) +
          edit.replacement +
          source.slice(edit.end),
      };
    });
}

function record(
  mutation: MutationCase,
  outcome: MutationRecord["outcome"],
  reason?: MutationRecord["reason"],
): MutationRecord {
  return {
    id: mutation.id,
    kind: mutation.kind,
    line: mutation.line,
    column: mutation.column,
    description: mutation.description,
    outcome,
    ...(reason ? { reason } : {}),
  };
}

async function defaultExecutor(
  mutation: MutationCase,
  input: MutationStrengthInput,
  timeoutMs: number,
): Promise<MutationRunResult> {
  const runner: Omit<TestRunnerInput, "mode"> = {
    root: input.root,
    files: input.batteryFiles,
    timeoutMs,
    redactDerived: true,
    declarationEmit: false,
    tier: "derived",
    overlays: {
      ...input.overlays,
      [input.sourcePath]: mutation.source,
    },
    compilerModulePath: input.compilerModulePath,
    tsconfigPath: input.tsconfigPath,
    ...(input.vitestConfigPath
      ? { vitestConfigPath: input.vitestConfigPath }
      : {}),
    ...(input.permissionSandbox ? { permissionSandbox: true } : {}),
  };
  return runMutationProcess(
    process.execPath,
    [
      ...mutantPermissionArgs(input),
      fileURLToPath(import.meta.url),
      "--mutant",
    ],
    {
      cwd: input.root,
      timeoutMs,
      stdin: JSON.stringify({ runner } satisfies SingleMutantInput),
    },
  );
}

/** Execute every compiling mutant in a fresh process and return a deterministic score. */
export async function runMutationStrength(
  value: unknown,
  executor: MutationExecutor = defaultExecutor,
): Promise<MutationStrengthResult> {
  const input = parseInput(value);
  const compiler = await compilerAt(input.compilerModulePath);
  const source =
    input.overlays?.[input.sourcePath] ??
    readFileSync(resolve(input.root, input.sourcePath), "utf8");
  const mutations = generateMutationCases(
    compiler,
    input.sourcePath,
    source,
    input.symbol,
    input.maxMutants,
  );
  const killed: MutationRecord[] = [];
  const survived: MutationRecord[] = [];
  const excluded: MutationRecord[] = [];
  if (mutations.length === 0) {
    excluded.push({
      id: "000:unsupported:0:0",
      kind: "unsupported",
      line: 0,
      column: 0,
      description: "no safe mutable site was found",
      outcome: "excluded",
      reason: "no-mutable-site",
    });
  }

  const deadline = Date.now() + input.globalTimeoutMs;
  let complete = true;
  for (const [index, mutation] of mutations.entries()) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) {
      complete = false;
      for (const unrun of mutations.slice(index))
        excluded.push(record(unrun, "excluded", "runner-error"));
      break;
    }
    const globallyLimited = remaining < input.timeoutMs;
    const outcome = await executor(
      mutation,
      input,
      Math.max(1, Math.min(input.timeoutMs, remaining)),
    );
    if (outcome.timedOut) {
      if (globallyLimited) {
        complete = false;
        excluded.push(record(mutation, "excluded", "runner-error"));
        for (const unrun of mutations.slice(index + 1))
          excluded.push(record(unrun, "excluded", "runner-error"));
        break;
      }
      killed.push(record(mutation, "killed", "timeout"));
      continue;
    }
    if (outcome.exitCode !== 0 || outcome.outputTruncated) {
      complete = false;
      excluded.push(record(mutation, "excluded", "runner-error"));
      continue;
    }
    let decoded: SingleMutantOutput;
    try {
      decoded = JSON.parse(outcome.stdout) as SingleMutantOutput;
    } catch {
      complete = false;
      excluded.push(record(mutation, "excluded", "runner-error"));
      continue;
    }
    if (decoded.compiled !== true) {
      excluded.push(record(mutation, "excluded", "did-not-compile"));
    } else if (decoded.killed === true) {
      killed.push(record(mutation, "killed", "test-failed"));
    } else {
      survived.push(record(mutation, "survived"));
    }
  }
  const applicable = killed.length + survived.length;
  return {
    protocol: MUTATION_PROTOCOL,
    sourcePath: input.sourcePath,
    symbol: input.symbol,
    concurrency: 1,
    complete,
    killed,
    survived,
    excluded,
    score: {
      killed: killed.length,
      applicable,
      survived: survived.length,
      excluded: excluded.length,
      ratio: applicable === 0 ? null : killed.length / applicable,
    },
  };
}

async function readStdin(): Promise<string> {
  const chunks: Buffer[] = [];
  let bytes = 0;
  for await (const raw of process.stdin) {
    const chunk = Buffer.from(raw);
    bytes += chunk.byteLength;
    if (bytes > MAX_MUTATION_INPUT_BYTES)
      throw new Error(
        `mutation input exceeds ${MAX_MUTATION_INPUT_BYTES} bytes`,
      );
    chunks.push(chunk);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function runSingleMutant(value: unknown): Promise<SingleMutantOutput> {
  if (!value || typeof value !== "object" || !("runner" in value))
    throw new Error("mutant input must contain runner settings");
  const runner = (value as { runner: Omit<TestRunnerInput, "mode"> }).runner;
  const typed = await runTestRunner({ ...runner, mode: "typecheck" });
  if (!typed.ok) return { compiled: false, killed: false };
  const tested = await runTestRunner({ ...runner, mode: "run" });
  return { compiled: true, killed: !tested.ok };
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === resolve(fileURLToPath(import.meta.url))) {
  if (!process.argv.includes("--mutant")) {
    const terminate = (): never => {
      for (const child of activeMutationProcesses) killProcessGroup(child);
      process.exit(1);
    };
    process.once("SIGINT", terminate);
    process.once("SIGTERM", terminate);
  }
  void (async () => {
    try {
      const value = JSON.parse(await readStdin()) as unknown;
      const result = process.argv.includes("--mutant")
        ? await runSingleMutant(value)
        : await runMutationStrength(value);
      // Vitest marks the process unsuccessful when a mutant is killed. The
      // coordinator consumes that result as data, so a valid protocol response
      // must leave through the success path.
      process.exitCode = 0;
      process.stdout.write(`${JSON.stringify(result)}\n`);
    } catch (error) {
      process.stderr.write(
        `${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`,
      );
      process.exitCode = 1;
    }
  })();
}
