import { existsSync, readFileSync } from "node:fs";
import {
  basename,
  dirname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import type ts from "@typescript/typescript6";
import { digestCanonical, sha256Bytes } from "./canonical.js";
import { assertWithinRoot, toPosix } from "./artifacts.js";
import { docsForNode } from "./docs.js";
import type { DiscoveredModule } from "./discovery.js";

/** Files and digest that define the type environment visible at a spec boundary. */
export interface TypeEnvironmentSnapshot {
  readonly digest: string;
  /** Environment identity with only @usejaunt/ts package metadata normalized. */
  readonly compatibilityDigest: string;
  /** Canonical documentation on the imported public surface. */
  readonly proseDigest: string;
  /** Deterministic imported/context TSDoc records for semantic-gate review. */
  readonly proseRecords: readonly ImportedDocsRecord[];
  readonly inputPaths: readonly string[];
}

const ENVIRONMENT_FILES = [
  "package.json",
  "package-lock.json",
  "npm-shrinkwrap.json",
  "pnpm-lock.yaml",
  "yarn.lock",
  "bun.lock",
  "bun.lockb",
] as const;

function isWithin(root: string, path: string): boolean {
  const value = relative(resolve(root), resolve(path));
  return value !== ".." && !value.startsWith(`..${sep}`) && !isAbsolute(value);
}

export function stablePathId(root: string, path: string): string {
  const normalized = path.replaceAll("\\", "/");
  const nodeModules = normalized.lastIndexOf("/node_modules/");
  if (nodeModules >= 0) {
    return `package:${normalized.slice(nodeModules + "/node_modules/".length)}`;
  }
  if (isWithin(root, path)) return `workspace:${toPosix(relative(root, path))}`;
  // An uncommon custom resolver may return a file outside the workspace.  Do
  // not put machine-specific absolute paths into a committed freshness digest.
  return `external:${basename(path)}`;
}

function semanticJson(source: string): unknown {
  try {
    return JSON.parse(source) as unknown;
  } catch {
    return { invalidJson: sha256Bytes(source) };
  }
}

function normalizeToolingMetadata(value: unknown, key = ""): unknown {
  if (
    key === "@usejaunt/ts" ||
    key === "node_modules/@usejaunt/ts" ||
    key.endsWith("/node_modules/@usejaunt/ts")
  ) {
    return "<jaunt-toolchain>";
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeToolingMetadata(item));
  }
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    if (object.name === "@usejaunt/ts") {
      return { name: "@usejaunt/ts" };
    }
    return Object.fromEntries(
      Object.entries(object).map(([childKey, item]) => [
        childKey,
        normalizeToolingMetadata(item, childKey),
      ]),
    );
  }
  return value;
}

function unquotedLockKey(value: string): string {
  const key = value.trim();
  if (
    key.length >= 2 &&
    ((key.startsWith('"') && key.endsWith('"')) ||
      (key.startsWith("'") && key.endsWith("'")))
  ) {
    return key.slice(1, -1);
  }
  return key;
}

function isToolingLockKey(value: string): boolean {
  const selectors = value.split(",").map(unquotedLockKey);
  return (
    selectors.length > 0 &&
    selectors.every(
      (selector) =>
        selector === "@usejaunt/ts" ||
        selector.startsWith("@usejaunt/ts@") ||
        selector.startsWith("/@usejaunt/ts@"),
    )
  );
}

/**
 * Normalize only @usejaunt/ts entries in text lockfiles. This handles pnpm
 * YAML, Yarn classic/Berry, and Bun's text lock without treating unrelated
 * package changes as a compatible toolchain upgrade. Legacy binary bun.lockb
 * remains intentionally opaque and therefore rebuilds conservatively.
 */
function normalizeToolingLockfile(source: string): string {
  const output: string[] = [];
  let skippedIndent: number | undefined;
  for (const original of source.match(/.*(?:\r?\n|$)/g) ?? []) {
    if (original === "") continue;
    const newline = original.endsWith("\r\n")
      ? "\r\n"
      : original.endsWith("\n")
        ? "\n"
        : "";
    const line = original.slice(0, original.length - newline.length);
    const indent = line.match(/^[\t ]*/)![0].length;
    const trimmed = line.trim();
    if (skippedIndent !== undefined) {
      if (trimmed === "" || indent > skippedIndent) continue;
      skippedIndent = undefined;
    }

    const entry = trimmed.match(/^(.+?)\s*([:=])\s*(.*)$/);
    if (entry && isToolingLockKey(entry[1]!)) {
      const marker = `${line.slice(0, indent)}"<jaunt-toolchain>"`;
      if (entry[2] === ":" && entry[3] === "") {
        output.push(`${marker}:${newline}`);
        skippedIndent = indent;
      } else {
        output.push(`${marker}${entry[2]} "<jaunt-toolchain>"${newline}`);
      }
      continue;
    }

    output.push(
      `${line.replace(
        /(["']@usejaunt\/ts["']\s*[:=]\s*)["'][^"']*["']/g,
        '$1"<jaunt-toolchain>"',
      )}${newline}`,
    );
  }
  return output.join("");
}

interface SourceSpan {
  readonly start: number;
  readonly end: number;
}

function declarationBodySpans(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly SourceSpan[] {
  const spans: SourceSpan[] = [];
  function visit(node: ts.Node): void {
    const callable =
      compiler.isFunctionDeclaration(node) ||
      compiler.isFunctionExpression(node) ||
      compiler.isArrowFunction(node) ||
      compiler.isMethodDeclaration(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node) ||
      compiler.isConstructorDeclaration(node);
    if (callable && node.body) {
      // A body can contribute to an exported type when TypeScript infers the
      // return. Keep those bodies, and all value/property initializers, in the
      // conservative surface. Explicit-return callables and constructors have
      // their complete type outside the body.
      const hasCompleteSignature =
        compiler.isConstructorDeclaration(node) ||
        compiler.isSetAccessorDeclaration(node) ||
        node.type !== undefined;
      if (hasCompleteSignature) {
        spans.push({
          start: node.body.getStart(sourceFile),
          end: node.body.end,
        });
        return;
      }
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return spans.sort((left, right) => left.start - right.start);
}

/**
 * Canonicalize syntax tokens while discarding trivia.  TypeScript's scanner
 * preserves literals and punctuation, so semantic declaration edits change the
 * digest while comments, indentation, and line endings do not.
 */
function semanticSource(
  compiler: typeof import("@typescript/typescript6"),
  path: string,
  source: string,
): unknown {
  if (path.endsWith(".json")) return semanticJson(source);
  const languageVariant = /\.[jt]sx$/.test(path)
    ? compiler.LanguageVariant.JSX
    : compiler.LanguageVariant.Standard;
  const scanner = compiler.createScanner(
    compiler.ScriptTarget.Latest,
    true,
    languageVariant,
    source,
  );
  const sourceFile = compiler.createSourceFile(
    path,
    source,
    compiler.ScriptTarget.Latest,
    true,
    languageVariant === compiler.LanguageVariant.JSX
      ? compiler.ScriptKind.TSX
      : compiler.ScriptKind.TS,
  );
  const bodySpans = /\.d\.[cm]?ts$/.test(path)
    ? []
    : declarationBodySpans(compiler, sourceFile);
  const tokens: [number, string][] = [];
  let spanIndex = 0;
  let emittedBodyMarker = false;
  for (
    let kind = scanner.scan();
    kind !== compiler.SyntaxKind.EndOfFileToken;
    kind = scanner.scan()
  ) {
    const position = scanner.getTokenPos();
    while (bodySpans[spanIndex] && position >= bodySpans[spanIndex]!.end) {
      spanIndex += 1;
      emittedBodyMarker = false;
    }
    const span = bodySpans[spanIndex];
    if (span && position >= span.start && position < span.end) {
      if (!emittedBodyMarker) tokens.push([-1, "<implementation-body>"]);
      emittedBodyMarker = true;
      continue;
    }
    const text =
      kind === compiler.SyntaxKind.StringLiteral ||
      kind === compiler.SyntaxKind.NoSubstitutionTemplateLiteral
        ? scanner.getTokenValue()
        : kind === compiler.SyntaxKind.NumericLiteral ||
            kind === compiler.SyntaxKind.BigIntLiteral
          ? scanner.getTokenValue()
          : scanner.getTokenText();
    tokens.push([kind, text]);
  }
  return tokens;
}

export interface ExportedDocsRecord {
  readonly symbol: string;
  readonly docs: string;
}

export interface ImportedDocsRecord {
  readonly id: string;
  readonly exports: readonly ExportedDocsRecord[];
}

function hasModifier(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  kind: ts.SyntaxKind,
): boolean {
  return (
    compiler.canHaveModifiers(node) &&
    (compiler.getModifiers(node)?.some((modifier) => modifier.kind === kind) ??
      false)
  );
}

function declarationName(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): string {
  if (
    (compiler.isFunctionDeclaration(node) ||
      compiler.isClassDeclaration(node) ||
      compiler.isInterfaceDeclaration(node) ||
      compiler.isTypeAliasDeclaration(node) ||
      compiler.isEnumDeclaration(node) ||
      compiler.isModuleDeclaration(node)) &&
    node.name
  ) {
    return node.name.getText();
  }
  if (
    (compiler.isMethodDeclaration(node) ||
      compiler.isMethodSignature(node) ||
      compiler.isPropertyDeclaration(node) ||
      compiler.isPropertySignature(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node)) &&
    node.name
  ) {
    return node.name.getText();
  }
  if (compiler.isConstructorDeclaration(node)) return "constructor";
  if (compiler.isCallSignatureDeclaration(node)) return "call";
  if (compiler.isConstructSignatureDeclaration(node)) return "construct";
  if (compiler.isIndexSignatureDeclaration(node)) return "index";
  if (compiler.isVariableDeclaration(node)) return node.name.getText();
  return compiler.SyntaxKind[node.kind] ?? String(node.kind);
}

/**
 * Collect documentation attached to direct exports and their public members.
 *
 * TSDoc is behavioral input for Jaunt, but TypeScript's scanner classifies it
 * as trivia. Keeping it in a separate digest lets a documentation-only context
 * edit take the prose path without turning it into a structural rebuild.
 */
function exportedDocs(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly ExportedDocsRecord[] {
  const records: ExportedDocsRecord[] = [];

  function add(node: ts.Node, symbol: string): void {
    const docs = docsForNode(compiler, sourceFile, node).text;
    if (docs) records.push({ symbol, docs });
  }

  for (const statement of sourceFile.statements) {
    if (!hasModifier(compiler, statement, compiler.SyntaxKind.ExportKeyword)) {
      continue;
    }
    if (compiler.isVariableStatement(statement)) {
      add(statement, "variables");
      for (const declaration of statement.declarationList.declarations) {
        add(declaration, `variable:${declarationName(compiler, declaration)}`);
      }
      continue;
    }

    const name = declarationName(compiler, statement);
    add(statement, name);
    if (
      compiler.isClassDeclaration(statement) ||
      compiler.isInterfaceDeclaration(statement)
    ) {
      for (const member of statement.members) {
        if (
          hasModifier(compiler, member, compiler.SyntaxKind.PrivateKeyword) ||
          hasModifier(compiler, member, compiler.SyntaxKind.ProtectedKeyword)
        ) {
          continue;
        }
        add(member, `${name}.${declarationName(compiler, member)}`);
      }
    }
  }

  return records.sort((left, right) => {
    const bySymbol = left.symbol.localeCompare(right.symbol);
    return bySymbol || left.docs.localeCompare(right.docs);
  });
}

function moduleSpecifiers(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly string[] {
  const result = new Set<string>();
  function visit(node: ts.Node): void {
    if (
      (compiler.isImportDeclaration(node) ||
        compiler.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      compiler.isStringLiteralLike(node.moduleSpecifier)
    ) {
      result.add(node.moduleSpecifier.text);
    } else if (
      compiler.isImportTypeNode(node) &&
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteralLike(node.argument.literal)
    ) {
      result.add(node.argument.literal.text);
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return [...result]
    .filter((value) => !/^@usejaunt\/ts(?:\/spec)?$/.test(value))
    .sort();
}

function environmentFiles(root: string, packageOwner: string): string[] {
  const files: string[] = [];
  let current = resolve(root, packageOwner);
  const boundary = resolve(root);
  while (isWithin(boundary, current)) {
    for (const name of ENVIRONMENT_FILES) {
      const path = join(current, name);
      if (existsSync(path)) files.push(path);
    }
    if (current === boundary) break;
    current = dirname(current);
  }
  return files;
}

function resolutionMode(
  compiler: typeof import("@typescript/typescript6"),
  path: string,
  compilerOptions: ts.CompilerOptions,
): ts.ResolutionMode {
  return compiler.getImpliedNodeFormatForFile(
    path,
    undefined,
    compiler.sys,
    compilerOptions,
  );
}

/**
 * Resolve and hash the imported declaration closure for one spec module.
 *
 * This intentionally follows every static import/export reachable from an
 * imported module, rather than trying to guess which declaration the checker
 * will eventually instantiate.  The conservative closure may rebuild a little
 * more often, but cannot leave a governed boundary fresh after a transitive
 * local or package type changes.
 */
export function collectTypeEnvironment(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  module: DiscoveredModule,
  compilerOptions: ts.CompilerOptions,
): TypeEnvironmentSnapshot {
  const records: { id: string; syntax: unknown }[] = [];
  const compatibleEnvironmentSyntax = new Map<string, unknown>();
  const proseRecords: ImportedDocsRecord[] = [];
  const inputPaths = new Set<string>();
  const visited = new Set<string>();
  const pending: { containingFile: string; specifier: string }[] =
    moduleSpecifiers(compiler, module.sourceFile).map((specifier) => ({
      containingFile: module.sourceFile.fileName,
      specifier,
    }));

  function addResolved(path: string, external = false): void {
    const absolute = external ? resolve(path) : assertWithinRoot(root, path);
    if (visited.has(absolute)) return;
    visited.add(absolute);
    if (!existsSync(absolute)) {
      records.push({
        id: stablePathId(root, absolute),
        syntax: { missing: true },
      });
      return;
    }
    let source: string;
    try {
      source = readFileSync(absolute, "utf8");
    } catch {
      records.push({
        id: stablePathId(root, absolute),
        syntax: { unreadable: true },
      });
      return;
    }
    inputPaths.add(absolute);
    records.push({
      id: stablePathId(root, absolute),
      syntax: semanticSource(compiler, absolute, source),
    });
    if (!/\.(?:[cm]?[jt]sx?|d\.[cm]?ts)$/.test(absolute)) return;
    const sourceFile = compiler.createSourceFile(
      absolute,
      source,
      compiler.ScriptTarget.Latest,
      true,
      /\.[cm]?tsx$/.test(absolute)
        ? compiler.ScriptKind.TSX
        : compiler.ScriptKind.TS,
    );
    const docs = exportedDocs(compiler, sourceFile);
    if (docs.length > 0) {
      proseRecords.push({ id: stablePathId(root, absolute), exports: docs });
    }
    for (const specifier of moduleSpecifiers(compiler, sourceFile)) {
      pending.push({ containingFile: absolute, specifier });
    }
    for (const reference of sourceFile.referencedFiles) {
      addResolved(resolve(dirname(absolute), reference.fileName), external);
    }
    for (const reference of sourceFile.typeReferenceDirectives) {
      const resolution = compiler.resolveTypeReferenceDirective(
        reference.fileName,
        absolute,
        compilerOptions,
        compiler.sys,
        undefined,
        undefined,
        resolutionMode(compiler, absolute, compilerOptions),
      ).resolvedTypeReferenceDirective;
      if (resolution?.resolvedFileName) {
        addResolved(
          resolution.resolvedFileName,
          external || resolution.isExternalLibraryImport === true,
        );
      } else
        records.push({
          id: `unresolved-type:${reference.fileName}`,
          syntax: null,
        });
    }
  }

  function drainPendingModules(): void {
    while (pending.length > 0) {
      const item = pending.shift();
      if (!item) break;
      const resolution = compiler.resolveModuleName(
        item.specifier,
        item.containingFile,
        compilerOptions,
        compiler.sys,
        undefined,
        undefined,
        resolutionMode(compiler, item.containingFile, compilerOptions),
      ).resolvedModule;
      if (resolution) {
        addResolved(
          resolution.resolvedFileName,
          resolution.isExternalLibraryImport === true,
        );
      } else
        records.push({
          id: `unresolved-module:${item.specifier}`,
          syntax: null,
        });
    }
  }
  drainPendingModules();

  const automaticTypes = compiler
    .getAutomaticTypeDirectiveNames(compilerOptions, compiler.sys)
    .sort();
  for (const typeName of automaticTypes) {
    const resolution = compiler.resolveTypeReferenceDirective(
      typeName,
      module.sourceFile.fileName,
      compilerOptions,
      compiler.sys,
      undefined,
      undefined,
      resolutionMode(compiler, module.sourceFile.fileName, compilerOptions),
    ).resolvedTypeReferenceDirective;
    if (resolution?.resolvedFileName) {
      addResolved(
        resolution.resolvedFileName,
        resolution.isExternalLibraryImport === true,
      );
    } else records.push({ id: `unresolved-type:${typeName}`, syntax: null });
  }
  drainPendingModules();

  for (const path of environmentFiles(root, module.route.packageOwner)) {
    const source = readFileSync(path, "utf8");
    inputPaths.add(path);
    const id = `environment:${toPosix(relative(root, path))}`;
    const syntax = path.endsWith(".json")
      ? semanticJson(source)
      : { sha256: sha256Bytes(source) };
    records.push({
      id,
      syntax,
    });
    compatibleEnvironmentSyntax.set(
      id,
      path.endsWith(".json")
        ? normalizeToolingMetadata(syntax)
        : { sha256: sha256Bytes(normalizeToolingLockfile(source)) },
    );
  }

  records.sort((left, right) => {
    const byId = left.id.localeCompare(right.id);
    return (
      byId ||
      digestCanonical(left.syntax).localeCompare(digestCanonical(right.syntax))
    );
  });
  const sortedProseRecords = proseRecords.sort((left, right) =>
    left.id.localeCompare(right.id),
  );
  return {
    digest: digestCanonical(records),
    compatibilityDigest: digestCanonical(
      records.map((record) => ({
        ...record,
        syntax: record.id.startsWith("environment:")
          ? (compatibleEnvironmentSyntax.get(record.id) ??
            normalizeToolingMetadata(record.syntax))
          : record.syntax,
      })),
    ),
    proseDigest: digestCanonical(sortedProseRecords),
    proseRecords: sortedProseRecords,
    inputPaths: [...inputPaths].sort(),
  };
}
