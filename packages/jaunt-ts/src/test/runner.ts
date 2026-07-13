#!/usr/bin/env node
import {
  closeSync,
  existsSync,
  lstatSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  realpathSync,
  rmdirSync,
  unlinkSync,
} from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import type ts from "@typescript/typescript6";
import { startVitest } from "vitest/node";
import {
  assertLexicallyWithinRoot,
  assertWithinRoot,
} from "../analyzer/artifacts.js";
import { fromTypeScriptDiagnostic } from "../analyzer/diagnostics.js";
import { auditPackageImport } from "../analyzer/provenance.js";
import type { DiagnosticRecord } from "../analyzer/types.js";
import { HeldOutLeakError, HeldOutLeakGuard } from "./heldout.js";
import {
  JauntReporter,
  classifyTier,
  type FailureCategory,
  type TestResultRecord,
} from "./reporter.js";

export const RUNNER_PROTOCOL = "jaunt-ts-test-runner/1" as const;
export const MAX_RUNNER_INPUT_BYTES = 16 * 1024 * 1024;
export const MAX_CAPTURED_STREAM_BYTES = 64 * 1024;
const TRUNCATION_MARKER = "\n[jaunt: captured output truncated]\n";

export interface TestRunnerInput {
  readonly root: string;
  readonly files: readonly string[];
  readonly tsconfigPath?: string;
  readonly projectConfigPaths?: readonly string[];
  readonly vitestConfigPath?: string;
  readonly timeoutMs: number;
  readonly redactDerived: boolean;
  readonly mode: "typecheck" | "run";
  readonly tier?: "example" | "derived";
  readonly declarationEmit?: boolean;
  readonly normalEmit?: boolean;
  readonly deletedFiles?: readonly string[];
  readonly packageRoot?: string;
  readonly overlays?: Readonly<Record<string, string>>;
  readonly compilerModulePath?: string;
  readonly generatedDir?: string;
  readonly permissionSandbox?: boolean;
}

export interface TestRunnerOutput {
  readonly ok: boolean;
  readonly mode: "typecheck" | "run";
  readonly diagnostics: readonly DiagnosticRecord[];
  readonly tests: readonly RunnerTestResultRecord[];
  readonly captured: { readonly stdout: string; readonly stderr: string };
  readonly emittedDeclarations?: readonly string[];
  readonly emittedJavaScript?: readonly string[];
}

/** The entire public surface for one protected derived failure. */
export interface ProtectedDerivedTestResultRecord {
  readonly caseId: string;
  readonly category: FailureCategory;
}

export type RunnerTestResultRecord =
  TestResultRecord | ProtectedDerivedTestResultRecord;

export function projectTestResults(
  records: readonly TestResultRecord[],
  redactDerived: boolean,
): readonly RunnerTestResultRecord[] {
  if (!redactDerived) return records;
  return records.flatMap((record): readonly RunnerTestResultRecord[] => {
    if (record.tier === "example") return [record];
    if (record.status !== "failed") return [];
    if (record.caseId === undefined || record.category === undefined) {
      throw new HeldOutLeakError();
    }
    return [{ caseId: record.caseId, category: record.category }];
  });
}

export function redactedRunnerFailure(
  mode: "typecheck" | "run",
): TestRunnerOutput {
  return {
    ok: false,
    mode,
    diagnostics: [],
    tests: [
      {
        caseId: "opaque-runner-failure",
        category: "runner",
      },
    ],
    captured: { stdout: "", stderr: "" },
  };
}

function redactTypecheckResult(result: TestRunnerOutput): TestRunnerOutput {
  const guard = new HeldOutLeakGuard();
  const diagnostics = result.diagnostics.map((diagnostic) => {
    guard.observe(diagnostic);
    const projected: DiagnosticRecord = {
      code: diagnostic.code,
      severity: diagnostic.severity,
      message: "Protected TypeScript diagnostic",
      ...(diagnostic.path === undefined ? {} : { path: diagnostic.path }),
      ...(diagnostic.start === undefined ? {} : { start: diagnostic.start }),
      ...(diagnostic.end === undefined ? {} : { end: diagnostic.end }),
      ...(diagnostic.line === undefined ? {} : { line: diagnostic.line }),
      ...(diagnostic.column === undefined ? {} : { column: diagnostic.column }),
    };
    guard.allow(projected);
    return projected;
  });
  const protectedResult = { ...result, diagnostics };
  guard.assertSafe(protectedResult);
  return protectedResult;
}

function parseInput(value: unknown): TestRunnerInput {
  if (!value || typeof value !== "object" || Array.isArray(value))
    throw new Error("runner input must be an object");
  const input = value as Record<string, unknown>;
  if (typeof input.root !== "string") throw new Error("root must be a string");
  if (
    !Array.isArray(input.files) ||
    input.files.some((file) => typeof file !== "string")
  ) {
    throw new Error("files must be an array of strings");
  }
  if (input.mode !== "typecheck" && input.mode !== "run")
    throw new Error("mode must be typecheck or run");
  if (
    input.tier !== undefined &&
    input.tier !== "example" &&
    input.tier !== "derived"
  ) {
    throw new Error("tier must be example or derived");
  }
  if (
    typeof input.timeoutMs !== "number" ||
    !Number.isInteger(input.timeoutMs) ||
    input.timeoutMs < 1
  ) {
    throw new Error("timeoutMs must be a positive integer");
  }
  if (typeof input.redactDerived !== "boolean")
    throw new Error("redactDerived must be boolean");
  const overlays = input.overlays;
  if (
    overlays !== undefined &&
    (!overlays || typeof overlays !== "object" || Array.isArray(overlays))
  ) {
    throw new Error("overlays must be an object");
  }
  const root = resolve(input.root);
  const files = input.files as string[];
  for (const file of files) assertWithinRoot(root, resolve(root, file));
  if (overlays !== undefined) {
    for (const path of Object.keys(overlays))
      assertWithinRoot(root, resolve(root, path));
  }
  if (typeof input.configPath === "string") {
    assertWithinRoot(root, resolve(root, input.configPath));
  }
  if (typeof input.tsconfigPath === "string") {
    assertWithinRoot(root, resolve(root, input.tsconfigPath));
  }
  if (input.normalEmit !== undefined && typeof input.normalEmit !== "boolean") {
    throw new Error("normalEmit must be boolean");
  }
  if (
    input.deletedFiles !== undefined &&
    (!Array.isArray(input.deletedFiles) ||
      input.deletedFiles.some((path) => typeof path !== "string"))
  ) {
    throw new Error("deletedFiles must be an array of strings");
  }
  for (const path of (input.deletedFiles as string[] | undefined) ?? []) {
    assertWithinRoot(root, resolve(root, path));
  }
  if (
    input.packageRoot !== undefined &&
    typeof input.packageRoot !== "string"
  ) {
    throw new Error("packageRoot must be a string");
  }
  if (typeof input.packageRoot === "string") {
    assertWithinRoot(root, resolve(root, input.packageRoot));
  }
  if (
    input.projectConfigPaths !== undefined &&
    (!Array.isArray(input.projectConfigPaths) ||
      input.projectConfigPaths.some((path) => typeof path !== "string"))
  ) {
    throw new Error("projectConfigPaths must be an array of strings");
  }
  for (const path of (input.projectConfigPaths as string[] | undefined) ?? []) {
    assertWithinRoot(root, resolve(root, path));
  }
  if (typeof input.vitestConfigPath === "string") {
    assertWithinRoot(root, resolve(root, input.vitestConfigPath));
  }
  if (typeof input.compilerModulePath === "string") {
    assertLexicallyWithinRoot(root, resolve(input.compilerModulePath));
  }
  if (
    input.generatedDir !== undefined &&
    (typeof input.generatedDir !== "string" ||
      input.generatedDir.trim() === "" ||
      isAbsolute(input.generatedDir))
  ) {
    throw new Error("generatedDir must be a non-empty relative path");
  }
  if (typeof input.generatedDir === "string") {
    assertWithinRoot(root, resolve(root, input.generatedDir));
  }
  if (
    input.permissionSandbox !== undefined &&
    typeof input.permissionSandbox !== "boolean"
  ) {
    throw new Error("permissionSandbox must be boolean");
  }
  return {
    root,
    files,
    timeoutMs: input.timeoutMs,
    redactDerived: input.redactDerived,
    mode: input.mode,
    ...(input.tier === "example" || input.tier === "derived"
      ? { tier: input.tier }
      : {}),
    ...(input.declarationEmit === true ? { declarationEmit: true } : {}),
    ...(input.normalEmit === true ? { normalEmit: true } : {}),
    ...(Array.isArray(input.deletedFiles)
      ? { deletedFiles: input.deletedFiles as string[] }
      : {}),
    ...(typeof input.packageRoot === "string"
      ? { packageRoot: input.packageRoot }
      : {}),
    ...(typeof input.tsconfigPath === "string"
      ? { tsconfigPath: input.tsconfigPath }
      : {}),
    ...(Array.isArray(input.projectConfigPaths)
      ? { projectConfigPaths: input.projectConfigPaths as string[] }
      : {}),
    ...(typeof input.vitestConfigPath === "string"
      ? { vitestConfigPath: input.vitestConfigPath }
      : {}),
    ...(typeof input.compilerModulePath === "string"
      ? { compilerModulePath: input.compilerModulePath }
      : {}),
    ...(typeof input.generatedDir === "string"
      ? { generatedDir: input.generatedDir }
      : {}),
    ...(input.permissionSandbox === true ? { permissionSandbox: true } : {}),
    ...(overlays === undefined
      ? {}
      : { overlays: overlays as Record<string, string> }),
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
  if (!compiler.createProgram || !compiler.sys)
    throw new Error("compilerModulePath has no compatible API");
  return compiler as typeof import("@typescript/typescript6");
}

interface ResolutionProject {
  readonly configPath: string;
  readonly parsed: ts.ParsedCommandLine;
}

function isContainedBy(parent: string, candidate: string): boolean {
  const value = relative(resolve(parent), resolve(candidate));
  return (
    value === "" ||
    (!value.startsWith(`..${sep}`) && value !== ".." && !isAbsolute(value))
  );
}

function projectGraphForResolution(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  entries: readonly string[],
): readonly ResolutionProject[] {
  const pending = entries.map((entry) =>
    assertWithinRoot(root, resolve(root, entry)),
  );
  const projects = new Map<string, ResolutionProject>();
  const host: ts.ParseConfigFileHost = {
    ...compiler.sys,
    onUnRecoverableConfigFileDiagnostic(diagnostic) {
      throw new Error(
        compiler.flattenDiagnosticMessageText(diagnostic.messageText, "\n"),
      );
    },
  };
  while (pending.length > 0) {
    const configPath = pending.shift();
    if (!configPath || projects.has(configPath)) continue;
    const parsed = compiler.getParsedCommandLineOfConfigFile(
      configPath,
      {},
      host,
    );
    if (!parsed)
      throw new Error(`Unable to parse ${relative(root, configPath)}`);
    projects.set(configPath, { configPath, parsed });
    for (const reference of parsed.projectReferences ?? []) {
      pending.push(
        assertWithinRoot(root, compiler.resolveProjectReferencePath(reference)),
      );
    }
  }
  return [...projects.values()].sort(
    (left, right) =>
      dirname(right.configPath).length - dirname(left.configPath).length ||
      left.configPath.localeCompare(right.configPath),
  );
}

function ownerForRuntimeImport(
  projects: readonly ResolutionProject[],
  importer: string,
): ResolutionProject | undefined {
  const absolute = resolve(importer.split("?")[0]!);
  const exact = projects.filter((project) =>
    project.parsed.fileNames.some((file) => resolve(file) === absolute),
  );
  if (exact.length > 0) return exact[0];
  return projects.find((project) =>
    isContainedBy(dirname(project.configPath), absolute),
  );
}

async function projectSourceResolver(input: TestRunnerInput): Promise<
  | {
      readonly name: string;
      readonly enforce: "pre";
      resolveId(specifier: string, importer?: string): string | null;
    }
  | undefined
> {
  if (!input.compilerModulePath) return undefined;
  const entries = [
    ...(input.projectConfigPaths ?? []),
    ...(input.tsconfigPath ? [input.tsconfigPath] : []),
  ];
  if (entries.length === 0) return undefined;
  const compiler = await compilerAt(input.compilerModulePath);
  const projects = projectGraphForResolution(compiler, input.root, [
    ...new Set(entries),
  ]);
  const overlays = new Map(
    Object.entries(input.overlays ?? {}).map(([path, source]) => [
      resolve(input.root, path),
      source,
    ]),
  );
  const host: ts.ModuleResolutionHost = {
    fileExists: (path) =>
      overlays.has(resolve(path)) || compiler.sys.fileExists(path),
    readFile: (path) =>
      overlays.get(resolve(path)) ?? compiler.sys.readFile(path),
    directoryExists: compiler.sys.directoryExists,
    getDirectories: compiler.sys.getDirectories,
    ...(compiler.sys.realpath ? { realpath: compiler.sys.realpath } : {}),
  };
  return {
    name: "jaunt-typescript-project-source-resolution",
    enforce: "pre",
    resolveId(specifier, importer) {
      if (!importer || specifier.startsWith("\0")) return null;
      const owner = ownerForRuntimeImport(projects, importer);
      if (!owner) return null;
      const resolution = compiler.resolveModuleName(
        specifier,
        resolve(importer.split("?")[0]!),
        owner.parsed.options,
        host,
      ).resolvedModule;
      if (!resolution) return null;
      const resolved = resolve(resolution.resolvedFileName);
      const workspaceRelative = relative(input.root, resolved);
      if (
        !isContainedBy(input.root, resolved) ||
        workspaceRelative.split(sep).includes("node_modules") ||
        /\.d\.(?:ts|mts|cts)$/.test(resolved)
      ) {
        return null;
      }
      const sourceOwner = ownerForRuntimeImport(projects, resolved);
      if (!sourceOwner) return null;
      return resolved;
    },
  };
}

function unwrappedRequireExpression(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
): boolean {
  let current = expression;
  while (
    compiler.isParenthesizedExpression(current) ||
    compiler.isNonNullExpression(current) ||
    compiler.isAsExpression(current) ||
    compiler.isTypeAssertionExpression(current)
  ) {
    current = current.expression;
  }
  if (compiler.isIdentifier(current)) return current.text === "require";
  return (
    compiler.isPropertyAccessExpression(current) &&
    ((current.name.text === "resolve" &&
      unwrappedRequireExpression(compiler, current.expression)) ||
      current.name.text === "require")
  );
}

const FORBIDDEN_DYNAMIC_BINDINGS = new Set([
  "Bun",
  "Deno",
  "Function",
  "Proxy",
  "SharedWorker",
  "WebAssembly",
  "Worker",
  "_linkedBinding",
  "binding",
  "createRequire",
  "dlopen",
  "eval",
  "getBuiltinModule",
  "global",
  "globalThis",
  "process",
  "require",
]);

const FORBIDDEN_DYNAMIC_PROPERTIES = new Set([
  ...FORBIDDEN_DYNAMIC_BINDINGS,
  "__proto__",
  "__lookupGetter__",
  "__lookupSetter__",
  "constructor",
  "defineProperties",
  "defineProperty",
  "getOwnPropertyDescriptor",
  "getOwnPropertyDescriptors",
  "getPrototypeOf",
  "setPrototypeOf",
]);

const FORBIDDEN_DYNAMIC_MODULES = new Set([
  "child_process",
  "cluster",
  "module",
  "node:child_process",
  "node:cluster",
  "node:module",
  "node:process",
  "node:repl",
  "node:vm",
  "node:worker_threads",
  "process",
  "repl",
  "vm",
  "worker_threads",
]);

function tripleSlashDirectiveRanges(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly { readonly pos: number; readonly end: number }[] {
  const ranges: { pos: number; end: number }[] = [];
  const scanner = compiler.createScanner(
    compiler.ScriptTarget.Latest,
    false,
    compiler.LanguageVariant.Standard,
    sourceFile.text,
  );
  const directive =
    /^\/\/\/[\t\v\f \u0085\u00a0\u1680\u2000-\u200b\u202f\u205f\u3000\ufeff]*<(?:reference|amd-(?:dependency|module))\b/iu;
  for (
    let token = scanner.scan();
    token !== compiler.SyntaxKind.EndOfFileToken;
    token = scanner.scan()
  ) {
    if (
      token !== compiler.SyntaxKind.SingleLineCommentTrivia ||
      !directive.test(scanner.getTokenText())
    ) {
      continue;
    }
    ranges.push({ pos: scanner.getTokenPos(), end: scanner.getTextPos() });
  }
  return ranges;
}

function staticallyKnownString(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
  depth = 0,
): string | undefined {
  if (depth > 16) return undefined;
  let current = expression;
  while (
    compiler.isParenthesizedExpression(current) ||
    compiler.isNonNullExpression(current) ||
    compiler.isAsExpression(current) ||
    compiler.isTypeAssertionExpression(current)
  ) {
    current = current.expression;
  }
  if (compiler.isStringLiteralLike(current)) return current.text;
  if (
    compiler.isBinaryExpression(current) &&
    current.operatorToken.kind === compiler.SyntaxKind.PlusToken
  ) {
    const left = staticallyKnownString(compiler, current.left, depth + 1);
    const right = staticallyKnownString(compiler, current.right, depth + 1);
    if (left === undefined || right === undefined) return undefined;
    const joined = left + right;
    return joined.length <= 256 ? joined : undefined;
  }
  if (compiler.isTemplateExpression(current)) {
    let value = current.head.text;
    for (const span of current.templateSpans) {
      const expressionValue = staticallyKnownString(
        compiler,
        span.expression,
        depth + 1,
      );
      if (expressionValue === undefined) return undefined;
      value += expressionValue + span.literal.text;
      if (value.length > 256) return undefined;
    }
    return value;
  }
  if (
    compiler.isCallExpression(current) &&
    compiler.isPropertyAccessExpression(current.expression)
  ) {
    const receiver = current.expression.expression;
    if (
      current.expression.name.text === "join" &&
      compiler.isArrayLiteralExpression(receiver) &&
      current.arguments.length <= 1
    ) {
      const separator = current.arguments[0]
        ? staticallyKnownString(compiler, current.arguments[0], depth + 1)
        : ",";
      if (separator === undefined) return undefined;
      const values = receiver.elements.map((element) =>
        compiler.isSpreadElement(element)
          ? undefined
          : staticallyKnownString(compiler, element, depth + 1),
      );
      if (values.some((value) => value === undefined)) return undefined;
      const joined = (values as string[]).join(separator);
      return joined.length <= 256 ? joined : undefined;
    }
    if (current.expression.name.text === "concat") {
      const first = staticallyKnownString(compiler, receiver, depth + 1);
      const rest = current.arguments.map((argument) =>
        staticallyKnownString(compiler, argument, depth + 1),
      );
      if (first === undefined || rest.some((value) => value === undefined))
        return undefined;
      const joined = first + (rest as string[]).join("");
      return joined.length <= 256 ? joined : undefined;
    }
  }
  return undefined;
}

function staticallyKnownPropertyName(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
): string | undefined {
  // Only syntax can establish a safe property name. Checker types are not
  // evidence about runtime values because generated tests may use assertions
  // to claim that an opaque string is a harmless literal.
  return staticallyKnownString(compiler, expression);
}

function staticallySafePropertyKey(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
): boolean {
  if (staticallyKnownPropertyName(compiler, expression) !== undefined) {
    return true;
  }
  let current = expression;
  while (
    compiler.isParenthesizedExpression(current) ||
    compiler.isNonNullExpression(current) ||
    compiler.isAsExpression(current) ||
    compiler.isTypeAssertionExpression(current) ||
    compiler.isSatisfiesExpression(current)
  ) {
    current = current.expression;
  }
  if (compiler.isNumericLiteral(current) || compiler.isBigIntLiteral(current)) {
    return true;
  }
  return (
    compiler.isPrefixUnaryExpression(current) &&
    (current.operator === compiler.SyntaxKind.PlusToken ||
      current.operator === compiler.SyntaxKind.MinusToken) &&
    compiler.isNumericLiteral(current.operand)
  );
}

function directReflectCall(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Identifier,
):
  { readonly call: ts.CallExpression; readonly operation: string } | undefined {
  const access = node.parent;
  if (!(
    (compiler.isPropertyAccessExpression(access) &&
      access.expression === node) ||
    (compiler.isElementAccessExpression(access) && access.expression === node)
  )) {
    return undefined;
  }
  const operation = compiler.isPropertyAccessExpression(access)
    ? access.name.text
    : access.argumentExpression
      ? staticallyKnownPropertyName(compiler, access.argumentExpression)
      : undefined;
  if (operation === undefined) return undefined;
  let outer: ts.Expression = access;
  while (
    compiler.isParenthesizedExpression(outer.parent) ||
    compiler.isNonNullExpression(outer.parent) ||
    compiler.isAsExpression(outer.parent) ||
    compiler.isTypeAssertionExpression(outer.parent) ||
    compiler.isSatisfiesExpression(outer.parent)
  ) {
    outer = outer.parent;
  }
  const parent = outer.parent;
  if (!compiler.isCallExpression(parent) || parent.expression !== outer)
    return undefined;
  return { call: parent, operation };
}

const FORBIDDEN_REFLECT_OPERATIONS = new Set(["apply", "construct"]);

function runtimeModuleReference(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): boolean {
  if (compiler.isImportDeclaration(node)) {
    const clause = node.importClause;
    if (!clause) return true;
    if (clause.isTypeOnly) return false;
    if (clause.name) return true;
    const bindings = clause.namedBindings;
    if (!bindings || compiler.isNamespaceImport(bindings)) return true;
    return bindings.elements.some((element) => !element.isTypeOnly);
  }
  if (compiler.isExportDeclaration(node)) {
    if (node.isTypeOnly) return false;
    const clause = node.exportClause;
    if (!clause || compiler.isNamespaceExport(clause)) return true;
    return clause.elements.some((element) => !element.isTypeOnly);
  }
  return (
    compiler.isImportEqualsDeclaration(node) || compiler.isCallExpression(node)
  );
}

function forbiddenDynamicExecutionReference(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): ts.Node | undefined {
  // Generated batteries have one intentionally narrow loading surface: static
  // ESM imports.  Reject the primitives themselves instead of attempting
  // unbounded alias/data-flow recovery for CommonJS and dynamic code.  That
  // makes `const load = require`, `require.call`, computed module.require,
  // createRequire aliases, eval, and Function construction fail closed at the
  // first reference.
  if (
    compiler.isIdentifier(node) &&
    FORBIDDEN_DYNAMIC_BINDINGS.has(node.text)
  ) {
    return node;
  }
  if (compiler.isIdentifier(node) && node.text === "Reflect") {
    const direct = directReflectCall(compiler, node);
    if (!direct) return node;
    if (FORBIDDEN_REFLECT_OPERATIONS.has(direct.operation)) {
      return direct.call.expression;
    }
    if (direct.operation === "get") {
      const property = direct.call.arguments[1];
      if (
        !property ||
        !staticallySafePropertyKey(compiler, property) ||
        FORBIDDEN_DYNAMIC_PROPERTIES.has(
          staticallyKnownPropertyName(compiler, property) ?? "",
        )
      ) {
        return property ?? direct.call;
      }
    }
  }
  if (
    compiler.isElementAccessExpression(node) &&
    node.argumentExpression &&
    FORBIDDEN_DYNAMIC_PROPERTIES.has(
      staticallyKnownPropertyName(compiler, node.argumentExpression) ?? "",
    )
  ) {
    return node.argumentExpression;
  }
  if (
    compiler.isElementAccessExpression(node) &&
    node.argumentExpression &&
    !staticallySafePropertyKey(compiler, node.argumentExpression)
  ) {
    return node.argumentExpression;
  }
  if (
    compiler.isPropertyAccessExpression(node) &&
    FORBIDDEN_DYNAMIC_PROPERTIES.has(node.name.text)
  ) {
    return node.name;
  }
  if (compiler.isBindingElement(node)) {
    const property =
      node.propertyName ??
      (compiler.isIdentifier(node.name) ? node.name : undefined);
    const name = property
      ? compiler.isComputedPropertyName(property)
        ? staticallyKnownPropertyName(compiler, property.expression)
        : compiler.isIdentifier(property) ||
            compiler.isStringLiteralLike(property)
          ? property.text
          : undefined
      : undefined;
    if (name && FORBIDDEN_DYNAMIC_PROPERTIES.has(name)) return property;
    if (
      property &&
      compiler.isComputedPropertyName(property) &&
      !staticallySafePropertyKey(compiler, property.expression)
    ) {
      return property;
    }
  }
  if (
    compiler.isCallExpression(node) &&
    node.expression.kind === compiler.SyntaxKind.ImportKeyword
  ) {
    return node.expression;
  }
  if (compiler.isImportEqualsDeclaration(node)) return node.moduleReference;
  return undefined;
}

function staticModuleReference(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): ts.StringLiteralLike | undefined {
  if (
    (compiler.isImportDeclaration(node) ||
      compiler.isExportDeclaration(node)) &&
    node.moduleSpecifier &&
    compiler.isStringLiteralLike(node.moduleSpecifier)
  ) {
    return node.moduleSpecifier;
  }
  if (
    compiler.isImportEqualsDeclaration(node) &&
    compiler.isExternalModuleReference(node.moduleReference) &&
    node.moduleReference.expression &&
    compiler.isStringLiteralLike(node.moduleReference.expression)
  ) {
    return node.moduleReference.expression;
  }
  if (
    compiler.isImportTypeNode(node) &&
    compiler.isLiteralTypeNode(node.argument) &&
    compiler.isStringLiteralLike(node.argument.literal)
  ) {
    return node.argument.literal;
  }
  if (
    compiler.isCallExpression(node) &&
    (node.expression.kind === compiler.SyntaxKind.ImportKeyword ||
      unwrappedRequireExpression(compiler, node.expression)) &&
    node.arguments.length >= 1 &&
    compiler.isStringLiteralLike(node.arguments[0]!)
  ) {
    return node.arguments[0] as ts.StringLiteralLike;
  }
  return undefined;
}

function normalizedModuleSegments(value: string): readonly string[] {
  let decoded = value;
  try {
    decoded = decodeURIComponent(value);
  } catch {
    // Invalid percent escapes remain literal and cannot hide a path segment.
  }
  return decoded
    .replaceAll("\\", "/")
    .split(/[?#]/, 1)[0]!
    .split("/")
    .filter(Boolean);
}

function containsSequence(
  values: readonly string[],
  sequence: readonly string[],
): boolean {
  if (sequence.length === 0 || values.length < sequence.length) return false;
  for (let index = 0; index <= values.length - sequence.length; index += 1) {
    if (sequence.every((value, offset) => values[index + offset] === value))
      return true;
  }
  return false;
}

function isPrivateTestModule(
  input: TestRunnerInput,
  specifier: string,
  resolvedPath?: string,
): boolean {
  const generated = normalizedModuleSegments(
    input.generatedDir ?? "__generated__",
  );
  const candidates: readonly {
    readonly segments: readonly string[];
    readonly generatedPath: boolean;
  }[] = [
    {
      segments: normalizedModuleSegments(specifier),
      generatedPath:
        specifier.startsWith(".") ||
        specifier.startsWith("/") ||
        specifier.startsWith("\\") ||
        specifier.startsWith("file:"),
    },
    ...(resolvedPath && isContainedBy(input.root, resolvedPath)
      ? [
          {
            segments: normalizedModuleSegments(
              relative(input.root, resolvedPath),
            ),
            generatedPath: true,
          },
        ]
      : []),
  ];
  return candidates.some(({ segments, generatedPath }) => {
    const file = segments.at(-1) ?? "";
    return (
      (generatedPath &&
        (containsSequence(segments, ["__generated__"]) ||
          containsSequence(segments, generated))) ||
      /\.jaunt(?:-test)?(?:\.(?:[cm]?[jt]s|[jt]sx))?$/.test(file)
    );
  });
}

function testModulePolicyDiagnostics(
  compiler: typeof import("@typescript/typescript6"),
  input: TestRunnerInput,
  program: ts.Program,
  host: ts.CompilerHost,
  options: ts.CompilerOptions,
): readonly DiagnosticRecord[] {
  const diagnostics: DiagnosticRecord[] = [];
  for (const relativePath of input.files) {
    const sourceFile = program.getSourceFile(resolve(input.root, relativePath));
    if (!sourceFile) continue;
    const generatedBattery =
      /\.(?:example|derived|contract)\.test\.(?:ts|tsx)$/.test(relativePath);
    function diagnostic(
      code: string,
      message: string,
      node: ts.Node,
    ): DiagnosticRecord {
      const start = node.getStart(sourceFile!);
      const position = sourceFile!.getLineAndCharacterOfPosition(start);
      return {
        code,
        severity: "error",
        message,
        path: relativePath.replaceAll("\\", "/"),
        start,
        end: node.getEnd(),
        line: position.line + 1,
        column: position.character + 1,
      };
    }
    function directiveDiagnostic(
      code: string,
      message: string,
      directive: { readonly pos: number; readonly end: number },
    ): DiagnosticRecord {
      const start = Math.max(0, directive.pos);
      const end = Math.max(start, directive.end);
      const position = sourceFile!.getLineAndCharacterOfPosition(start);
      return {
        code,
        severity: "error",
        message,
        path: relativePath.replaceAll("\\", "/"),
        start,
        end,
        line: position.line + 1,
        column: position.character + 1,
      };
    }
    if (generatedBattery) {
      for (const reference of sourceFile.typeReferenceDirectives) {
        const provenance = auditPackageImport(
          input.root,
          sourceFile.fileName,
          reference.fileName,
          true,
          undefined,
          false,
        );
        if (provenance) {
          diagnostics.push(
            directiveDiagnostic(provenance.code, provenance.message, reference),
          );
        }
      }
      for (const directive of tripleSlashDirectiveRanges(
        compiler,
        sourceFile,
      )) {
        diagnostics.push(
          directiveDiagnostic(
            "JAUNT_TS_TEST_REFERENCE_DIRECTIVE",
            "Generated tests may not use triple-slash or AMD reference directives; use static ESM imports and the configured TypeScript project",
            directive,
          ),
        );
      }
    }
    function visit(node: ts.Node): void {
      const forbidden = forbiddenDynamicExecutionReference(compiler, node);
      if (forbidden) {
        diagnostics.push(
          diagnostic(
            "JAUNT_TS_TEST_DYNAMIC_LOADER",
            "Generated tests may use static ESM imports only; CommonJS loaders and dynamic code are forbidden",
            forbidden,
          ),
        );
      }
      const literal = staticModuleReference(compiler, node);
      if (literal) {
        if (
          generatedBattery &&
          runtimeModuleReference(compiler, node) &&
          FORBIDDEN_DYNAMIC_MODULES.has(literal.text)
        ) {
          diagnostics.push(
            diagnostic(
              "JAUNT_TS_TEST_DYNAMIC_LOADER",
              "Generated tests may not import Node dynamic-execution or process-loader modules",
              literal,
            ),
          );
        }
        const resolution = compiler.resolveModuleName(
          literal.text,
          sourceFile!.fileName,
          options,
          host,
        ).resolvedModule;
        const resolvedPath = resolution?.resolvedFileName;
        if (isPrivateTestModule(input, literal.text, resolvedPath)) {
          diagnostics.push(
            diagnostic(
              "JAUNT_TS_TEST_PRIVATE_IMPORT",
              "Generated tests must import the public facade, not a private spec or generated artifact",
              literal,
            ),
          );
        }
        if (generatedBattery) {
          const workspaceRelative = resolvedPath
            ? relative(input.root, resolve(resolvedPath))
            : undefined;
          const resolvedWorkspaceFile =
            resolvedPath &&
            isContainedBy(input.root, resolvedPath) &&
            !workspaceRelative?.split(sep).includes("node_modules")
              ? resolve(resolvedPath)
              : undefined;
          const packageResolution = resolvedWorkspaceFile
            ? { resolvedWorkspaceFile }
            : undefined;
          const provenance = auditPackageImport(
            input.root,
            sourceFile!.fileName,
            literal.text,
            true,
            packageResolution,
            false,
          );
          if (provenance) {
            diagnostics.push(
              diagnostic(provenance.code, provenance.message, literal),
            );
          }
        }
      }
      compiler.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  return diagnostics;
}

async function typecheck(input: TestRunnerInput): Promise<TestRunnerOutput> {
  if (!input.compilerModulePath)
    throw new Error("compilerModulePath is required for typecheck mode");
  const compiler = await compilerAt(input.compilerModulePath);
  const configPath = resolve(input.root, input.tsconfigPath ?? "tsconfig.json");
  const config = compiler.readConfigFile(configPath, compiler.sys.readFile);
  if (config.error)
    throw new Error(
      compiler.flattenDiagnosticMessageText(config.error.messageText, "\n"),
    );
  const parsed = compiler.parseJsonConfigFileContent(
    config.config,
    compiler.sys,
    dirname(configPath),
    {},
    configPath,
  );
  const overlays = new Map(
    Object.entries(input.overlays ?? {}).map(([path, source]) => [
      resolve(input.root, path),
      source,
    ]),
  );
  const deleted = new Set(
    (input.deletedFiles ?? []).map((path) => resolve(input.root, path)),
  );
  const emitRequested =
    input.declarationEmit === true || input.normalEmit === true;
  const options: ts.CompilerOptions = {
    ...parsed.options,
    noEmit: !emitRequested,
    strict: true,
    noImplicitAny: true,
    ...(emitRequested
      ? {
          declaration: true,
          declarationMap: false,
          emitDeclarationOnly: input.normalEmit !== true,
          incremental: false,
          composite: false,
          noEmitOnError: true,
          sourceMap: false,
        }
      : {}),
  };
  const base = compiler.createCompilerHost(options, true);
  const host: ts.CompilerHost = {
    ...base,
    fileExists: (path) =>
      !deleted.has(resolve(path)) &&
      (overlays.has(resolve(path)) || base.fileExists(path)),
    readFile: (path) =>
      deleted.has(resolve(path))
        ? undefined
        : (overlays.get(resolve(path)) ?? base.readFile(path)),
    getSourceFile: (path, target, onError, createNew) => {
      if (deleted.has(resolve(path))) return undefined;
      const source = overlays.get(resolve(path));
      return source === undefined
        ? base.getSourceFile(path, target, onError, createNew)
        : compiler.createSourceFile(
            path,
            source,
            target,
            true,
            path.endsWith(".tsx")
              ? compiler.ScriptKind.TSX
              : compiler.ScriptKind.TS,
          );
    },
  };
  const roots = [
    ...parsed.fileNames,
    ...input.files.map((path) => resolve(input.root, path)),
    ...(input.normalEmit === true ? [] : overlays.keys()),
  ].filter((path) => !deleted.has(resolve(path)));
  const program = compiler.createProgram({
    rootNames: [...new Set(roots)],
    options,
    host,
  });
  const emittedFiles = new Map<string, string>();
  const emittedDeclarations: string[] = [];
  const emittedJavaScript: string[] = [];
  const emitDiagnostics: DiagnosticRecord[] = [];
  const packageRoot = resolve(input.root, input.packageRoot ?? ".");
  const emit = emitRequested
    ? program.emit(undefined, (path, source) => {
        const absolute = resolve(path);
        const workspaceRelative = relative(input.root, absolute).replaceAll(
          "\\",
          "/",
        );
        if (!isContainedBy(input.root, absolute)) {
          emitDiagnostics.push({
            code: "JAUNT_TS_EJECT_OUTPUT_ESCAPE",
            severity: "error",
            message: `TypeScript emit escapes the workspace root: ${workspaceRelative}`,
            path: workspaceRelative,
          });
          return;
        }
        if (!isContainedBy(packageRoot, absolute)) {
          emitDiagnostics.push({
            code: "JAUNT_TS_EJECT_PACKAGE_ESCAPE",
            severity: "error",
            message: `TypeScript emit escapes package owner ${input.packageRoot ?? "."}: ${workspaceRelative}`,
            path: workspaceRelative,
          });
          return;
        }
        emittedFiles.set(absolute, source);
        if (/\.d\.(?:ts|mts|cts)$/.test(absolute)) {
          emittedDeclarations.push(workspaceRelative);
        } else if (/\.(?:js|jsx|mjs|cjs)$/.test(absolute)) {
          emittedJavaScript.push(workspaceRelative);
        }
      })
    : undefined;
  const diagnostics: DiagnosticRecord[] = [
    ...[
      ...compiler.getPreEmitDiagnostics(program),
      ...(emit?.diagnostics ?? []),
    ].map((diagnostic) =>
      fromTypeScriptDiagnostic(compiler, input.root, diagnostic),
    ),
    ...testModulePolicyDiagnostics(compiler, input, program, host, options),
    ...emitDiagnostics,
  ];
  if (
    input.declarationEmit === true &&
    !diagnostics.some((diagnostic) => diagnostic.severity === "error") &&
    emittedDeclarations.length === 0
  ) {
    diagnostics.push({
      code: "JAUNT_TS_DECLARATION_EMIT_EMPTY",
      severity: "error",
      message: "The ejected project emitted no TypeScript declarations",
    });
  }
  if (
    input.normalEmit === true &&
    !diagnostics.some((diagnostic) => diagnostic.severity === "error")
  ) {
    const emitted = new Set(emittedFiles.keys());
    const targetOutputs = new Set<string>();
    const emitCommandLine: ts.ParsedCommandLine = { ...parsed, options };
    for (const file of input.files) {
      const sourcePath = resolve(input.root, file);
      try {
        for (const output of compiler.getOutputFileNames(
          emitCommandLine,
          sourcePath,
          !compiler.sys.useCaseSensitiveFileNames,
        )) {
          targetOutputs.add(resolve(output));
        }
      } catch (error) {
        diagnostics.push({
          code: "JAUNT_TS_EJECT_OUTPUT_UNRESOLVED",
          severity: "error",
          message: `Could not determine normal emit outputs for ${file}: ${error instanceof Error ? error.message : String(error)}`,
          path: file,
        });
      }
    }
    const expectedJavaScript = [...targetOutputs].filter((path) =>
      /\.(?:js|jsx|mjs|cjs)$/.test(path),
    );
    const expectedDeclarations = [...targetOutputs].filter((path) =>
      /\.d\.(?:ts|mts|cts)$/.test(path),
    );
    for (const [kind, paths] of [
      ["JavaScript", expectedJavaScript],
      ["declaration", expectedDeclarations],
    ] as const) {
      if (paths.length === 0 || paths.some((path) => !emitted.has(path))) {
        diagnostics.push({
          code: "JAUNT_TS_EJECT_OUTPUT_MISSING",
          severity: "error",
          message: `Normal TypeScript emit did not produce the ejected module's ${kind} output`,
        });
      }
    }
    const forbidden: readonly [RegExp, string][] = [
      [/@usejaunt\/ts(?:\/spec)?/, "Jaunt marker runtime"],
      [
        /(?:from\s+|import\s*\()["'][^"']*\.jaunt(?:[./][^"']*)?["']/,
        "private spec",
      ],
      [/__generated__/, "generated artifact"],
      [/(?:^|\n)\s*\/\/[^\n]*\bjaunt:/i, "Jaunt provenance"],
      [
        /\b(?:__jaunt_impl_[A-Za-z_$][\w$]*|__JauntApi)\b/,
        "reserved Jaunt binding",
      ],
    ];
    for (const path of targetOutputs) {
      const source = emittedFiles.get(path);
      if (source === undefined) continue;
      const outputPath = relative(input.root, path).replaceAll("\\", "/");
      if (/\.jaunt(?:\.|\/)|(?:^|\/)__generated__(?:\/|$)/.test(outputPath)) {
        diagnostics.push({
          code: "JAUNT_TS_EJECT_UNSAFE_OUTPUT",
          severity: "error",
          message: `Ejected module emitted to a Jaunt-private path: ${outputPath}`,
          path: outputPath,
        });
      }
      for (const [pattern, description] of forbidden) {
        if (!pattern.test(source)) continue;
        diagnostics.push({
          code: "JAUNT_TS_EJECT_UNSAFE_OUTPUT",
          severity: "error",
          message: `${outputPath} still references ${description}`,
          path: outputPath,
        });
      }
    }
  }
  return {
    ok: !diagnostics.some((diagnostic) => diagnostic.severity === "error"),
    mode: "typecheck",
    diagnostics,
    tests: [],
    captured: { stdout: "", stderr: "" },
    ...(emitRequested
      ? { emittedDeclarations: emittedDeclarations.sort() }
      : {}),
    ...(input.normalEmit === true
      ? { emittedJavaScript: emittedJavaScript.sort() }
      : {}),
  };
}

const GENERATED_BATTERY_SUFFIXES = [
  ".example.test.ts",
  ".example.test.tsx",
  ".derived.test.ts",
  ".derived.test.tsx",
  ".contract.test.ts",
  ".contract.test.tsx",
] as const;

function sourceForRunnerInput(input: TestRunnerInput, path: string): string {
  const overlaid = input.overlays?.[path];
  if (overlaid !== undefined) return overlaid;
  return readFileSync(resolve(input.root, path), "utf8");
}

function protectedBatteryExists(root: string): boolean {
  const skipped = new Set([
    ".git",
    ".jaunt",
    ".venv",
    "coverage",
    "node_modules",
    "venv",
  ]);
  const pending = [resolve(root)];
  while (pending.length > 0) {
    const directory = pending.pop();
    if (!directory) continue;
    for (const entry of readdirSync(directory)) {
      if (skipped.has(entry)) continue;
      const path = resolve(directory, entry);
      const metadata = lstatSync(path);
      if (metadata.isSymbolicLink()) continue;
      if (metadata.isDirectory()) {
        pending.push(path);
        continue;
      }
      if (!GENERATED_BATTERY_SUFFIXES.some((suffix) => entry.endsWith(suffix)))
        continue;
      const relativePath = relative(root, path).replaceAll("\\", "/");
      const source = readFileSync(path, "utf8");
      if (classifyTier(relativePath, source) !== "example") return true;
    }
  }
  return false;
}

function validateRunTier(input: TestRunnerInput): "example" | "derived" {
  const tiers = new Set(
    input.files.map((path) =>
      classifyTier(path, sourceForRunnerInput(input, path)),
    ),
  );
  if (tiers.size !== 1)
    throw new Error("protected test tiers must run in separate workspaces");
  const observed = tiers.values().next().value as "example" | "derived";
  if (input.tier !== undefined && input.tier !== observed)
    throw new Error("test source does not match the requested protected tier");
  if (observed === "example" && protectedBatteryExists(input.root)) {
    throw new Error(
      "example tests require a workspace with no protected battery files",
    );
  }
  return observed;
}

const PERMISSION_GUARD_INSTALLED = Symbol.for(
  "@usejaunt/ts/permission-guard-installed",
);

async function run(input: TestRunnerInput): Promise<TestRunnerOutput> {
  validateRunTier(input);
  if (
    input.permissionSandbox &&
    Reflect.get(globalThis, PERMISSION_GUARD_INSTALLED) !== true
  ) {
    throw new Error("protected Node permission guard was not preloaded");
  }
  const overlays = input.overlays ?? {};
  const reporter = new JauntReporter(input.root, overlays, input.redactDerived);
  const overlayAbsolute = new Map(
    Object.entries(overlays).map(([path, source]) => [
      resolve(input.root, path),
      source,
    ]),
  );
  const capturedOut: string[] = [];
  const capturedErr: string[] = [];
  let capturedOutBytes = 0;
  let capturedErrBytes = 0;
  let stdoutTruncated = false;
  let stderrTruncated = false;
  function capture(
    chunks: string[],
    chunk: string | Uint8Array,
    currentBytes: number,
  ): { bytes: number; truncated: boolean } {
    const buffer = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    const remaining = Math.max(0, MAX_CAPTURED_STREAM_BYTES - currentBytes);
    if (remaining > 0)
      chunks.push(buffer.subarray(0, remaining).toString("utf8"));
    return {
      bytes: currentBytes + Math.min(buffer.byteLength, remaining),
      truncated: buffer.byteLength > remaining,
    };
  }
  const originalOut = process.stdout.write.bind(process.stdout);
  const originalErr = process.stderr.write.bind(process.stderr);
  const placeholders = materializeOverlayPlaceholders(input);
  process.stdout.write = ((chunk: string | Uint8Array) => {
    const captured = capture(capturedOut, chunk, capturedOutBytes);
    capturedOutBytes = captured.bytes;
    stdoutTruncated ||= captured.truncated;
    return true;
  }) as typeof process.stdout.write;
  process.stderr.write = ((chunk: string | Uint8Array) => {
    const captured = capture(capturedErr, chunk, capturedErrBytes);
    capturedErrBytes = captured.bytes;
    stderrTruncated ||= captured.truncated;
    return true;
  }) as typeof process.stderr.write;
  try {
    const sourceResolver = await projectSourceResolver(input);
    const instance = await startVitest(
      "test",
      input.files.map((path) => resolve(input.root, path)),
      {
        root: input.root,
        run: true,
        watch: false,
        passWithNoTests: false,
        reporters: [reporter],
        include: [...input.files],
        testTimeout: input.timeoutMs,
        hookTimeout: input.timeoutMs,
        ...(input.tier ? { pool: "threads" as const } : {}),
        config: input.vitestConfigPath
          ? resolve(input.root, input.vitestConfigPath)
          : false,
      },
      {
        cacheDir: resolve(input.root, ".jaunt-vitest-cache"),
        server: { fs: { allow: [input.root], strict: true } },
        plugins: [
          ...(sourceResolver ? [sourceResolver] : []),
          {
            name: "jaunt-test-overlays",
            enforce: "pre",
            load(id: string) {
              return overlayAbsolute.get(resolve(id.split("?")[0]!)) ?? null;
            },
          },
        ],
      },
    );
    await instance?.close();
  } finally {
    process.stdout.write = originalOut;
    process.stderr.write = originalErr;
    removeOverlayPlaceholders(placeholders);
  }
  const ok =
    reporter.results.length > 0 &&
    reporter.results.every((result) => result.status !== "failed");
  const redactOutput = input.redactDerived;
  function renderedCapture(
    chunks: readonly string[],
    reporterOutput: string,
    alreadyTruncated: boolean,
  ): string {
    if (redactOutput) return "";
    const buffer = Buffer.from(chunks.join("") + reporterOutput);
    const truncated =
      alreadyTruncated ||
      reporter.outputTruncated ||
      buffer.byteLength > MAX_CAPTURED_STREAM_BYTES;
    return (
      buffer.subarray(0, MAX_CAPTURED_STREAM_BYTES).toString("utf8") +
      (truncated ? TRUNCATION_MARKER : "")
    );
  }
  stdoutTruncated ||= reporter.outputTruncated;
  stderrTruncated ||= reporter.outputTruncated;
  const diagnostics: DiagnosticRecord[] = [];
  if (stdoutTruncated || stderrTruncated) {
    diagnostics.push({
      code: "JAUNT_TS_RUNNER_OUTPUT_TRUNCATED",
      severity: "warning",
      message: `Captured runner output exceeded ${MAX_CAPTURED_STREAM_BYTES} bytes per stream and was truncated`,
    });
  }
  const output: TestRunnerOutput = {
    ok,
    mode: "run",
    diagnostics,
    tests: projectTestResults(reporter.results, redactOutput),
    captured: {
      stdout: renderedCapture(
        capturedOut,
        reporter.captured.stdout,
        stdoutTruncated,
      ),
      stderr: renderedCapture(
        capturedErr,
        reporter.captured.stderr,
        stderrTruncated,
      ),
    },
  };
  if (input.redactDerived) {
    reporter.heldOut.observe(capturedOut);
    reporter.heldOut.observe(capturedErr);
    reporter.heldOut.observe(reporter.captured);
    reporter.assertNoHeldOutLeak(output);
  }
  return output;
}

interface OverlayPlaceholders {
  readonly files: readonly string[];
  readonly directories: readonly string[];
}

function errnoIs(error: unknown, ...codes: readonly string[]): boolean {
  return (
    error instanceof Error &&
    "code" in error &&
    codes.includes(String((error as NodeJS.ErrnoException).code))
  );
}

function removeOverlayPlaceholders(placeholders: OverlayPlaceholders): void {
  for (const path of [...placeholders.files].reverse()) {
    try {
      unlinkSync(path);
    } catch (error) {
      if (!errnoIs(error, "ENOENT")) throw error;
    }
  }
  for (const path of [...placeholders.directories].reverse()) {
    try {
      rmdirSync(path);
    } catch (error) {
      if (!errnoIs(error, "ENOENT", "ENOTEMPTY", "EEXIST")) throw error;
    }
  }
}

function materializeOverlayPlaceholders(
  input: TestRunnerInput,
): OverlayPlaceholders {
  const files: string[] = [];
  const directories: string[] = [];
  try {
    for (const relativePath of input.files) {
      if (input.overlays?.[relativePath] === undefined) continue;
      const path = assertWithinRoot(
        input.root,
        resolve(input.root, relativePath),
      );
      if (existsSync(path)) continue;

      const missingDirectories: string[] = [];
      for (
        let parent = dirname(path);
        !existsSync(parent);
        parent = dirname(parent)
      ) {
        missingDirectories.push(assertWithinRoot(input.root, parent));
      }
      for (const directory of missingDirectories.reverse()) {
        try {
          mkdirSync(directory);
          directories.push(directory);
        } catch (error) {
          if (!errnoIs(error, "EEXIST")) throw error;
        }
      }
      try {
        const descriptor = openSync(path, "wx");
        closeSync(descriptor);
        files.push(path);
      } catch (error) {
        if (!errnoIs(error, "EEXIST")) throw error;
      }
    }
    return { files, directories };
  } catch (error) {
    removeOverlayPlaceholders({ files, directories });
    throw error;
  }
}

export async function runTestRunner(value: unknown): Promise<TestRunnerOutput> {
  const input = parseInput(value);
  try {
    if (input.mode === "run") return await run(input);
    const result = await typecheck(input);
    return input.redactDerived ? redactTypecheckResult(result) : result;
  } catch (error) {
    if (!input.redactDerived) throw error;
    const guard = new HeldOutLeakGuard();
    guard.observe(error);
    const fallback = redactedRunnerFailure(input.mode);
    guard.allow(fallback);
    guard.assertSafe(fallback);
    return fallback;
  }
}

export async function readBoundedInput(
  stream: AsyncIterable<string | Uint8Array>,
  maxBytes = MAX_RUNNER_INPUT_BYTES,
): Promise<string> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of stream) {
    const buffer = Buffer.from(chunk);
    total += buffer.byteLength;
    if (total > maxBytes) {
      throw new Error(`runner input exceeds ${maxBytes} bytes`);
    }
    chunks.push(buffer);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function readStdin(): Promise<string> {
  return readBoundedInput(process.stdin);
}

function comparableEntryPath(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

const invokedPath = process.argv[1] ? comparableEntryPath(process.argv[1]) : "";
if (invokedPath === comparableEntryPath(fileURLToPath(import.meta.url))) {
  void (async () => {
    let mode: "typecheck" | "run" = "run";
    let redactDerived = true;
    try {
      const value = JSON.parse(await readStdin()) as unknown;
      if (
        value &&
        typeof value === "object" &&
        "mode" in value &&
        ((value as { mode?: unknown }).mode === "typecheck" ||
          (value as { mode?: unknown }).mode === "run")
      ) {
        mode = (value as { mode: "typecheck" | "run" }).mode;
      }
      if (
        value &&
        typeof value === "object" &&
        "redactDerived" in value &&
        (value as { redactDerived?: unknown }).redactDerived === false
      ) {
        redactDerived = false;
      }
      const result = await runTestRunner(value);
      process.stdout.write(`${JSON.stringify(result)}\n`);
    } catch (error) {
      const result = redactDerived
        ? redactedRunnerFailure(mode)
        : {
            ok: false,
            mode,
            diagnostics: [
              {
                code: "JAUNT_TS_RUNNER",
                severity: "error",
                message: error instanceof Error ? error.message : String(error),
              },
            ],
            tests: [],
            captured: { stdout: "", stderr: "" },
          };
      process.stdout.write(`${JSON.stringify(result)}\n`);
      process.exitCode = 1;
    }
  })();
}
