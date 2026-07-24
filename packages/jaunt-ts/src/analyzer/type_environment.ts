import { existsSync, readFileSync, realpathSync } from "node:fs";
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
  /** Environment identity scoped to resolved declarations and project metadata. */
  readonly compatibilityDigest: string;
  /** Per-input compatibility records persisted for actionable provenance diffs. */
  readonly compatibilityRecords: readonly SemanticEnvironmentRecord[];
  /** Tool-only provenance records excluded from semantic compatibility. */
  readonly toolingRecords: readonly SemanticEnvironmentRecord[];
  /** Canonical documentation on the imported public surface. */
  readonly proseDigest: string;
  /** Deterministic imported/context TSDoc records for semantic-gate review. */
  readonly proseRecords: readonly ImportedDocsRecord[];
  /** Bounded workspace declarations supplied to implementation and test models. */
  readonly modelTypeSources: readonly ModelTypeSource[];
  readonly inputPaths: readonly string[];
}

export interface ModelTypeSource {
  readonly id: string;
  readonly priority: "requested" | "supporting";
  readonly source: string;
}

const MODEL_TYPE_CONTEXT_LIMIT = 64 * 1024;

function compareCodeUnits(left: string, right: string): number {
  return left < right ? -1 : left > right ? 1 : 0;
}

export interface SemanticEnvironmentRecord {
  readonly id: string;
  readonly digest: string;
}

function compatibilityGroupId(id: string): string {
  if (id.startsWith("package:")) {
    const path = id.slice("package:".length);
    const parts = path.split("/");
    const packageName = path.startsWith("@")
      ? parts.slice(0, 2).join("/")
      : (parts[0] ?? path);
    return `package:${packageName}`;
  }
  if (id.startsWith("unresolved-module:")) return "unresolved-modules";
  if (id.startsWith("unresolved-type:")) return "unresolved-types";
  return id;
}

export function groupSemanticEnvironmentRecords(
  records: readonly { readonly id: string; readonly digest: string }[],
): readonly SemanticEnvironmentRecord[] {
  const grouped = new Map<string, Map<string, string>>();
  for (const record of records) {
    const groupId = compatibilityGroupId(record.id);
    const members = grouped.get(groupId) ?? new Map<string, string>();
    members.set(record.id, record.digest);
    grouped.set(groupId, members);
  }
  return [...grouped]
    .map(([id, members]) => ({
      id,
      digest: digestCanonical(
        [...members]
          .map(([memberId, digest]) => ({ id: memberId, digest }))
          .sort((left, right) => compareCodeUnits(left.id, right.id)),
      ),
    }))
    .sort((left, right) => compareCodeUnits(left.id, right.id));
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

const LOCK_FILES = new Set<string>([
  "package-lock.json",
  "npm-shrinkwrap.json",
  "pnpm-lock.yaml",
  "yarn.lock",
  "bun.lock",
  "bun.lockb",
]);

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

function normalizeToolingMetadata(
  value: unknown,
  key = "",
  depth = 0,
): unknown {
  if (
    key === "@usejaunt/ts" ||
    key === "node_modules/@usejaunt/ts" ||
    key.endsWith("/node_modules/@usejaunt/ts")
  ) {
    return "<jaunt-toolchain>";
  }
  if (Array.isArray(value)) {
    return value.map((item) => normalizeToolingMetadata(item, key, depth + 1));
  }
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    if (object.name === "@usejaunt/ts") {
      return { name: "@usejaunt/ts" };
    }
    return Object.fromEntries(
      Object.entries(object)
        // The root package-manager selector controls installation tooling; it
        // does not change the declarations visible to a governed module.
        .filter(([childKey]) => depth !== 0 || childKey !== "packageManager")
        .map(([childKey, item]) => [
          childKey,
          normalizeToolingMetadata(item, childKey, depth + 1),
        ]),
    );
  }
  return value;
}

function toolingProvenanceRecords(
  root: string,
  path: string,
  syntax: unknown,
): readonly SemanticEnvironmentRecord[] {
  if (
    basename(path) !== "package.json" ||
    syntax === null ||
    typeof syntax !== "object" ||
    Array.isArray(syntax)
  ) {
    return [];
  }
  const packageManager = (syntax as Record<string, unknown>).packageManager;
  if (typeof packageManager !== "string" || packageManager.trim() === "") {
    return [];
  }
  return [
    {
      id: `tooling:packageManager:${toPosix(relative(root, path))}`,
      digest: digestCanonical(packageManager.trim()),
    },
  ];
}

interface SourceSpan {
  readonly start: number;
  readonly end: number;
}

interface TemplateTokenStarts {
  readonly all: ReadonlySet<number>;
  readonly tagged: ReadonlySet<number>;
}

function templateTokenStarts(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): TemplateTokenStarts {
  const all = new Set<number>();
  const tagged = new Set<number>();
  function visit(node: ts.Node): void {
    if (
      compiler.isTemplateExpression(node) ||
      compiler.isTemplateLiteralTypeNode(node)
    ) {
      all.add(node.head.getStart(sourceFile));
      for (const span of node.templateSpans) {
        all.add(span.literal.getStart(sourceFile));
      }
    }
    if (compiler.isTaggedTemplateExpression(node)) {
      const template = node.template;
      if (compiler.isNoSubstitutionTemplateLiteral(template)) {
        tagged.add(template.getStart(sourceFile));
      } else {
        tagged.add(template.head.getStart(sourceFile));
        for (const span of template.templateSpans) {
          tagged.add(span.literal.getStart(sourceFile));
        }
      }
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return { all, tagged };
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
  const templates = templateTokenStarts(compiler, sourceFile);
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
    if (
      kind === compiler.SyntaxKind.CloseBraceToken &&
      templates.all.has(position)
    ) {
      kind = scanner.reScanTemplateToken(true);
    }
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
    const text = templates.tagged.has(position)
      ? "<tagged-template-text>"
      : kind === compiler.SyntaxKind.StringLiteral ||
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
    const bySymbol = compareCodeUnits(left.symbol, right.symbol);
    return bySymbol || compareCodeUnits(left.docs, right.docs);
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
      compiler.isImportEqualsDeclaration(node) &&
      compiler.isExternalModuleReference(node.moduleReference) &&
      node.moduleReference.expression &&
      compiler.isStringLiteralLike(node.moduleReference.expression)
    ) {
      result.add(node.moduleReference.expression.text);
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

interface DirectTypeImport {
  readonly specifier: string;
  readonly names: readonly string[];
}

interface TypeImportBinding {
  readonly specifier: string;
  readonly importedName: string;
  readonly localName: string;
  readonly typeOnly: boolean;
  readonly importEquals: boolean;
  readonly namespaceBinding: boolean;
}

function importBindings(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly TypeImportBinding[] {
  const bindings: TypeImportBinding[] = [];
  for (const statement of sourceFile.statements) {
    if (
      compiler.isImportEqualsDeclaration(statement) &&
      compiler.isExternalModuleReference(statement.moduleReference) &&
      statement.moduleReference.expression &&
      compiler.isStringLiteralLike(statement.moduleReference.expression)
    ) {
      bindings.push({
        specifier: statement.moduleReference.expression.text,
        importedName: "*",
        localName: statement.name.text,
        typeOnly: statement.isTypeOnly,
        importEquals: true,
        namespaceBinding: true,
      });
      continue;
    }
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier) ||
      !statement.importClause
    ) {
      continue;
    }
    const clause = statement.importClause;
    if (clause.name) {
      bindings.push({
        specifier: statement.moduleSpecifier.text,
        importedName: "default",
        localName: clause.name.text,
        typeOnly: clause.isTypeOnly,
        importEquals: false,
        namespaceBinding: false,
      });
    }
    const namedBindings = clause.namedBindings;
    if (namedBindings && compiler.isNamespaceImport(namedBindings)) {
      bindings.push({
        specifier: statement.moduleSpecifier.text,
        importedName: "*",
        localName: namedBindings.name.text,
        typeOnly: clause.isTypeOnly,
        importEquals: false,
        namespaceBinding: true,
      });
    }
    const named =
      namedBindings && compiler.isNamedImports(namedBindings)
        ? namedBindings.elements
        : [];
    for (const item of named) {
      bindings.push({
        specifier: statement.moduleSpecifier.text,
        importedName: item.propertyName?.text ?? item.name.text,
        localName: item.name.text,
        typeOnly: clause.isTypeOnly || item.isTypeOnly,
        importEquals: false,
        namespaceBinding: false,
      });
    }
  }
  return bindings;
}

type TypePositionNamespace = "type" | "value";
type ModelTypeNamespace = TypePositionNamespace | "both";

function modelTypeRequest(
  path: string,
  namespace: ModelTypeNamespace = "both",
): string {
  return namespace === "both" ? path : `${namespace}:${path}`;
}

function modelTypeRequestNamespace(request: string): ModelTypeNamespace {
  return request.startsWith("type:")
    ? "type"
    : request.startsWith("value:")
      ? "value"
      : "both";
}

function modelTypeRequestPath(request: string): string {
  const namespace = modelTypeRequestNamespace(request);
  return namespace === "both" ? request : request.slice(namespace.length + 1);
}

function modelTypeRequestWithPath(request: string, path: string): string {
  return modelTypeRequest(path, modelTypeRequestNamespace(request));
}

function identifierTypePositionNamespace(
  compiler: typeof import("@typescript/typescript6"),
  identifier: ts.Identifier,
): TypePositionNamespace | undefined {
  const immediateParent = identifier.parent;
  if (compiler.isExportSpecifier(immediateParent)) {
    const declaration = immediateParent.parent.parent;
    return (immediateParent.propertyName ?? immediateParent.name) ===
      identifier &&
      (immediateParent.isTypeOnly ||
        (compiler.isExportDeclaration(declaration) && declaration.isTypeOnly))
      ? "type"
      : undefined;
  }
  const namedParent = immediateParent as ts.Node & {
    readonly name?: ts.Node;
  };
  if (
    namedParent.name === identifier ||
    (compiler.isPropertyAccessExpression(immediateParent) &&
      immediateParent.name === identifier) ||
    (compiler.isQualifiedName(immediateParent) &&
      immediateParent.right === identifier)
  ) {
    return undefined;
  }

  let child: ts.Node = identifier;
  for (
    let parent: ts.Node | undefined = identifier.parent;
    parent;
    child = parent, parent = parent.parent
  ) {
    // A computed property expression is evaluated in the value namespace even
    // when the property itself appears inside a type literal. Declaration-
    // surface traversal handles it without making runtime object literals
    // eligible for direct type-context discovery.
    if (compiler.isComputedPropertyName(parent)) return undefined;
    if (compiler.isExpressionWithTypeArguments(parent)) {
      const heritage = parent.parent;
      if (!compiler.isHeritageClause(heritage)) return undefined;
      return heritage.token === compiler.SyntaxKind.ImplementsKeyword ||
        compiler.isInterfaceDeclaration(heritage.parent)
        ? "type"
        : undefined;
    }
    if (parent.kind === compiler.SyntaxKind.TypeQuery) return "value";
    if (compiler.isTypeNode(parent)) return "type";
    if (
      compiler.isImportDeclaration(parent) ||
      compiler.isImportEqualsDeclaration(parent) ||
      compiler.isStatement(parent)
    ) {
      return undefined;
    }
    // The expression side of `value as Type` / `value satisfies Type` is not a
    // type reference merely because its sibling is a TypeNode.
    if (
      (compiler.isAsExpression(parent) ||
        compiler.isTypeAssertionExpression(parent) ||
        compiler.isSatisfiesExpression(parent)) &&
      parent.expression === child
    ) {
      return undefined;
    }
  }
  return undefined;
}

function identifierIsShadowedImportReference(
  compiler: typeof import("@typescript/typescript6"),
  identifier: ts.Identifier,
  referenceNamespace: TypePositionNamespace,
): boolean {
  function bindingNameContains(name: ts.BindingName): boolean {
    if (compiler.isIdentifier(name)) return name.text === identifier.text;
    return name.elements.some(
      (element) =>
        !compiler.isOmittedExpression(element) &&
        bindingNameContains(element.name),
    );
  }

  function containsInferBinding(node: ts.Node): boolean {
    let found = false;
    function visit(child: ts.Node): void {
      if (found) return;
      if (compiler.isConditionalTypeNode(child)) {
        // Only an infer in the nested conditional's extends type belongs to
        // that conditional. Infer binders in its check and result branches are
        // still owned by the enclosing conditional whose extends type contains
        // the nested conditional.
        visit(child.checkType);
        visit(child.trueType);
        visit(child.falseType);
        return;
      }
      if (
        compiler.isInferTypeNode(child) &&
        child.typeParameter.name.text === identifier.text
      ) {
        found = true;
        return;
      }
      compiler.forEachChild(child, visit);
    }
    visit(node);
    return found;
  }

  // Discovery-bound roots expose binder locals. Transitive declarations are
  // reparsed for projection, so the structural fallbacks below cover lexical
  // binders that remain on declaration surfaces without a second Program.
  // Match only the namespace needed by this reference: a value parameter
  // shadows `typeof Entity`, but not an imported return type named `Entity`.
  const escapedName = identifier.text.startsWith("__")
    ? `_${identifier.text}`
    : identifier.text;
  const shadowMask =
    referenceNamespace === "value"
      ? compiler.SymbolFlags.Value |
        compiler.SymbolFlags.Namespace |
        compiler.SymbolFlags.Alias
      : compiler.SymbolFlags.Type |
        compiler.SymbolFlags.Namespace |
        compiler.SymbolFlags.Alias;
  for (
    let child: ts.Node = identifier,
      scope: ts.Node | undefined = identifier.parent;
    scope && !compiler.isSourceFile(scope);
    child = scope, scope = scope.parent
  ) {
    const locals = (
      scope as ts.Node & {
        readonly locals?: ReadonlyMap<
          unknown,
          { readonly flags: ts.SymbolFlags }
        >;
      }
    ).locals;
    const shadow = locals?.get(escapedName);
    const typeParameters = (
      scope as ts.Node & {
        readonly typeParameters?: readonly ts.TypeParameterDeclaration[];
      }
    ).typeParameters;
    const parameters = (
      scope as ts.Node & {
        readonly parameters?: readonly ts.ParameterDeclaration[];
      }
    ).parameters;
    const inferTypeParameter = compiler.isInferTypeNode(scope)
      ? scope.typeParameter
      : undefined;
    const conditionalInferShadow =
      referenceNamespace === "type" &&
      compiler.isConditionalTypeNode(scope) &&
      child === scope.trueType &&
      containsInferBinding(scope.extendsType);
    if (
      (shadow !== undefined && (shadow.flags & shadowMask) !== 0) ||
      (referenceNamespace === "value" &&
        parameters?.some((parameter) => bindingNameContains(parameter.name))) ||
      (referenceNamespace === "type" &&
        (typeParameters?.some(
          (parameter) => parameter.name.text === identifier.text,
        ) ||
          inferTypeParameter?.name.text === identifier.text ||
          conditionalInferShadow ||
          (compiler.isMappedTypeNode(scope) &&
            scope.typeParameter.name.text === identifier.text)))
    ) {
      return true;
    }
  }
  return false;
}

function identifierDeclarationSurfaceNamespace(
  compiler: typeof import("@typescript/typescript6"),
  identifier: ts.Identifier,
): TypePositionNamespace | undefined {
  const typePosition = identifierTypePositionNamespace(compiler, identifier);
  if (typePosition) {
    return identifierIsShadowedImportReference(
      compiler,
      identifier,
      typePosition,
    )
      ? undefined
      : typePosition;
  }
  const immediateParent = identifier.parent;
  const namedParent = immediateParent as ts.Node & {
    readonly name?: ts.Node;
  };
  if (
    namedParent.name === identifier ||
    (compiler.isPropertyAccessExpression(immediateParent) &&
      immediateParent.name === identifier) ||
    (compiler.isQualifiedName(immediateParent) &&
      immediateParent.right === identifier)
  ) {
    return undefined;
  }
  for (
    let parent: ts.Node | undefined = identifier.parent;
    parent;
    parent = parent.parent
  ) {
    if (compiler.isComputedPropertyName(parent)) {
      return identifierIsShadowedImportReference(compiler, identifier, "value")
        ? undefined
        : "value";
    }
    if (compiler.isExpressionWithTypeArguments(parent)) {
      const heritage = parent.parent;
      return compiler.isHeritageClause(heritage) &&
        heritage.token === compiler.SyntaxKind.ExtendsKeyword &&
        compiler.isClassDeclaration(heritage.parent) &&
        !identifierIsShadowedImportReference(compiler, identifier, "value")
        ? "value"
        : undefined;
    }
    if (compiler.isStatement(parent)) return undefined;
  }
  return undefined;
}

function namespaceMemberRequest(
  compiler: typeof import("@typescript/typescript6"),
  identifier: ts.Identifier,
): string {
  const segments: string[] = [];
  let current: ts.Node = identifier;
  while (true) {
    const parent = current.parent;
    if (
      compiler.isQualifiedName(parent) &&
      parent.left === current &&
      compiler.isIdentifier(parent.right)
    ) {
      segments.push(parent.right.text);
      current = parent;
      continue;
    }
    if (
      compiler.isPropertyAccessExpression(parent) &&
      parent.expression === current
    ) {
      segments.push(parent.name.text);
      current = parent;
      continue;
    }
    break;
  }
  return segments.length > 0 ? segments.join(".") : "*";
}

function requestForImportBinding(
  binding: TypeImportBinding,
  member: string,
): string {
  const path = modelTypeRequestPath(member);
  const suffix = path === "*" ? "" : `.${path}`;
  const rewritten = binding.importEquals
    ? `export=${suffix}`
    : binding.namespaceBinding
      ? path
      : `${binding.importedName}${suffix}`;
  return modelTypeRequestWithPath(member, rewritten);
}

function qualifiedNameText(
  compiler: typeof import("@typescript/typescript6"),
  name: ts.EntityName | undefined,
): string | undefined {
  if (!name) return undefined;
  if (compiler.isIdentifier(name)) return name.text;
  const left = qualifiedNameText(compiler, name.left);
  return left ? `${left}.${name.right.text}` : name.right.text;
}

function staticAccessPath(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): string | undefined {
  if (compiler.isIdentifier(node)) return node.text;
  if (compiler.isQualifiedName(node)) {
    return qualifiedNameText(compiler, node);
  }
  if (
    compiler.isPropertyAccessExpression(node) &&
    compiler.isIdentifier(node.name)
  ) {
    const expression = staticAccessPath(compiler, node.expression);
    return expression ? `${expression}.${node.name.text}` : undefined;
  }
  return undefined;
}

function typePositionImportUses(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  candidates: ReadonlySet<string>,
): ReadonlyMap<string, ReadonlySet<string>> {
  const used = new Map<string, Set<string>>();
  function visit(node: ts.Node): void {
    if (compiler.isIdentifier(node) && candidates.has(node.text)) {
      const namespace = identifierTypePositionNamespace(compiler, node);
      if (
        namespace === undefined ||
        identifierIsShadowedImportReference(compiler, node, namespace)
      ) {
        compiler.forEachChild(node, visit);
        return;
      }
      const requests = used.get(node.text) ?? new Set<string>();
      requests.add(
        modelTypeRequest(namespaceMemberRequest(compiler, node), namespace),
      );
      used.set(node.text, requests);
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return used;
}

function declarationPositionImportUses(
  compiler: typeof import("@typescript/typescript6"),
  declarations: readonly ModelTypeDeclaration[],
  candidates: ReadonlySet<string>,
  namespaceDeclarations?: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >,
): ReadonlyMap<string, ReadonlySet<string>> {
  const used = new Map<string, Set<string>>();
  function visit(node: ts.Node): void {
    if (
      compiler.isDecorator(node) ||
      compiler.isClassStaticBlockDeclaration(node)
    ) {
      return;
    }
    if (
      compiler.isModuleDeclaration(node) &&
      namespaceDeclarations?.has(node)
    ) {
      for (const child of namespaceDeclarations.get(node) ?? []) visit(child);
      return;
    }
    if (compiler.isIdentifier(node) && candidates.has(node.text)) {
      const namespace = identifierDeclarationSurfaceNamespace(compiler, node);
      if (namespace === undefined) {
        compiler.forEachChild(node, visit);
        return;
      }
      const requests = used.get(node.text) ?? new Set<string>();
      requests.add(
        modelTypeRequest(namespaceMemberRequest(compiler, node), namespace),
      );
      used.set(node.text, requests);
    }
    const body =
      compiler.isFunctionDeclaration(node) ||
      compiler.isMethodDeclaration(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node) ||
      compiler.isConstructorDeclaration(node)
        ? node.body
        : undefined;
    const initializer =
      compiler.isVariableDeclaration(node) ||
      compiler.isPropertyDeclaration(node) ||
      compiler.isParameter(node) ||
      compiler.isEnumMember(node)
        ? node.initializer
        : undefined;
    compiler.forEachChild(node, (child) => {
      if (child !== body && child !== initializer) visit(child);
    });
  }
  for (const declaration of declarations) visit(declaration);
  return used;
}

function directTypeImports(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  compilerOptions: ts.CompilerOptions,
  rootDeclarations: readonly ModelTypeDeclaration[],
): readonly DirectTypeImport[] {
  const grouped = new Map<string, Set<string>>();
  function add(specifier: string, name: string): void {
    const names = grouped.get(specifier) ?? new Set<string>();
    names.add(name);
    grouped.set(specifier, names);
  }
  const bindings = importBindings(compiler, sourceFile);
  const typeCandidates = bindings.filter(
    (binding) =>
      binding.typeOnly ||
      binding.importEquals ||
      compilerOptions.verbatimModuleSyntax !== true,
  );
  const typeCandidateNames = new Set(
    typeCandidates.map((binding) => binding.localName),
  );
  const typeUses = typePositionImportUses(
    compiler,
    sourceFile,
    typeCandidateNames,
  );
  // A concrete base is a value-space reference, but its public declaration is
  // still required model context for a governed root class. Keep this bounded
  // to discovered root declarations so unrelated runtime imports stay absent.
  // Unlike ordinary type-position discovery, this scan must consider value
  // imports under verbatimModuleSyntax because class heritage is intentionally
  // a runtime reference.
  const declarationUses = declarationPositionImportUses(
    compiler,
    rootDeclarations,
    new Set(bindings.map((binding) => binding.localName)),
  );
  for (const binding of bindings) {
    const requestedNames = new Set([
      ...(typeUses.get(binding.localName) ?? []),
      ...(declarationUses.get(binding.localName) ?? []),
    ]);
    if (binding.namespaceBinding) {
      for (const member of requestedNames) {
        add(binding.specifier, requestForImportBinding(binding, member));
      }
    } else if (binding.typeOnly || requestedNames.size > 0) {
      if (requestedNames.size === 0) {
        add(binding.specifier, modelTypeRequest(binding.importedName, "type"));
      } else {
        for (const member of requestedNames) {
          add(binding.specifier, requestForImportBinding(binding, member));
        }
      }
    }
  }
  function visit(node: ts.Node): void {
    if (
      compiler.isDecorator(node) ||
      compiler.isClassStaticBlockDeclaration(node)
    ) {
      return;
    }
    if (
      compiler.isImportTypeNode(node) &&
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteralLike(node.argument.literal)
    ) {
      add(
        node.argument.literal.text,
        modelTypeRequest(
          qualifiedNameText(compiler, node.qualifier) ?? "*",
          node.isTypeOf ? "value" : "type",
        ),
      );
    }
    const body =
      compiler.isFunctionDeclaration(node) ||
      compiler.isMethodDeclaration(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node) ||
      compiler.isConstructorDeclaration(node)
        ? node.body
        : undefined;
    const initializer =
      compiler.isVariableDeclaration(node) ||
      compiler.isPropertyDeclaration(node) ||
      compiler.isParameter(node) ||
      compiler.isEnumMember(node)
        ? node.initializer
        : undefined;
    compiler.forEachChild(node, (child) => {
      if (child !== body && child !== initializer) visit(child);
    });
  }
  visit(sourceFile);
  return [...grouped]
    .sort(([left], [right]) => compareCodeUnits(left, right))
    .map(([specifier, names]) => ({
      specifier,
      names: [...names].sort(),
    }));
}

function modelTypeDependencyImports(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  selection: ModelTypeSelection,
): readonly DirectTypeImport[] {
  const grouped = new Map<string, Set<string>>();
  function add(specifier: string, name: string): void {
    const names = grouped.get(specifier) ?? new Set<string>();
    names.add(name);
    grouped.set(specifier, names);
  }
  const declarations = [...selection.requested, ...selection.supporting];
  const bindings = importBindings(compiler, sourceFile);
  const uses = declarationPositionImportUses(
    compiler,
    declarations,
    new Set(bindings.map((binding) => binding.localName)),
    selection.namespaceDeclarations,
  );
  for (const binding of bindings) {
    for (const member of uses.get(binding.localName) ?? []) {
      add(binding.specifier, requestForImportBinding(binding, member));
    }
  }
  function visit(node: ts.Node): void {
    if (
      compiler.isDecorator(node) ||
      compiler.isClassStaticBlockDeclaration(node)
    ) {
      return;
    }
    if (
      compiler.isModuleDeclaration(node) &&
      selection.namespaceDeclarations.has(node)
    ) {
      for (const child of selection.namespaceDeclarations.get(node) ?? []) {
        visit(child);
      }
      return;
    }
    if (
      compiler.isImportTypeNode(node) &&
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteralLike(node.argument.literal)
    ) {
      add(
        node.argument.literal.text,
        modelTypeRequest(
          qualifiedNameText(compiler, node.qualifier) ?? "*",
          node.isTypeOf ? "value" : "type",
        ),
      );
    }
    const body =
      compiler.isFunctionDeclaration(node) ||
      compiler.isMethodDeclaration(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node) ||
      compiler.isConstructorDeclaration(node)
        ? node.body
        : undefined;
    const initializer =
      compiler.isVariableDeclaration(node) ||
      compiler.isPropertyDeclaration(node) ||
      compiler.isParameter(node) ||
      compiler.isEnumMember(node)
        ? node.initializer
        : undefined;
    compiler.forEachChild(node, (child) => {
      if (child !== body && child !== initializer) visit(child);
    });
  }
  for (const declaration of declarations) {
    visit(declaration);
  }
  return [...grouped]
    .sort(([left], [right]) => compareCodeUnits(left, right))
    .map(([specifier, names]) => ({
      specifier,
      names: [...names].sort(),
    }));
}

function workspaceModelTypePath(
  root: string,
  resolvedFileName: string,
): string | undefined {
  let physicalRoot: string;
  let physicalPath: string;
  try {
    physicalRoot = realpathSync(root);
    physicalPath = realpathSync(resolvedFileName);
  } catch {
    return undefined;
  }
  if (!isWithin(physicalRoot, physicalPath)) return undefined;
  const relativePath = toPosix(relative(physicalRoot, physicalPath));
  if (relativePath.split("/").includes("node_modules")) return undefined;
  if (/\.jaunt\.[cm]?[jt]sx?$/.test(physicalPath)) return undefined;
  return physicalPath;
}

type ModelTypeDeclaration =
  | ts.InterfaceDeclaration
  | ts.TypeAliasDeclaration
  | ts.EnumDeclaration
  | ts.ClassDeclaration
  | ts.FunctionDeclaration
  | ts.VariableStatement
  | ts.ModuleDeclaration;

function modelTypeDeclarations(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly ModelTypeDeclaration[] {
  return sourceFile.statements.filter((statement) =>
    isModelTypeDeclaration(compiler, statement),
  );
}

function isModelTypeDeclaration(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.Statement,
): statement is ModelTypeDeclaration {
  return (
    compiler.isInterfaceDeclaration(statement) ||
    compiler.isTypeAliasDeclaration(statement) ||
    compiler.isEnumDeclaration(statement) ||
    compiler.isClassDeclaration(statement) ||
    compiler.isFunctionDeclaration(statement) ||
    compiler.isVariableStatement(statement) ||
    compiler.isModuleDeclaration(statement)
  );
}

function modelTypeDeclarationNames(
  compiler: typeof import("@typescript/typescript6"),
  statement: ModelTypeDeclaration,
): readonly string[] {
  if (compiler.isVariableStatement(statement)) {
    return statement.declarationList.declarations.flatMap((declaration) =>
      compiler.isIdentifier(declaration.name) ? [declaration.name.text] : [],
    );
  }
  return statement.name ? [statement.name.text] : [];
}

function requestRoot(request: string): string {
  const path = modelTypeRequestPath(request);
  const separator = path.indexOf(".");
  return separator === -1 ? path : path.slice(0, separator);
}

function requestTail(request: string): string | undefined {
  const path = modelTypeRequestPath(request);
  const separator = path.indexOf(".");
  return separator === -1 ? undefined : path.slice(separator + 1);
}

function declarationSupportsModelTypeNamespace(
  compiler: typeof import("@typescript/typescript6"),
  statement: ModelTypeDeclaration,
  namespace: ModelTypeNamespace,
): boolean {
  if (namespace === "both") return true;
  if (
    compiler.isClassDeclaration(statement) ||
    compiler.isEnumDeclaration(statement) ||
    compiler.isModuleDeclaration(statement)
  ) {
    return true;
  }
  return namespace === "type"
    ? compiler.isInterfaceDeclaration(statement) ||
        compiler.isTypeAliasDeclaration(statement)
    : compiler.isFunctionDeclaration(statement) ||
        compiler.isVariableStatement(statement);
}

function requestMatchesModelTypeDeclaration(
  compiler: typeof import("@typescript/typescript6"),
  statement: ModelTypeDeclaration,
  request: string,
): boolean {
  const path = modelTypeRequestPath(request);
  return (
    declarationSupportsModelTypeNamespace(
      compiler,
      statement,
      modelTypeRequestNamespace(request),
    ) &&
    (path === "*" ||
      modelTypeDeclarationNames(compiler, statement).some(
        (name) => requestRoot(request) === name,
      ) ||
      (requestRoot(request) === "default" &&
        hasModifier(compiler, statement, compiler.SyntaxKind.DefaultKeyword)))
  );
}

function nestedModelTypeRequests(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.ModuleDeclaration,
  requests: ReadonlySet<string>,
): ReadonlySet<string> {
  const nested = new Set<string>();
  for (const request of requests) {
    if (!requestMatchesModelTypeDeclaration(compiler, statement, request)) {
      continue;
    }
    if (modelTypeRequestPath(request) === "*") {
      nested.add(modelTypeRequestWithPath(request, "*"));
      continue;
    }
    nested.add(modelTypeRequestWithPath(request, requestTail(request) ?? "*"));
  }
  return nested;
}

function moduleModelTypeDeclarations(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.ModuleDeclaration,
): readonly ModelTypeDeclaration[] {
  const body = statement.body;
  if (!body) return [];
  if (compiler.isModuleDeclaration(body)) return [body];
  if (!compiler.isModuleBlock(body)) return [];
  return body.statements.filter((child) =>
    isModelTypeDeclaration(compiler, child),
  );
}

function modelTypeDeclarationReferences(
  compiler: typeof import("@typescript/typescript6"),
  statement: ModelTypeDeclaration,
): ReadonlySet<string> {
  const references = new Set<string>();

  function referencePath(identifier: ts.Identifier): string {
    const segments = [identifier.text];
    let current: ts.Node = identifier;
    while (true) {
      const parent = current.parent;
      if (
        compiler.isQualifiedName(parent) &&
        parent.left === current &&
        compiler.isIdentifier(parent.right)
      ) {
        segments.push(parent.right.text);
        current = parent;
        continue;
      }
      if (
        compiler.isPropertyAccessExpression(parent) &&
        parent.expression === current
      ) {
        segments.push(parent.name.text);
        current = parent;
        continue;
      }
      break;
    }
    return segments.join(".");
  }

  function visit(node: ts.Node): void {
    if (
      compiler.isDecorator(node) ||
      compiler.isClassStaticBlockDeclaration(node)
    ) {
      return;
    }
    if (compiler.isIdentifier(node)) {
      const namespace = identifierDeclarationSurfaceNamespace(compiler, node);
      if (namespace !== undefined) {
        references.add(modelTypeRequest(referencePath(node), namespace));
      }
      return;
    }

    const body =
      compiler.isFunctionDeclaration(node) ||
      compiler.isMethodDeclaration(node) ||
      compiler.isGetAccessorDeclaration(node) ||
      compiler.isSetAccessorDeclaration(node) ||
      compiler.isConstructorDeclaration(node)
        ? node.body
        : undefined;
    const initializer =
      compiler.isVariableDeclaration(node) ||
      compiler.isPropertyDeclaration(node) ||
      compiler.isParameter(node) ||
      compiler.isEnumMember(node)
        ? node.initializer
        : undefined;
    compiler.forEachChild(node, (child) => {
      if (child !== body && child !== initializer) visit(child);
    });
  }

  visit(statement);
  return references;
}

interface ModelTypeSelection {
  readonly requested: readonly ModelTypeDeclaration[];
  readonly supporting: readonly ModelTypeDeclaration[];
  readonly references: ReadonlySet<string>;
  readonly namespaceDeclarations: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >;
}

interface ModelTypeContainerSelection {
  readonly selected: readonly ModelTypeDeclaration[];
  readonly references: ReadonlySet<string>;
  readonly unresolvedReferences: ReadonlySet<string>;
  readonly namespaceDeclarations: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >;
}

function selectModelTypeContainer(
  compiler: typeof import("@typescript/typescript6"),
  declarations: readonly ModelTypeDeclaration[],
  initialRequests: ReadonlySet<string>,
): ModelTypeContainerSelection {
  let requests = new Set(initialRequests);

  while (true) {
    const selected = declarations.filter((declaration) =>
      [...requests].some((request) =>
        requestMatchesModelTypeDeclaration(compiler, declaration, request),
      ),
    );
    const nextRequests = new Set(requests);
    const references = new Set<string>();
    const unresolvedReferences = new Set<string>();
    const namespaceDeclarations = new Map<
      ts.ModuleDeclaration,
      readonly ModelTypeDeclaration[]
    >();

    function routeReference(reference: string): void {
      if (
        declarations.some((declaration) =>
          requestMatchesModelTypeDeclaration(compiler, declaration, reference),
        )
      ) {
        nextRequests.add(reference);
      } else {
        unresolvedReferences.add(reference);
      }
    }

    const moduleGroups = new Map<string, ts.ModuleDeclaration[]>();
    for (const declaration of selected) {
      if (compiler.isModuleDeclaration(declaration)) {
        const group = moduleGroups.get(declaration.name.text) ?? [];
        group.push(declaration);
        moduleGroups.set(declaration.name.text, group);
        continue;
      }
      for (const reference of modelTypeDeclarationReferences(
        compiler,
        declaration,
      )) {
        references.add(reference);
        routeReference(reference);
      }
    }
    for (const modules of moduleGroups.values()) {
      const childDeclarations = new Map<
        ts.ModuleDeclaration,
        readonly ModelTypeDeclaration[]
      >();
      const nestedRequests = new Set<string>();
      for (const module of modules) {
        childDeclarations.set(
          module,
          moduleModelTypeDeclarations(compiler, module),
        );
        for (const request of nestedModelTypeRequests(
          compiler,
          module,
          requests,
        )) {
          nestedRequests.add(request);
        }
      }
      const nested = selectModelTypeContainer(
        compiler,
        [...childDeclarations.values()].flat(),
        nestedRequests,
      );
      const selectedChildren = new Set(nested.selected);
      for (const [module, children] of childDeclarations) {
        namespaceDeclarations.set(
          module,
          children.filter((child) => selectedChildren.has(child)),
        );
      }
      for (const [namespace, children] of nested.namespaceDeclarations) {
        namespaceDeclarations.set(namespace, children);
      }
      for (const reference of nested.references) references.add(reference);
      for (const reference of nested.unresolvedReferences) {
        routeReference(reference);
      }
    }

    if (
      requests.size === nextRequests.size &&
      [...requests].every((request) => nextRequests.has(request))
    ) {
      return {
        selected,
        references,
        unresolvedReferences,
        namespaceDeclarations,
      };
    }
    requests = nextRequests;
  }
}

/** Close requested declarations over same-file names without inspecting runtime code. */
function selectModelTypeDeclarations(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  requestedNames: ReadonlySet<string>,
): ModelTypeSelection {
  const declarations = modelTypeDeclarations(compiler, sourceFile);
  const requested = declarations.filter((declaration) =>
    [...requestedNames].some((request) =>
      requestMatchesModelTypeDeclaration(compiler, declaration, request),
    ),
  );
  const selection = selectModelTypeContainer(
    compiler,
    declarations,
    requestedNames,
  );
  const selected = new Set(selection.selected);

  return {
    requested,
    supporting: declarations.filter(
      (declaration) =>
        selected.has(declaration) && !requested.includes(declaration),
    ),
    references: selection.references,
    namespaceDeclarations: selection.namespaceDeclarations,
  };
}

function locallyAliasedTypeNames(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  requestedNames: ReadonlySet<string>,
): ReadonlySet<string> {
  const expanded = new Set(requestedNames);
  const hasExportEquals = sourceFile.statements.some(
    (statement) =>
      compiler.isExportAssignment(statement) && statement.isExportEquals,
  );

  function rewritePrefix(from: string, to: string): boolean {
    let added = false;
    for (const request of expanded) {
      const path = modelTypeRequestPath(request);
      if (path !== from && !path.startsWith(`${from}.`)) continue;
      const suffix = path === from ? "" : path.slice(from.length + 1);
      const rewritten = modelTypeRequestWithPath(
        request,
        suffix ? `${to}.${suffix}` : to,
      );
      if (!expanded.has(rewritten)) {
        expanded.add(rewritten);
        added = true;
      }
    }
    return added;
  }

  let changed = true;
  while (changed) {
    changed = false;
    for (const statement of sourceFile.statements) {
      if (compiler.isExportAssignment(statement)) {
        const exportedPath = staticAccessPath(compiler, statement.expression);
        if (exportedPath) {
          changed =
            rewritePrefix(
              statement.isExportEquals ? "export=" : "default",
              exportedPath,
            ) || changed;
        }
        continue;
      }
      if (
        compiler.isImportEqualsDeclaration(statement) &&
        !compiler.isExternalModuleReference(statement.moduleReference)
      ) {
        const target = qualifiedNameText(compiler, statement.moduleReference);
        if (target) {
          changed = rewritePrefix(statement.name.text, target) || changed;
        }
        continue;
      }
      if (
        !compiler.isExportDeclaration(statement) ||
        statement.moduleSpecifier ||
        !statement.exportClause ||
        compiler.isNamespaceExport(statement.exportClause)
      ) {
        continue;
      }
      for (const element of statement.exportClause.elements) {
        const localName = element.propertyName?.text ?? element.name.text;
        changed = rewritePrefix(element.name.text, localName) || changed;
      }
    }
    if (!hasExportEquals) {
      for (const request of expanded) {
        const path = modelTypeRequestPath(request);
        if (path !== "export=" && !path.startsWith("export=.")) {
          continue;
        }
        const rewritten = modelTypeRequestWithPath(
          request,
          path === "export=" ? "*" : path.slice("export=.".length),
        );
        if (!expanded.has(rewritten)) {
          expanded.add(rewritten);
          changed = true;
        }
      }
    }
  }
  return expanded;
}

function forwardedTypeImports(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  requestedNames: ReadonlySet<string>,
): readonly DirectTypeImport[] {
  const grouped = new Map<string, Set<string>>();
  const localBindings = new Map<string, TypeImportBinding[]>();
  for (const binding of importBindings(compiler, sourceFile)) {
    const bindings = localBindings.get(binding.localName) ?? [];
    bindings.push(binding);
    localBindings.set(binding.localName, bindings);
  }

  function add(specifier: string, name: string): void {
    const names = grouped.get(specifier) ?? new Set<string>();
    names.add(name);
    grouped.set(specifier, names);
  }

  function requestedMembers(name: string): readonly string[] {
    const members: string[] = [];
    for (const request of requestedNames) {
      const path = modelTypeRequestPath(request);
      if (path === "*" || path === name) {
        members.push(modelTypeRequestWithPath(request, "*"));
      } else if (path.startsWith(`${name}.`)) {
        members.push(
          modelTypeRequestWithPath(request, path.slice(name.length + 1)),
        );
      }
    }
    return members;
  }

  function addBindingRequest(binding: TypeImportBinding, member: string): void {
    add(binding.specifier, requestForImportBinding(binding, member));
  }

  for (const statement of sourceFile.statements) {
    if (
      compiler.isExportAssignment(statement) &&
      compiler.isIdentifier(statement.expression)
    ) {
      for (const member of requestedMembers(statement.expression.text)) {
        for (const binding of localBindings.get(statement.expression.text) ??
          []) {
          addBindingRequest(binding, member);
        }
      }
      continue;
    }
    if (!compiler.isExportDeclaration(statement)) continue;
    const clause = statement.exportClause;
    const moduleSpecifier =
      statement.moduleSpecifier &&
      compiler.isStringLiteralLike(statement.moduleSpecifier)
        ? statement.moduleSpecifier.text
        : undefined;

    if (moduleSpecifier) {
      if (!clause) {
        for (const name of requestedNames) {
          const path = modelTypeRequestPath(name);
          if (path !== "default" && !path.startsWith("default.")) {
            add(moduleSpecifier, name);
          }
        }
        continue;
      }
      if (compiler.isNamespaceExport(clause)) {
        for (const member of requestedMembers(clause.name.text)) {
          add(moduleSpecifier, member);
        }
        continue;
      }
      for (const element of clause.elements) {
        for (const member of requestedMembers(element.name.text)) {
          const importedName = element.propertyName?.text ?? element.name.text;
          const memberPath = modelTypeRequestPath(member);
          add(
            moduleSpecifier,
            modelTypeRequestWithPath(
              member,
              memberPath === "*"
                ? importedName
                : `${importedName}.${memberPath}`,
            ),
          );
        }
      }
      continue;
    }

    if (!clause || compiler.isNamespaceExport(clause)) continue;
    for (const element of clause.elements) {
      const localName = element.propertyName?.text ?? element.name.text;
      for (const member of requestedMembers(element.name.text)) {
        for (const binding of localBindings.get(localName) ?? []) {
          addBindingRequest(binding, member);
        }
      }
    }
  }
  // Static export assignments and internal import-equals aliases are expanded
  // into their local access paths above. Forward any resulting path through
  // the external binding at its root without collapsing the requested suffix.
  for (const binding of importBindings(compiler, sourceFile)) {
    for (const member of requestedMembers(binding.localName)) {
      addBindingRequest(binding, member);
    }
  }

  return [...grouped]
    .sort(([left], [right]) => compareCodeUnits(left, right))
    .map(([specifier, names]) => ({
      specifier,
      names: [...names].sort(),
    }));
}

type ModelTypePriority = "requested" | "supporting";

interface ModelTypeChunk {
  readonly path: string;
  readonly priority: ModelTypePriority;
  readonly order: number;
  readonly source: string;
}

function declarationModifiers(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  options: { readonly ambient?: boolean } = {},
): ts.Modifier[] | undefined {
  const modifiers = (
    compiler.canHaveModifiers(node) ? (compiler.getModifiers(node) ?? []) : []
  ).filter(
    (modifier) =>
      modifier.kind !== compiler.SyntaxKind.PrivateKeyword &&
      modifier.kind !== compiler.SyntaxKind.ProtectedKeyword &&
      modifier.kind !== compiler.SyntaxKind.AsyncKeyword,
  );
  if (
    options.ambient === true &&
    // `declare` cannot be combined with a default export. A bodyless
    // `export default class/function` is the canonical declaration form in a
    // .d.ts surface and retains the original default-export relationship.
    !modifiers.some(
      (modifier) => modifier.kind === compiler.SyntaxKind.DefaultKeyword,
    ) &&
    !modifiers.some(
      (modifier) => modifier.kind === compiler.SyntaxKind.DeclareKeyword,
    )
  ) {
    modifiers.push(
      compiler.factory.createModifier(compiler.SyntaxKind.DeclareKeyword),
    );
  }
  return modifiers.length > 0 ? modifiers : undefined;
}

function inferredInitializerType(
  compiler: typeof import("@typescript/typescript6"),
  initializer: ts.Expression | undefined,
): ts.TypeNode {
  if (
    initializer &&
    (compiler.isStringLiteralLike(initializer) ||
      compiler.isNoSubstitutionTemplateLiteral(initializer))
  ) {
    return compiler.factory.createKeywordTypeNode(
      compiler.SyntaxKind.StringKeyword,
    );
  }
  if (
    initializer &&
    (compiler.isNumericLiteral(initializer) ||
      (compiler.isPrefixUnaryExpression(initializer) &&
        compiler.isNumericLiteral(initializer.operand)))
  ) {
    return compiler.factory.createKeywordTypeNode(
      compiler.SyntaxKind.NumberKeyword,
    );
  }
  if (
    initializer &&
    (initializer.kind === compiler.SyntaxKind.TrueKeyword ||
      initializer.kind === compiler.SyntaxKind.FalseKeyword)
  ) {
    return compiler.factory.createKeywordTypeNode(
      compiler.SyntaxKind.BooleanKeyword,
    );
  }
  if (initializer && compiler.isArrayLiteralExpression(initializer)) {
    return compiler.factory.createArrayTypeNode(
      compiler.factory.createKeywordTypeNode(
        compiler.SyntaxKind.UnknownKeyword,
      ),
    );
  }
  return compiler.factory.createKeywordTypeNode(
    compiler.SyntaxKind.UnknownKeyword,
  );
}

function declarationParameter(
  compiler: typeof import("@typescript/typescript6"),
  parameter: ts.ParameterDeclaration,
): ts.ParameterDeclaration {
  return compiler.factory.updateParameterDeclaration(
    parameter,
    undefined,
    parameter.dotDotDotToken,
    parameter.name,
    parameter.questionToken ??
      (parameter.initializer
        ? compiler.factory.createToken(compiler.SyntaxKind.QuestionToken)
        : undefined),
    parameter.type ?? inferredInitializerType(compiler, parameter.initializer),
    undefined,
  );
}

function inferredBodyType(
  compiler: typeof import("@typescript/typescript6"),
  body: ts.Block | undefined,
): ts.TypeNode {
  const returns = body?.statements.filter(compiler.isReturnStatement) ?? [];
  if (returns.length === 1) {
    if (!returns[0]!.expression) {
      return compiler.factory.createKeywordTypeNode(
        compiler.SyntaxKind.VoidKeyword,
      );
    }
    return inferredInitializerType(compiler, returns[0]!.expression);
  }
  return compiler.factory.createKeywordTypeNode(
    compiler.SyntaxKind.UnknownKeyword,
  );
}

function declarationClass(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.ClassDeclaration,
): ts.ClassDeclaration {
  const members: ts.ClassElement[] = [];
  for (const member of statement.members) {
    if (
      hasModifier(compiler, member, compiler.SyntaxKind.PrivateKeyword) ||
      hasModifier(compiler, member, compiler.SyntaxKind.ProtectedKeyword) ||
      compiler.isClassStaticBlockDeclaration(member) ||
      ("name" in member &&
        member.name &&
        compiler.isPrivateIdentifier(member.name))
    ) {
      continue;
    }
    if (compiler.isPropertyDeclaration(member)) {
      members.push(
        compiler.factory.updatePropertyDeclaration(
          member,
          declarationModifiers(compiler, member),
          member.name,
          member.questionToken,
          member.type ?? inferredInitializerType(compiler, member.initializer),
          undefined,
        ),
      );
    } else if (compiler.isMethodDeclaration(member)) {
      members.push(
        compiler.factory.updateMethodDeclaration(
          member,
          declarationModifiers(compiler, member),
          member.asteriskToken,
          member.name,
          member.questionToken,
          member.typeParameters,
          member.parameters.map((parameter) =>
            declarationParameter(compiler, parameter),
          ),
          member.type ?? inferredBodyType(compiler, member.body),
          undefined,
        ),
      );
    } else if (compiler.isConstructorDeclaration(member)) {
      members.push(
        compiler.factory.updateConstructorDeclaration(
          member,
          declarationModifiers(compiler, member),
          member.parameters.map((parameter) =>
            declarationParameter(compiler, parameter),
          ),
          undefined,
        ),
      );
    } else if (compiler.isGetAccessorDeclaration(member)) {
      members.push(
        compiler.factory.updateGetAccessorDeclaration(
          member,
          declarationModifiers(compiler, member),
          member.name,
          member.parameters.map((parameter) =>
            declarationParameter(compiler, parameter),
          ),
          member.type ?? inferredBodyType(compiler, member.body),
          undefined,
        ),
      );
    } else if (compiler.isSetAccessorDeclaration(member)) {
      members.push(
        compiler.factory.updateSetAccessorDeclaration(
          member,
          declarationModifiers(compiler, member),
          member.name,
          member.parameters.map((parameter) =>
            declarationParameter(compiler, parameter),
          ),
          undefined,
        ),
      );
    } else if (compiler.isIndexSignatureDeclaration(member)) {
      members.push(
        compiler.factory.updateIndexSignature(
          member,
          declarationModifiers(compiler, member),
          member.parameters.map((parameter) =>
            declarationParameter(compiler, parameter),
          ),
          member.type,
        ),
      );
    }
  }
  return compiler.factory.updateClassDeclaration(
    statement,
    declarationModifiers(compiler, statement, { ambient: true }),
    statement.name,
    statement.typeParameters,
    statement.heritageClauses,
    members,
  );
}

function declarationEnum(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.EnumDeclaration,
): ts.EnumDeclaration {
  const members = statement.members.map((member) => {
    const initializer = member.initializer;
    const safeInitializer =
      initializer &&
      (compiler.isStringLiteralLike(initializer) ||
        compiler.isNumericLiteral(initializer) ||
        (compiler.isPrefixUnaryExpression(initializer) &&
          compiler.isNumericLiteral(initializer.operand)))
        ? initializer
        : undefined;
    return compiler.factory.updateEnumMember(
      member,
      member.name,
      safeInitializer,
    );
  });
  return compiler.factory.updateEnumDeclaration(
    statement,
    declarationModifiers(compiler, statement, { ambient: true }),
    statement.name,
    members,
  );
}

function declarationFunction(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.FunctionDeclaration,
): ts.FunctionDeclaration {
  return compiler.factory.updateFunctionDeclaration(
    statement,
    declarationModifiers(compiler, statement, { ambient: true }),
    statement.asteriskToken,
    statement.name,
    statement.typeParameters,
    statement.parameters.map((parameter) =>
      declarationParameter(compiler, parameter),
    ),
    statement.type ?? inferredBodyType(compiler, statement.body),
    undefined,
  );
}

function declarationVariables(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.VariableStatement,
): ts.VariableStatement | undefined {
  const declarations = statement.declarationList.declarations.flatMap(
    (declaration) => {
      if (!compiler.isIdentifier(declaration.name)) return [];
      return [
        compiler.factory.updateVariableDeclaration(
          declaration,
          declaration.name,
          declaration.exclamationToken,
          declaration.type ??
            inferredInitializerType(compiler, declaration.initializer),
          undefined,
        ),
      ];
    },
  );
  if (declarations.length === 0) return undefined;
  return compiler.factory.updateVariableStatement(
    statement,
    declarationModifiers(compiler, statement, { ambient: true }),
    compiler.factory.updateVariableDeclarationList(
      statement.declarationList,
      declarations,
    ),
  );
}

function declarationModule(
  compiler: typeof import("@typescript/typescript6"),
  statement: ts.ModuleDeclaration,
  namespaceDeclarations?: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >,
): ts.ModuleDeclaration {
  const body = statement.body;
  const selected = namespaceDeclarations?.get(statement);
  const projectedBody: ts.ModuleBody | undefined =
    body && compiler.isModuleBlock(body)
      ? compiler.factory.updateModuleBlock(
          body,
          (
            selected ??
            body.statements.filter((child) =>
              isModelTypeDeclaration(compiler, child),
            )
          ).flatMap((child) => {
            const declaration = declarationStatement(
              compiler,
              child,
              namespaceDeclarations,
            );
            return declaration ? [declaration] : [];
          }),
        )
      : body && compiler.isModuleDeclaration(body)
        ? selected === undefined || selected.includes(body)
          ? (declarationModule(
              compiler,
              body,
              namespaceDeclarations,
            ) as ts.NamespaceDeclaration)
          : compiler.factory.createModuleBlock([])
        : body;
  return compiler.factory.updateModuleDeclaration(
    statement,
    declarationModifiers(compiler, statement, { ambient: true }),
    statement.name,
    projectedBody,
  );
}

function declarationStatement(
  compiler: typeof import("@typescript/typescript6"),
  statement: ModelTypeDeclaration,
  namespaceDeclarations?: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >,
): ts.Statement | undefined {
  return compiler.isClassDeclaration(statement)
    ? declarationClass(compiler, statement)
    : compiler.isEnumDeclaration(statement)
      ? declarationEnum(compiler, statement)
      : compiler.isFunctionDeclaration(statement)
        ? declarationFunction(compiler, statement)
        : compiler.isVariableStatement(statement)
          ? declarationVariables(compiler, statement)
          : compiler.isModuleDeclaration(statement)
            ? declarationModule(compiler, statement, namespaceDeclarations)
            : statement;
}

function declarationText(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  statement: ts.Statement,
  namespaceDeclarations?: ReadonlyMap<
    ts.ModuleDeclaration,
    readonly ModelTypeDeclaration[]
  >,
): string {
  const declaration = isModelTypeDeclaration(compiler, statement)
    ? declarationStatement(compiler, statement, namespaceDeclarations)
    : statement;
  if (!declaration) return "";
  return compiler
    .createPrinter({ newLine: compiler.NewLineKind.LineFeed })
    .printNode(compiler.EmitHint.Unspecified, declaration, sourceFile)
    .trim();
}

function modelTypeChunks(
  compiler: typeof import("@typescript/typescript6"),
  path: string,
  source: string,
  selectedNames: ReadonlySet<string>,
  requestedRootNames: ReadonlySet<string>,
  declarationEmitted: boolean,
): readonly ModelTypeChunk[] {
  const sourceFile = compiler.createSourceFile(
    path,
    source,
    compiler.ScriptTarget.Latest,
    true,
    /\.[cm]?tsx$/.test(path) ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
  );
  const selection = selectModelTypeDeclarations(
    compiler,
    sourceFile,
    selectedNames,
  );
  const requestedRoots = new Set(
    selectModelTypeDeclarations(compiler, sourceFile, requestedRootNames)
      .requested,
  );
  const requested = selection.requested.filter((statement) =>
    requestedRoots.has(statement),
  );
  const supporting = [
    ...selection.requested.filter(
      (statement) => !requestedRoots.has(statement),
    ),
    ...selection.supporting,
  ];
  const referencedImportNames = selection.references;
  function referencesName(
    references: ReadonlySet<string>,
    name: string,
  ): boolean {
    return [...references].some((reference) => {
      const path = modelTypeRequestPath(reference);
      return path === name || path.startsWith(`${name}.`);
    });
  }
  const imports = sourceFile.statements.filter((statement) => {
    if (compiler.isImportEqualsDeclaration(statement)) {
      return referencesName(referencedImportNames, statement.name.text);
    }
    if (compiler.isImportDeclaration(statement)) {
      if (!declarationEmitted && statement.importClause?.isTypeOnly !== true) {
        return false;
      }
      const clause = statement.importClause;
      if (!clause) return false;
      if (
        clause.name &&
        referencesName(referencedImportNames, clause.name.text)
      ) {
        return true;
      }
      const bindings = clause.namedBindings;
      if (bindings && compiler.isNamespaceImport(bindings)) {
        return referencesName(referencedImportNames, bindings.name.text);
      }
      return (
        bindings !== undefined &&
        compiler.isNamedImports(bindings) &&
        bindings.elements.some((element) =>
          referencesName(referencedImportNames, element.name.text),
        )
      );
    }
    if (
      !compiler.isExportDeclaration(statement) ||
      statement.moduleSpecifier ||
      !statement.exportClause ||
      compiler.isNamespaceExport(statement.exportClause)
    ) {
      return false;
    }
    return statement.exportClause.elements.some((element) => {
      const localName = element.propertyName?.text ?? element.name.text;
      return (
        [...selectedNames].some(
          (request) => modelTypeRequestPath(request) === "*",
        ) ||
        referencesName(selectedNames, element.name.text) ||
        referencesName(selectedNames, localName)
      );
    });
  });
  return [
    ...requested.map((statement) => ({
      path,
      priority: "requested" as const,
      order: statement.getStart(sourceFile),
      source: declarationText(
        compiler,
        sourceFile,
        statement,
        selection.namespaceDeclarations,
      ),
    })),
    ...[...imports, ...supporting].map((statement) => ({
      path,
      priority: "supporting" as const,
      order: statement.getStart(sourceFile),
      source: declarationText(
        compiler,
        sourceFile,
        statement,
        selection.namespaceDeclarations,
      ),
    })),
  ].filter((chunk) => chunk.source.length > 0);
}

function utf8Length(value: string): number {
  return Buffer.byteLength(value, "utf8");
}

function boundedModelTypeSources(
  root: string,
  chunks: readonly ModelTypeChunk[],
): readonly ModelTypeSource[] {
  const ordered = [...chunks].sort((left, right) => {
    const byPriority =
      (left.priority === "requested" ? 0 : 1) -
      (right.priority === "requested" ? 0 : 1);
    return (
      byPriority ||
      compareCodeUnits(left.path, right.path) ||
      left.order - right.order ||
      compareCodeUnits(left.source, right.source)
    );
  });
  const allGroups = new Map<string, ModelTypeChunk[]>();
  for (const chunk of ordered) {
    const key = `${chunk.priority}\0${chunk.path}`;
    const values = allGroups.get(key) ?? [];
    values.push(chunk);
    allGroups.set(key, values);
  }
  const allBytes = [...allGroups.values()].reduce(
    (total, values) =>
      total + utf8Length(`${values.map((item) => item.source).join("\n\n")}\n`),
    0,
  );
  const omissionTemplate = `// Jaunt omitted ${ordered.length} type-context chunks to stay within ${MODEL_TYPE_CONTEXT_LIMIT} UTF-8 bytes.\n`;
  const available =
    allBytes <= MODEL_TYPE_CONTEXT_LIMIT
      ? MODEL_TYPE_CONTEXT_LIMIT
      : MODEL_TYPE_CONTEXT_LIMIT - utf8Length(omissionTemplate);
  const selected = new Map<string, ModelTypeChunk[]>();
  let used = 0;
  let omitted = 0;
  for (const chunk of ordered) {
    const key = `${chunk.priority}\0${chunk.path}`;
    const values = selected.get(key) ?? [];
    const addition = `${values.length > 0 ? "\n\n" : ""}${chunk.source}${
      values.length === 0 ? "\n" : ""
    }`;
    if (used + utf8Length(addition) > available) {
      omitted += 1;
      continue;
    }
    values.push(chunk);
    selected.set(key, values);
    used += utf8Length(addition);
  }
  const sources = [...selected.values()].map((values) => ({
    id: stablePathId(root, values[0]!.path),
    priority: values[0]!.priority,
    source: `${values.map((item) => item.source).join("\n\n")}\n`,
  }));
  if (omitted > 0) {
    sources.push({
      id: "workspace:.jaunt/type-context-omissions.ts",
      priority: "supporting",
      source: `// Jaunt omitted ${omitted} type-context chunks to stay within ${MODEL_TYPE_CONTEXT_LIMIT} UTF-8 bytes.\n`,
    });
  }
  return sources;
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
  const compatibilityIgnoredIds = new Set<string>();
  const toolingRecords: SemanticEnvironmentRecord[] = [];
  const proseRecords: ImportedDocsRecord[] = [];
  const modelTypeSources: ModelTypeSource[] = [];
  const inputPaths = new Set<string>();
  const visited = new Set<string>();
  const pending: { containingFile: string; specifier: string }[] =
    moduleSpecifiers(compiler, module.sourceFile).map((specifier) => ({
      containingFile: module.sourceFile.fileName,
      specifier,
    }));

  const requestedModelTypes = new Map<string, Map<string, ModelTypePriority>>();
  const processedModelTypes = new Map<string, Map<string, ModelTypePriority>>();
  const modelSourceFiles = new Map<
    string,
    {
      readonly source: string;
      readonly names: Set<string>;
      readonly requestedNames: Set<string>;
    }
  >();

  function priorityRank(priority: ModelTypePriority): number {
    return priority === "requested" ? 0 : 1;
  }

  function priorityCovers(
    processed: ReadonlyMap<string, ModelTypePriority> | undefined,
    name: string,
    priority: ModelTypePriority,
  ): boolean {
    return [...(processed ?? [])].some(([processedName, candidate]) => {
      const processedNamespace = modelTypeRequestNamespace(processedName);
      const requestedNamespace = modelTypeRequestNamespace(name);
      const namespaceCovered =
        processedNamespace === "both" ||
        processedNamespace === requestedNamespace;
      const processedPath = modelTypeRequestPath(processedName);
      const requestedPath = modelTypeRequestPath(name);
      return (
        namespaceCovered &&
        (processedPath === "*" ||
          processedPath === requestedPath ||
          requestedPath.startsWith(`${processedPath}.`)) &&
        priorityRank(candidate) <= priorityRank(priority)
      );
    });
  }

  function mergePriority(
    values: Map<string, ModelTypePriority>,
    name: string,
    priority: ModelTypePriority,
  ): void {
    const previous = values.get(name);
    if (
      previous === undefined ||
      priorityRank(priority) < priorityRank(previous)
    ) {
      values.set(name, priority);
    }
  }

  function enqueueModelTypeImport(
    containingFile: string,
    item: DirectTypeImport,
    priority: ModelTypePriority,
  ): void {
    const resolution = compiler.resolveModuleName(
      item.specifier,
      containingFile,
      compilerOptions,
      compiler.sys,
      undefined,
      undefined,
      resolutionMode(compiler, containingFile, compilerOptions),
    ).resolvedModule;
    if (!resolution) return;
    const path = workspaceModelTypePath(root, resolution.resolvedFileName);
    if (!path) return;
    const names =
      requestedModelTypes.get(path) ?? new Map<string, ModelTypePriority>();
    for (const name of item.names) mergePriority(names, name, priority);
    requestedModelTypes.set(path, names);
  }

  for (const item of directTypeImports(
    compiler,
    module.sourceFile,
    compilerOptions,
    [
      ...module.symbols.flatMap<ModelTypeDeclaration>((symbol) =>
        symbol.kind === "class" ? [symbol.declaration] : symbol.declarations,
      ),
      ...module.typeDeclarations,
    ],
  )) {
    enqueueModelTypeImport(module.sourceFile.fileName, item, "requested");
  }

  while (true) {
    const path = [...requestedModelTypes.keys()].sort().find((candidate) => {
      const requested = requestedModelTypes.get(candidate);
      const processed = processedModelTypes.get(candidate);
      if (!requested) return false;
      return [...requested].some(
        ([name, priority]) => !priorityCovers(processed, name, priority),
      );
    });
    if (!path) break;

    const requested = requestedModelTypes.get(path) ?? new Map();
    const processed = processedModelTypes.get(path) ?? new Map();
    const pendingRequests = new Map(
      [...requested].filter(
        ([name, priority]) => !priorityCovers(processed, name, priority),
      ),
    );
    for (const [name, priority] of pendingRequests) {
      mergePriority(processed, name, priority);
    }
    processedModelTypes.set(path, processed);

    try {
      const source = readFileSync(path, "utf8");
      const sourceFile = compiler.createSourceFile(
        path,
        source,
        compiler.ScriptTarget.Latest,
        true,
        /\.[cm]?tsx$/.test(path)
          ? compiler.ScriptKind.TSX
          : compiler.ScriptKind.TS,
      );
      const requestedNames = new Set(
        [...pendingRequests]
          .filter(([, priority]) => priority === "requested")
          .map(([name]) => name),
      );
      const supportingNames = new Set(
        [...pendingRequests]
          .filter(([, priority]) => priority === "supporting")
          .map(([name]) => name),
      );
      const expandedRequestedNames = locallyAliasedTypeNames(
        compiler,
        sourceFile,
        requestedNames,
      );
      const expandedSupportingNames = locallyAliasedTypeNames(
        compiler,
        sourceFile,
        supportingNames,
      );
      const expandedNames = new Set([
        ...expandedRequestedNames,
        ...expandedSupportingNames,
      ]);
      const selection = selectModelTypeDeclarations(
        compiler,
        sourceFile,
        expandedNames,
      );
      if (selection.requested.length > 0) {
        const entry = modelSourceFiles.get(path) ?? {
          source,
          names: new Set<string>(),
          requestedNames: new Set<string>(),
        };
        for (const name of expandedNames) entry.names.add(name);
        for (const name of expandedRequestedNames) {
          entry.requestedNames.add(name);
        }
        modelSourceFiles.set(path, entry);

        // Follow only imports referenced by the projected declaration closure.
        // Declaration emit captures inferred value types without exposing the
        // initializers or function bodies that produced them.
        const emitted = emittedDeclarationSource(path, source);
        const emittedSourceFile = compiler.createSourceFile(
          path,
          emitted.source,
          compiler.ScriptTarget.Latest,
          true,
          /\.[cm]?tsx$/.test(path)
            ? compiler.ScriptKind.TSX
            : compiler.ScriptKind.TS,
        );
        const emittedSelection = selectModelTypeDeclarations(
          compiler,
          emittedSourceFile,
          expandedNames,
        );
        const dependencySourceFile =
          emittedSelection.requested.length > 0
            ? emittedSourceFile
            : sourceFile;
        const dependencySelection =
          emittedSelection.requested.length > 0 ? emittedSelection : selection;
        for (const item of modelTypeDependencyImports(
          compiler,
          dependencySourceFile,
          dependencySelection,
        )) {
          enqueueModelTypeImport(path, item, "supporting");
        }
      }
      for (const item of forwardedTypeImports(
        compiler,
        sourceFile,
        expandedRequestedNames,
      )) {
        enqueueModelTypeImport(path, item, "requested");
      }
      for (const item of forwardedTypeImports(
        compiler,
        sourceFile,
        expandedSupportingNames,
      )) {
        enqueueModelTypeImport(path, item, "supporting");
      }
    } catch {
      // The normal environment traversal below owns unreadable-input
      // diagnostics. Model context remains best-effort and non-authoritative.
    }
  }

  function emittedDeclarationSource(
    path: string,
    fallback: string,
  ): { readonly source: string; readonly emitted: boolean } {
    if (/\.d\.[cm]?ts$/.test(path)) return { source: fallback, emitted: true };
    try {
      const output = compiler.transpileDeclaration(fallback, {
        fileName: path,
        compilerOptions: {
          ...compilerOptions,
          noEmit: false,
          declaration: true,
          declarationMap: false,
          emitDeclarationOnly: true,
          noEmitOnError: false,
          sourceMap: false,
        },
        reportDiagnostics: true,
      }).outputText;
      if (output.trim() !== "") {
        return { source: output, emitted: true };
      }
    } catch {
      // Syntactic declaration sanitization below remains a safe fallback.
    }
    return { source: fallback, emitted: false };
  }

  const modelChunks: ModelTypeChunk[] = [];
  for (const [path, entry] of [...modelSourceFiles].sort(([left], [right]) =>
    compareCodeUnits(left, right),
  )) {
    const declaration = emittedDeclarationSource(path, entry.source);
    modelChunks.push(
      ...modelTypeChunks(
        compiler,
        path,
        declaration.source,
        entry.names,
        entry.requestedNames,
        declaration.emitted,
      ),
    );
  }
  modelTypeSources.push(...boundedModelTypeSources(root, modelChunks));

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
    toolingRecords.push(...toolingProvenanceRecords(root, path, syntax));
    if (LOCK_FILES.has(basename(path))) {
      compatibilityIgnoredIds.add(id);
      continue;
    }
    compatibleEnvironmentSyntax.set(id, normalizeToolingMetadata(syntax));
  }

  records.sort((left, right) => {
    const byId = compareCodeUnits(left.id, right.id);
    return (
      byId ||
      compareCodeUnits(
        digestCanonical(left.syntax),
        digestCanonical(right.syntax),
      )
    );
  });
  const sortedProseRecords = proseRecords.sort((left, right) =>
    compareCodeUnits(left.id, right.id),
  );
  const rawCompatibilityRecords = records
    .filter((record) => !compatibilityIgnoredIds.has(record.id))
    .map((record) => {
      const syntax = record.id.startsWith("environment:")
        ? (compatibleEnvironmentSyntax.get(record.id) ??
          normalizeToolingMetadata(record.syntax))
        : record.syntax;
      return { id: record.id, digest: digestCanonical(syntax) };
    });
  const compatibilityRecords = groupSemanticEnvironmentRecords(
    rawCompatibilityRecords,
  );
  const groupedToolingRecords = groupSemanticEnvironmentRecords(toolingRecords);
  return {
    digest: digestCanonical(records),
    compatibilityDigest: digestCanonical(compatibilityRecords),
    compatibilityRecords,
    toolingRecords: groupedToolingRecords,
    proseDigest: digestCanonical(sortedProseRecords),
    proseRecords: sortedProseRecords,
    modelTypeSources,
    inputPaths: [...inputPaths].sort(),
  };
}
