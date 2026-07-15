import type ts from "@typescript/typescript6";
import { resolve } from "node:path";
import type { DiagnosticRecord } from "./types.js";
import type { ContractModuleIR } from "./ir.js";
import { serializeType } from "./ir.js";
import { diagnosticAt, sortDiagnostics } from "./diagnostics.js";
import {
  relativeModuleSpecifier,
  renderClassTypeAlias,
  renderDocs,
  renderTypeImport,
} from "./mirror.js";
import {
  auditPackageImport,
  type PackageImportResolution,
} from "./provenance.js";
import { canonicalJson } from "./canonical.js";

export interface CompositionResult {
  readonly source: string;
  /** Candidate with deterministic imports and preserved bodies applied. */
  readonly candidateSource: string;
  readonly diagnostics: readonly DiagnosticRecord[];
}

interface TripleSlashDirectiveRange {
  readonly start: number;
  readonly end: number;
}

/**
 * Return every TypeScript triple-slash directive retained by a generated
 * candidate. TypeScript exposes reference directives on separate public
 * arrays, but AMD directives and `no-default-lib` are not represented there
 * consistently across the supported compiler versions. Scanning only
 * directive-shaped line comments keeps the policy independent of that
 * compiler-version split while leaving ordinary `///` documentation alone.
 */
function tripleSlashDirectiveRanges(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly TripleSlashDirectiveRange[] {
  const ranges: TripleSlashDirectiveRange[] = [];
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
    ranges.push({ start: scanner.getTokenPos(), end: scanner.getTextPos() });
  }
  return ranges;
}

function declaredNames(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): Set<string> {
  const names = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (
      (compiler.isFunctionDeclaration(statement) ||
        compiler.isClassDeclaration(statement)) &&
      statement.name
    ) {
      names.add(statement.name.text);
    }
    if (compiler.isVariableStatement(statement)) {
      for (const declaration of statement.declarationList.declarations) {
        if (compiler.isIdentifier(declaration.name))
          names.add(declaration.name.text);
      }
    }
  }
  return names;
}

function memberName(
  compiler: typeof import("@typescript/typescript6"),
  member: ts.ClassElement,
): string | undefined {
  const name = member.name;
  if (!name)
    return compiler.isConstructorDeclaration(member)
      ? "constructor"
      : undefined;
  if (
    compiler.isIdentifier(name) ||
    compiler.isStringLiteral(name) ||
    compiler.isNumericLiteral(name)
  ) {
    return name.text;
  }
  if (compiler.isPrivateIdentifier(name)) return `#${name.text}`;
  return undefined;
}

function candidateClass(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  name: string,
): ts.ClassLikeDeclaration | undefined {
  for (const statement of sourceFile.statements) {
    if (compiler.isClassDeclaration(statement) && statement.name?.text === name)
      return statement;
    if (!compiler.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (
        compiler.isIdentifier(declaration.name) &&
        declaration.name.text === name &&
        declaration.initializer &&
        compiler.isClassExpression(declaration.initializer)
      ) {
        return declaration.initializer;
      }
    }
  }
  return undefined;
}

function hasFreshFunctionBinding(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  name: string,
): boolean {
  for (const statement of sourceFile.statements) {
    if (
      compiler.isFunctionDeclaration(statement) &&
      statement.name?.text === name
    ) {
      return true;
    }
    if (!compiler.isVariableStatement(statement)) continue;
    for (const declaration of statement.declarationList.declarations) {
      if (
        !compiler.isIdentifier(declaration.name) ||
        declaration.name.text !== name ||
        !declaration.initializer
      ) {
        continue;
      }
      let initializer = declaration.initializer;
      while (compiler.isParenthesizedExpression(initializer)) {
        initializer = initializer.expression;
      }
      return (
        compiler.isArrowFunction(initializer) ||
        compiler.isFunctionExpression(initializer)
      );
    }
  }
  return false;
}

function validateClassSurface(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceFile: ts.SourceFile,
  ir: ContractModuleIR,
  diagnostics: DiagnosticRecord[],
): void {
  for (const symbol of ir.symbols.filter((item) => item.kind === "class")) {
    const declaration = candidateClass(
      compiler,
      sourceFile,
      `__jaunt_impl_${symbol.name}`,
    );
    if (!declaration) continue;
    const extendsClause = declaration.heritageClauses?.find(
      (clause) => clause.token === compiler.SyntaxKind.ExtendsKeyword,
    );
    const implementsClause = declaration.heritageClauses?.find(
      (clause) => clause.token === compiler.SyntaxKind.ImplementsKeyword,
    );
    const actualBase = extendsClause?.types[0];
    const expectedBase = symbol.heritage;
    const heritageMatches =
      expectedBase === undefined
        ? extendsClause === undefined
        : extendsClause?.types.length === 1 &&
          actualBase !== undefined &&
          compiler.isIdentifier(actualBase.expression) &&
          actualBase.expression.text === expectedBase.implementationName &&
          canonicalJson(
            (actualBase.typeArguments ?? []).map((argument) =>
              serializeType(compiler, argument),
            ),
          ) === canonicalJson(expectedBase.typeArguments);
    if (!heritageMatches || implementsClause) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          declaration,
          "JAUNT_TS_CANDIDATE_HERITAGE",
          `Generated class ${symbol.name} must use the authored concrete heritage exactly`,
        ),
      );
    }
    const declarationModifiers = compiler.canHaveModifiers(declaration)
      ? compiler.getModifiers(declaration)
      : undefined;
    if (
      declarationModifiers?.some(
        (modifier) => modifier.kind === compiler.SyntaxKind.AbstractKeyword,
      )
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          declaration,
          "JAUNT_TS_CANDIDATE_ABSTRACT",
          "Generated governed classes must remain concrete",
        ),
      );
    }
    const authored = new Map(
      symbol.members.map((member) => [
        `${member.static ? "static" : "instance"}:${member.kind}:${member.name}`,
        member,
      ]),
    );
    const seen = new Set<string>();
    for (const member of declaration.members) {
      const name = memberName(compiler, member);
      if (!name || name.startsWith("#")) continue;
      const modifiers = compiler.canHaveModifiers(member)
        ? compiler.getModifiers(member)
        : undefined;
      if (
        modifiers?.some(
          (modifier) =>
            modifier.kind === compiler.SyntaxKind.PrivateKeyword ||
            modifier.kind === compiler.SyntaxKind.ProtectedKeyword,
        )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            member,
            "JAUNT_TS_CANDIDATE_NOMINAL_MEMBER",
            "Generated classes may use #private state, not TypeScript private/protected members",
          ),
        );
        continue;
      }
      const kind = compiler.isConstructorDeclaration(member)
        ? "constructor"
        : compiler.isGetAccessorDeclaration(member)
          ? "getter"
          : compiler.isSetAccessorDeclaration(member)
            ? "setter"
            : compiler.isPropertyDeclaration(member)
              ? "property"
              : compiler.isMethodDeclaration(member)
                ? "method"
                : "unsupported";
      const staticMember =
        modifiers?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.StaticKeyword,
        ) ?? false;
      const key = `${staticMember ? "static" : "instance"}:${kind}:${name}`;
      const expected = authored.get(key);
      if (!expected) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            member,
            "JAUNT_TS_EXTRA_PUBLIC_MEMBER",
            `Generated class ${symbol.name} has undeclared public member ${name}`,
          ),
        );
        continue;
      }
      seen.add(key);
      if (
        expected.kind === "property" &&
        compiler.isPropertyDeclaration(member)
      ) {
        const candidateReadonly =
          modifiers?.some(
            (modifier) => modifier.kind === compiler.SyntaxKind.ReadonlyKeyword,
          ) ?? false;
        if (candidateReadonly !== expected.readonly) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_MEMBER_MODIFIER",
              `readonly modifier for ${symbol.name}.${name} does not match the spec`,
            ),
          );
        }
      }
      const candidateOptional =
        (compiler.isPropertyDeclaration(member) ||
          compiler.isMethodDeclaration(member)) &&
        member.questionToken !== undefined;
      if (candidateOptional !== expected.optional) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            member,
            "JAUNT_TS_MEMBER_MODIFIER",
            `optional modifier for ${symbol.name}.${name} does not match the spec`,
          ),
        );
      }
    }
    for (const [key, member] of authored) {
      if (!seen.has(key) && !member.inheritedConstructor) {
        diagnostics.push({
          code: "JAUNT_TS_MISSING_PUBLIC_MEMBER",
          severity: "error",
          message: `Generated class ${symbol.name} is missing ${member.kind} ${member.name}`,
          path: ir.implementationPath,
        });
      }
    }
  }
}

function classElementKey(
  compiler: typeof import("@typescript/typescript6"),
  member: ts.ClassElement,
): string | undefined {
  const name = memberName(compiler, member);
  if (!name) return undefined;
  const modifiers = compiler.canHaveModifiers(member)
    ? compiler.getModifiers(member)
    : undefined;
  const staticMember =
    modifiers?.some(
      (modifier) => modifier.kind === compiler.SyntaxKind.StaticKeyword,
    ) ?? false;
  const kind = compiler.isGetAccessorDeclaration(member)
    ? "getter"
    : compiler.isSetAccessorDeclaration(member)
      ? "setter"
      : compiler.isMethodDeclaration(member)
        ? "method"
        : undefined;
  return kind
    ? `${staticMember ? "static" : "instance"}:${kind}:${name}`
    : undefined;
}

function applyPreservedBodies(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  ir: ContractModuleIR,
  sourceFile: ts.SourceFile,
  candidate: string,
  diagnostics: DiagnosticRecord[],
): string {
  const edits: { start: number; end: number; content: string }[] = [];
  for (const symbol of ir.symbols.filter((item) => item.kind === "class")) {
    const declaration = candidateClass(
      compiler,
      sourceFile,
      `__jaunt_impl_${symbol.name}`,
    );
    if (!declaration) continue;
    for (const member of symbol.members.filter((item) => item.preserved)) {
      const key = `${member.static ? "static" : "instance"}:${member.kind}:${member.name}`;
      const matches = declaration.members.filter(
        (candidateMember) =>
          classElementKey(compiler, candidateMember) === key &&
          (compiler.isMethodDeclaration(candidateMember) ||
            compiler.isGetAccessorDeclaration(candidateMember) ||
            compiler.isSetAccessorDeclaration(candidateMember)) &&
          candidateMember.body !== undefined,
      ) as (
        | ts.MethodDeclaration
        | ts.GetAccessorDeclaration
        | ts.SetAccessorDeclaration
      )[];
      if (matches.length !== 1 || !member.preservedBody) {
        diagnostics.push({
          code: "JAUNT_TS_PRESERVE_CANDIDATE",
          severity: "error",
          message: `Generated class ${symbol.name} must contain exactly one concrete ${member.kind} ${member.name} for @jauntPreserve body insertion`,
          path: ir.implementationPath,
        });
        continue;
      }
      const body = matches[0]!.body!;
      const memberStart = matches[0]!.getStart(sourceFile);
      if (member.docs) {
        const lineStart = candidate.lastIndexOf("\n", memberStart - 1) + 1;
        const indent = candidate.slice(lineStart, memberStart);
        const renderedDocs = renderDocs(member.docs, indent);
        edits.push({
          start: memberStart,
          end: memberStart,
          content: `${renderedDocs.startsWith(indent) ? renderedDocs.slice(indent.length) : renderedDocs}${indent}`,
        });
      }
      edits.push({
        start: body.getStart(sourceFile),
        end: body.end,
        content: member.preservedBody,
      });
    }
  }
  let output = candidate;
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    output = `${output.slice(0, edit.start)}${edit.content}${output.slice(edit.end)}`;
  }
  return output;
}

function importLocalNames(
  compiler: typeof import("@typescript/typescript6"),
  declaration: ts.ImportDeclaration,
): readonly string[] {
  const clause = declaration.importClause;
  if (!clause) return [];
  return [
    ...(clause.name ? [clause.name.text] : []),
    ...(clause.namedBindings && compiler.isNamespaceImport(clause.namedBindings)
      ? [clause.namedBindings.name.text]
      : []),
    ...(clause.namedBindings && compiler.isNamedImports(clause.namedBindings)
      ? clause.namedBindings.elements.map((element) => element.name.text)
      : []),
  ];
}

function injectRuntimeImports(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  ir: ContractModuleIR,
  sourceFile: ts.SourceFile,
  candidate: string,
  diagnostics: DiagnosticRecord[],
): string {
  const provided = new Map<string, string>();
  for (const statement of sourceFile.statements) {
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier)
    )
      continue;
    for (const name of importLocalNames(compiler, statement)) {
      provided.set(name, statement.moduleSpecifier.text);
    }
  }
  const declared = declaredNames(compiler, sourceFile);
  const imports: string[] = [];
  for (const item of ir.typeImports.filter(
    (candidateImport) => candidateImport.runtime,
  )) {
    const names = [
      ...(item.defaultImport ? [item.defaultImport] : []),
      ...(item.namespaceImport ? [item.namespaceImport] : []),
      ...item.namedImports.map((binding) => binding.local),
    ];
    const matching = names.filter(
      (name) => provided.get(name) === item.specifier,
    );
    if (matching.length === names.length) continue;
    const conflicts = names.filter(
      (name) =>
        declared.has(name) ||
        (provided.has(name) && provided.get(name) !== item.specifier),
    );
    if (matching.length > 0 || conflicts.length > 0 || item.typeOnly) {
      diagnostics.push({
        code: "JAUNT_TS_REQUIRED_IMPORT_COLLISION",
        severity: "error",
        message: item.typeOnly
          ? `Runtime class/preserve binding from ${item.specifier} was imported with import type`
          : `Candidate conflicts with deterministic runtime import binding(s): ${[...new Set([...matching, ...conflicts])].sort().join(", ")}`,
        path: ir.implementationPath,
      });
      continue;
    }
    imports.push(renderTypeImport(item, true));
  }
  return imports.length ? `${imports.join("\n")}\n${candidate}` : candidate;
}

function renderedBoundary(ir: ContractModuleIR): readonly string[] {
  return ir.symbols.flatMap((symbol) => [
    ...(symbol.kind === "function"
      ? [
          `Object.defineProperty(__jaunt_impl_${symbol.name}, "name", { value: ${JSON.stringify(symbol.name)}, configurable: true });`,
        ]
      : []),
    `${renderDocs(symbol.docs)}export const ${symbol.name}: typeof __JauntApi.${symbol.name} = __jaunt_impl_${symbol.name};`,
    ...(symbol.kind === "class" ? [renderClassTypeAlias(symbol)] : []),
  ]);
}

function renderedBoundaryStatements(ir: ContractModuleIR): readonly string[] {
  return ir.symbols.flatMap((symbol) => [
    ...(symbol.kind === "function"
      ? [
          `Object.defineProperty(__jaunt_impl_${symbol.name}, "name", { value: ${JSON.stringify(symbol.name)}, configurable: true });`,
        ]
      : []),
    `export const ${symbol.name}: typeof __JauntApi.${symbol.name} = __jaunt_impl_${symbol.name};`,
    ...(symbol.kind === "class" ? [renderClassTypeAlias(symbol)] : []),
  ]);
}

function maskPreservedBodiesForAudit(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  ir: ContractModuleIR,
  candidate: string,
): string {
  const path = resolve(root, ir.implementationPath);
  const sourceFile = compiler.createSourceFile(
    path,
    candidate,
    compiler.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
  );
  const edits: { start: number; end: number }[] = [];
  for (const symbol of ir.symbols.filter((item) => item.kind === "class")) {
    const declaration = candidateClass(
      compiler,
      sourceFile,
      `__jaunt_impl_${symbol.name}`,
    );
    if (!declaration) continue;
    for (const member of symbol.members.filter((item) => item.preserved)) {
      const key = `${member.static ? "static" : "instance"}:${member.kind}:${member.name}`;
      const matches = declaration.members.filter(
        (candidateMember) =>
          classElementKey(compiler, candidateMember) === key &&
          (compiler.isMethodDeclaration(candidateMember) ||
            compiler.isGetAccessorDeclaration(candidateMember) ||
            compiler.isSetAccessorDeclaration(candidateMember)) &&
          candidateMember.body !== undefined,
      ) as (
        | ts.MethodDeclaration
        | ts.GetAccessorDeclaration
        | ts.SetAccessorDeclaration
      )[];
      if (matches.length !== 1) continue;
      const body = matches[0]!.body!;
      edits.push({ start: body.getStart(sourceFile), end: body.end });
    }
  }
  let output = candidate;
  for (const edit of edits.sort((left, right) => right.start - left.start)) {
    output = `${output.slice(0, edit.start)}{}${output.slice(edit.end)}`;
  }
  return output;
}

/** Re-run current candidate policy over the model-authored portion of built output. */
export function auditBuiltImplementationPolicy(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  ir: ContractModuleIR,
  source: string,
  resolvePackageImport: (
    specifier: string,
  ) => PackageImportResolution | undefined = () => undefined,
): readonly DiagnosticRecord[] {
  const path = resolve(root, ir.implementationPath);
  const sourceFile = compiler.createSourceFile(
    path,
    source,
    compiler.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
  );
  const api = relativeModuleSpecifier(ir.implementationPath, ir.apiMirrorPath);
  const expectedHeaderImport = `import type * as __JauntApi from ${JSON.stringify(api)};`;
  const firstStatement = sourceFile.statements[0];
  const expectedBoundary = renderedBoundaryStatements(ir);
  const boundaryStatements = sourceFile.statements.slice(
    -expectedBoundary.length,
  );
  if (
    !firstStatement ||
    source.slice(firstStatement.getStart(sourceFile), firstStatement.end) !==
      expectedHeaderImport ||
    boundaryStatements.length !== expectedBoundary.length ||
    boundaryStatements.some(
      (statement, index) =>
        source.slice(statement.getStart(sourceFile), statement.end) !==
        expectedBoundary[index],
    )
  ) {
    return [
      {
        code: "JAUNT_TS_IMPLEMENTATION_PROVENANCE",
        severity: "error",
        message:
          "The existing built implementation does not have a canonical Jaunt header and boundary",
        path: ir.implementationPath,
      },
    ];
  }
  const candidateEnd = boundaryStatements[0]!.getFullStart();
  if (candidateEnd < firstStatement.end) {
    return [
      {
        code: "JAUNT_TS_IMPLEMENTATION_PROVENANCE",
        severity: "error",
        message:
          "The existing built implementation has an invalid Jaunt boundary",
        path: ir.implementationPath,
      },
    ];
  }
  const candidate = source.slice(firstStatement.end, candidateEnd).trim();
  const diagnostics = composeCandidate(
    compiler,
    root,
    ir,
    maskPreservedBodiesForAudit(compiler, root, ir, candidate),
    resolvePackageImport,
  ).diagnostics;
  if (/^\s*\/\/\s*@ts-(?:ignore|expect-error|nocheck)/m.test(source)) {
    return sortDiagnostics([
      ...diagnostics,
      {
        code: "JAUNT_TS_SUPPRESSION",
        severity: "error",
        message:
          "TypeScript suppression directives are forbidden in generated candidates",
        path,
      },
    ]);
  }
  return diagnostics;
}

export function composeCandidate(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  ir: ContractModuleIR,
  candidate: string,
  resolvePackageImport: (
    specifier: string,
  ) => PackageImportResolution | undefined = () => undefined,
): CompositionResult {
  const path = resolve(root, ir.implementationPath);
  const sourceFile = compiler.createSourceFile(
    path,
    candidate,
    compiler.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
  );
  const diagnostics: DiagnosticRecord[] = [];
  if (/^\s*\/\/\s*@ts-(?:ignore|expect-error|nocheck)/m.test(candidate)) {
    diagnostics.push({
      code: "JAUNT_TS_SUPPRESSION",
      severity: "error",
      message:
        "TypeScript suppression directives are forbidden in generated candidates",
      path,
    });
  }
  const requireAliases = new Set(["require"]);
  const createRequireAliases = new Set<string>();
  const moduleApiAliases = new Set<string>();
  const commonJsModuleAliases = new Set(["module"]);
  const reservedNames = new Set(
    ir.symbols.map((symbol) => `__jaunt_impl_${symbol.name}`),
  );
  const publicTypeNames = new Set([
    ...ir.typeDeclarations.map((declaration) => declaration.name),
    ...ir.symbols
      .filter((symbol) => symbol.kind === "class")
      .map((symbol) => symbol.name),
  ]);
  const unwrapExpression = (expression: ts.Expression): ts.Expression => {
    let current = expression;
    while (compiler.isParenthesizedExpression(current)) {
      current = current.expression;
    }
    return current;
  };
  const staticPropertyName = (
    expression: ts.Expression,
  ): string | undefined => {
    const current = unwrapExpression(expression);
    return compiler.isStringLiteral(current) ||
      compiler.isNoSubstitutionTemplateLiteral(current) ||
      compiler.isNumericLiteral(current)
      ? current.text
      : undefined;
  };
  const accessedProperty = (
    expression: ts.Expression,
  ): { readonly base: ts.Expression; readonly name?: string } | undefined => {
    const current = unwrapExpression(expression);
    if (compiler.isPropertyAccessExpression(current)) {
      const name = current.name.text;
      return {
        base: unwrapExpression(current.expression),
        ...(name === undefined ? {} : { name }),
      };
    }
    if (compiler.isElementAccessExpression(current)) {
      const name = current.argumentExpression
        ? staticPropertyName(current.argumentExpression)
        : undefined;
      return {
        base: unwrapExpression(current.expression),
        ...(name === undefined ? {} : { name }),
      };
    }
    return undefined;
  };
  const isNodeModuleSpecifier = (specifier: string): boolean =>
    specifier === "node:module" || specifier === "module";
  for (const statement of sourceFile.statements) {
    if (
      compiler.isImportEqualsDeclaration(statement) &&
      !statement.isTypeOnly &&
      compiler.isExternalModuleReference(statement.moduleReference) &&
      statement.moduleReference.expression &&
      compiler.isStringLiteral(statement.moduleReference.expression) &&
      isNodeModuleSpecifier(statement.moduleReference.expression.text)
    ) {
      moduleApiAliases.add(statement.name.text);
      continue;
    }
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier) ||
      !isNodeModuleSpecifier(statement.moduleSpecifier.text) ||
      statement.importClause?.isTypeOnly
    ) {
      continue;
    }
    const clause = statement.importClause;
    if (!clause) continue;
    if (clause.name) moduleApiAliases.add(clause.name.text);
    if (
      clause.namedBindings &&
      compiler.isNamespaceImport(clause.namedBindings)
    ) {
      moduleApiAliases.add(clause.namedBindings.name.text);
    }
    if (clause.namedBindings && compiler.isNamedImports(clause.namedBindings)) {
      for (const element of clause.namedBindings.elements) {
        if (
          !element.isTypeOnly &&
          (element.propertyName?.text ?? element.name.text) === "createRequire"
        ) {
          createRequireAliases.add(element.name.text);
        }
      }
    }
  }
  function isNodeModuleDynamicImport(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    const argument = compiler.isCallExpression(current)
      ? current.arguments[0]
      : undefined;
    return (
      compiler.isCallExpression(current) &&
      current.expression.kind === compiler.SyntaxKind.ImportKeyword &&
      current.arguments.length === 1 &&
      argument !== undefined &&
      (compiler.isStringLiteral(argument) ||
        compiler.isNoSubstitutionTemplateLiteral(argument)) &&
      isNodeModuleSpecifier(argument.text)
    );
  }
  function isNodeModuleValue(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    if (compiler.isIdentifier(current) && moduleApiAliases.has(current.text)) {
      return true;
    }
    if (
      compiler.isAwaitExpression(current) &&
      isNodeModuleDynamicImport(current.expression)
    ) {
      return true;
    }
    const call =
      compiler.isCallExpression(current) &&
      isRequireCallTarget(current.expression)
        ? current
        : undefined;
    const argument = call?.arguments[0];
    return (
      call !== undefined &&
      call.arguments.length === 1 &&
      argument !== undefined &&
      (compiler.isStringLiteral(argument) ||
        compiler.isNoSubstitutionTemplateLiteral(argument)) &&
      isNodeModuleSpecifier(argument.text)
    );
  }
  function isCreateRequireReference(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    if (
      compiler.isIdentifier(current) &&
      createRequireAliases.has(current.text)
    ) {
      return true;
    }
    const property = accessedProperty(current);
    return (
      property?.name === "createRequire" && isNodeModuleValue(property.base)
    );
  }
  function isCommonJsModuleValue(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    return (
      compiler.isIdentifier(current) && commonJsModuleAliases.has(current.text)
    );
  }
  function isCommonJsRequireReference(node: ts.Expression): boolean {
    const property = accessedProperty(node);
    return property?.name === "require" && isCommonJsModuleValue(property.base);
  }
  const addAlias = (aliases: Set<string>, name: string): void => {
    if (!aliases.has(name)) {
      aliases.add(name);
      changed = true;
    }
  };
  function registerLoaderAlias(name: string, initializer: ts.Expression): void {
    const current = unwrapExpression(initializer);
    if (compiler.isIdentifier(current)) {
      if (requireAliases.has(current.text)) addAlias(requireAliases, name);
      if (createRequireAliases.has(current.text))
        addAlias(createRequireAliases, name);
      if (moduleApiAliases.has(current.text)) addAlias(moduleApiAliases, name);
      if (commonJsModuleAliases.has(current.text))
        addAlias(commonJsModuleAliases, name);
    }
    if (
      compiler.isCallExpression(current) &&
      isCreateRequireReference(current.expression)
    ) {
      addAlias(requireAliases, name);
    }
    if (isNodeModuleValue(current)) addAlias(moduleApiAliases, name);
    if (isCreateRequireReference(current)) {
      addAlias(createRequireAliases, name);
    }
    if (isCommonJsModuleValue(current)) addAlias(commonJsModuleAliases, name);
    if (isCommonJsRequireReference(current)) addAlias(requireAliases, name);
  }
  let changed = true;
  while (changed) {
    changed = false;
    function findRequireAliases(node: ts.Node): void {
      if (
        compiler.isVariableDeclaration(node) &&
        compiler.isIdentifier(node.name) &&
        node.initializer
      ) {
        registerLoaderAlias(node.name.text, node.initializer);
      }
      if (
        compiler.isVariableDeclaration(node) &&
        compiler.isObjectBindingPattern(node.name) &&
        node.initializer &&
        (isNodeModuleValue(node.initializer) ||
          isCommonJsModuleValue(node.initializer))
      ) {
        const nodeModuleValue = isNodeModuleValue(node.initializer);
        const commonJsModuleValue = isCommonJsModuleValue(node.initializer);
        for (const element of node.name.elements) {
          if (!compiler.isIdentifier(element.name)) continue;
          const importedName = element.propertyName
            ? compiler.isIdentifier(element.propertyName) ||
              compiler.isStringLiteral(element.propertyName) ||
              compiler.isNumericLiteral(element.propertyName)
              ? element.propertyName.text
              : undefined
            : element.name.text;
          if (nodeModuleValue && importedName === "createRequire") {
            addAlias(createRequireAliases, element.name.text);
          }
          if (commonJsModuleValue && importedName === "require") {
            addAlias(requireAliases, element.name.text);
          }
        }
      }
      if (
        compiler.isBinaryExpression(node) &&
        node.operatorToken.kind === compiler.SyntaxKind.EqualsToken &&
        compiler.isIdentifier(unwrapExpression(node.left))
      ) {
        registerLoaderAlias(
          (unwrapExpression(node.left) as ts.Identifier).text,
          node.right,
        );
      }
      compiler.forEachChild(node, findRequireAliases);
    }
    findRequireAliases(sourceFile);
  }
  function isCommonJsExport(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    if (compiler.isIdentifier(current)) return current.text === "exports";
    const property = accessedProperty(current);
    if (!property) return false;
    if (
      compiler.isIdentifier(property.base) &&
      property.base.text === "module"
    ) {
      // A dynamic module[key] assignment can be module["exports"] at runtime,
      // so it is intentionally rejected rather than guessed safe.
      return property.name === undefined || property.name === "exports";
    }
    return isCommonJsExport(property.base);
  }
  function isRequireCallTarget(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    if (compiler.isIdentifier(current) && requireAliases.has(current.text)) {
      return true;
    }
    if (isCommonJsRequireReference(current)) return true;
    return (
      compiler.isCallExpression(current) &&
      isCreateRequireReference(current.expression)
    );
  }
  function isCommonJsExportReference(node: ts.Node): boolean {
    if (
      !compiler.isIdentifier(node) &&
      !compiler.isPropertyAccessExpression(node) &&
      !compiler.isElementAccessExpression(node) &&
      !compiler.isParenthesizedExpression(node)
    ) {
      return false;
    }
    if (
      compiler.isIdentifier(node) &&
      compiler.isPropertyAccessExpression(node.parent) &&
      node.parent.name === node
    ) {
      return false;
    }
    if (!isCommonJsExport(node)) return false;
    const parent = node.parent;
    return !(
      ((compiler.isPropertyAccessExpression(parent) ||
        compiler.isElementAccessExpression(parent)) &&
        parent.expression === node &&
        isCommonJsExport(parent)) ||
      (compiler.isParenthesizedExpression(parent) &&
        parent.expression === node &&
        isCommonJsExport(parent))
    );
  }
  function transparentOuterNode(node: ts.Node): ts.Node {
    let current = node;
    while (
      compiler.isParenthesizedExpression(current.parent) &&
      current.parent.expression === current
    ) {
      current = current.parent;
    }
    return current;
  }
  function isDeclarationOrPropertyName(node: ts.Identifier): boolean {
    const parent = node.parent;
    return (
      (compiler.isVariableDeclaration(parent) && parent.name === node) ||
      (compiler.isParameter(parent) && parent.name === node) ||
      (compiler.isBindingElement(parent) && parent.name === node) ||
      (compiler.isImportClause(parent) && parent.name === node) ||
      (compiler.isImportSpecifier(parent) &&
        (parent.name === node || parent.propertyName === node)) ||
      (compiler.isNamespaceImport(parent) && parent.name === node) ||
      (compiler.isImportEqualsDeclaration(parent) && parent.name === node) ||
      ((compiler.isFunctionDeclaration(parent) ||
        compiler.isFunctionExpression(parent) ||
        compiler.isClassDeclaration(parent) ||
        compiler.isClassExpression(parent) ||
        compiler.isTypeAliasDeclaration(parent) ||
        compiler.isInterfaceDeclaration(parent)) &&
        parent.name === node) ||
      (compiler.isPropertyAccessExpression(parent) && parent.name === node) ||
      (compiler.isPropertyAssignment(parent) && parent.name === node) ||
      (compiler.isBindingElement(parent) && parent.propertyName === node)
    );
  }
  function isDirectCallTarget(node: ts.Node): boolean {
    const outer = transparentOuterNode(node);
    return (
      compiler.isExpression(outer) &&
      compiler.isCallExpression(outer.parent) &&
      unwrapExpression(outer.parent.expression) === unwrapExpression(outer)
    );
  }
  function isSimpleAliasPropagation(
    node: ts.Node,
    aliases: ReadonlySet<string>,
  ): boolean {
    const outer = transparentOuterNode(node);
    const parent = outer.parent;
    if (
      compiler.isVariableDeclaration(parent) &&
      parent.initializer === outer &&
      compiler.isIdentifier(parent.name)
    ) {
      return aliases.has(parent.name.text);
    }
    return (
      compiler.isBinaryExpression(parent) &&
      parent.operatorToken.kind === compiler.SyntaxKind.EqualsToken &&
      parent.right === outer &&
      compiler.isIdentifier(unwrapExpression(parent.left)) &&
      aliases.has((unwrapExpression(parent.left) as ts.Identifier).text)
    );
  }
  function propertyAccessFromBase(node: ts.Node): ts.Expression | undefined {
    const outer = transparentOuterNode(node);
    const parent = outer.parent;
    return compiler.isExpression(outer) &&
      (compiler.isPropertyAccessExpression(parent) ||
        compiler.isElementAccessExpression(parent)) &&
      parent.expression === outer
      ? parent
      : undefined;
  }
  function objectBindingForInitializer(
    node: ts.Node,
  ): ts.ObjectBindingPattern | undefined {
    const outer = transparentOuterNode(node);
    const parent = outer.parent;
    return compiler.isVariableDeclaration(parent) &&
      parent.initializer === outer &&
      compiler.isObjectBindingPattern(parent.name)
      ? parent.name
      : undefined;
  }
  function safeObjectBinding(
    binding: ts.ObjectBindingPattern,
    forbidden: ReadonlySet<string> = new Set(),
  ): boolean {
    return binding.elements.every((element) => {
      if (element.dotDotDotToken || !compiler.isIdentifier(element.name))
        return false;
      if (
        element.propertyName &&
        !compiler.isIdentifier(element.propertyName) &&
        !compiler.isStringLiteral(element.propertyName) &&
        !compiler.isNumericLiteral(element.propertyName)
      ) {
        return false;
      }
      const name =
        element.propertyName?.getText(sourceFile) ?? element.name.text;
      return !forbidden.has(name.replace(/^["']|["']$/g, ""));
    });
  }
  function isSafeModuleApiUse(node: ts.Node): boolean {
    if (isSimpleAliasPropagation(node, moduleApiAliases)) return true;
    const binding = objectBindingForInitializer(node);
    if (binding) return safeObjectBinding(binding);
    const property = propertyAccessFromBase(node);
    return (
      property !== undefined && accessedProperty(property)?.name !== undefined
    );
  }
  function unsafeLoaderReference(node: ts.Node): ts.Identifier | undefined {
    if (!compiler.isIdentifier(node) || isDeclarationOrPropertyName(node)) {
      return undefined;
    }
    if (requireAliases.has(node.text)) {
      return isDirectCallTarget(node) ||
        isSimpleAliasPropagation(node, requireAliases)
        ? undefined
        : node;
    }
    if (createRequireAliases.has(node.text)) {
      return isDirectCallTarget(node) ||
        isSimpleAliasPropagation(node, createRequireAliases)
        ? undefined
        : node;
    }
    if (moduleApiAliases.has(node.text)) {
      if (isSimpleAliasPropagation(node, moduleApiAliases)) return undefined;
      const binding = objectBindingForInitializer(node);
      if (binding) return safeObjectBinding(binding) ? undefined : node;
      const property = propertyAccessFromBase(node);
      if (!property) return node;
      const accessed = accessedProperty(property);
      if (accessed?.name === undefined) return node;
      if (accessed.name !== "createRequire") return undefined;
      return isDirectCallTarget(property) ||
        isSimpleAliasPropagation(property, createRequireAliases)
        ? undefined
        : node;
    }
    if (commonJsModuleAliases.has(node.text)) {
      if (isSimpleAliasPropagation(node, commonJsModuleAliases))
        return undefined;
      const binding = objectBindingForInitializer(node);
      if (binding)
        return safeObjectBinding(binding, new Set(["exports"]))
          ? undefined
          : node;
      const property = propertyAccessFromBase(node);
      if (!property) return node;
      const accessed = accessedProperty(property);
      if (accessed?.name === undefined) return node;
      if (accessed.name !== "require") return undefined;
      return isDirectCallTarget(property) ||
        isSimpleAliasPropagation(property, requireAliases)
        ? undefined
        : node;
    }
    return undefined;
  }
  function unsafeLoaderExpression(node: ts.Node): ts.Node | undefined {
    if (!compiler.isExpression(node)) return undefined;
    const current = unwrapExpression(node);
    // Parenthesized wrappers are visited through their contained expression,
    // whose context is recovered by transparentOuterNode.
    if (current !== node || compiler.isIdentifier(current)) return undefined;

    if (
      compiler.isCallExpression(current) &&
      isCreateRequireReference(current.expression)
    ) {
      return isDirectCallTarget(current) ||
        isSimpleAliasPropagation(current, requireAliases)
        ? undefined
        : current;
    }

    if (
      isCreateRequireReference(current) ||
      isCommonJsRequireReference(current)
    ) {
      const property = accessedProperty(current);
      const base = property?.base;
      // Identifier bases are already checked by unsafeLoaderReference. This
      // branch closes direct-expression routes such as
      // require("node:module").createRequire.bind(...).
      if (
        base &&
        compiler.isIdentifier(base) &&
        (moduleApiAliases.has(base.text) ||
          commonJsModuleAliases.has(base.text))
      ) {
        return undefined;
      }
      const aliases = isCreateRequireReference(current)
        ? createRequireAliases
        : requireAliases;
      return isDirectCallTarget(current) ||
        isSimpleAliasPropagation(current, aliases)
        ? undefined
        : current;
    }

    if (isNodeModuleValue(current)) {
      return isSafeModuleApiUse(current) ? undefined : current;
    }
    return undefined;
  }
  function isImportMetaUrl(node: ts.Expression): boolean {
    const current = unwrapExpression(node);
    return (
      compiler.isPropertyAccessExpression(current) &&
      current.name.text === "url" &&
      compiler.isMetaProperty(current.expression) &&
      current.expression.keywordToken === compiler.SyntaxKind.ImportKeyword &&
      current.expression.name.text === "meta"
    );
  }
  for (const directive of tripleSlashDirectiveRanges(compiler, sourceFile)) {
    const location = sourceFile.getLineAndCharacterOfPosition(directive.start);
    diagnostics.push({
      code: "JAUNT_TS_TRIPLE_SLASH_DIRECTIVE",
      severity: "error",
      message:
        "Generated implementations may not contain triple-slash directives; use imports and tsconfig.json instead",
      path: ir.implementationPath,
      start: directive.start,
      end: directive.end,
      line: location.line + 1,
      column: location.character + 1,
    });
  }
  for (const reference of sourceFile.typeReferenceDirectives) {
    const provenance = auditPackageImport(
      root,
      path,
      reference.fileName,
      false,
      resolvePackageImport(reference.fileName),
      false,
    );
    if (!provenance) continue;
    const start = Math.max(0, reference.pos);
    const end = Math.max(start, reference.end);
    const location = sourceFile.getLineAndCharacterOfPosition(start);
    diagnostics.push({
      ...provenance,
      path: ir.implementationPath,
      start,
      end,
      line: location.line + 1,
      column: location.character + 1,
    });
  }
  function visit(node: ts.Node): void {
    const unsafeLoader = unsafeLoaderReference(node);
    if (unsafeLoader) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          unsafeLoader,
          "JAUNT_TS_DYNAMIC_IMPORT",
          "Generated candidates may not pass, bind, reflect, destructure, or conditionally select module loaders; use a direct loader alias with one static string literal",
        ),
      );
    }
    const unsafeExpression = unsafeLoaderExpression(node);
    if (unsafeExpression) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          unsafeExpression,
          "JAUNT_TS_DYNAMIC_IMPORT",
          "Generated candidates may not pass, bind, reflect, destructure, or conditionally select module loaders; use a direct loader alias with one static string literal",
        ),
      );
    }
    if (
      compiler.isCallExpression(node) &&
      isNodeModuleDynamicImport(node) &&
      !compiler.isAwaitExpression(transparentOuterNode(node).parent)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_DYNAMIC_IMPORT",
          "Dynamic node:module imports must be awaited directly so createRequire provenance remains auditable",
        ),
      );
    }
    if (
      compiler.isCallExpression(node) &&
      isCreateRequireReference(node.expression) &&
      (node.arguments.length !== 1 ||
        !node.arguments[0] ||
        !isImportMetaUrl(node.arguments[0]))
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_DYNAMIC_IMPORT",
          "createRequire must be called directly with import.meta.url so package provenance is resolved from the generated module",
        ),
      );
    }
    if (node.kind === compiler.SyntaxKind.AnyKeyword) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_CANDIDATE_ANY",
          "Explicit `any` is forbidden",
        ),
      );
    }
    if (
      compiler.isExportAssignment(node) ||
      compiler.isExportDeclaration(node)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_MODEL_EXPORT",
          "Codex may not author exports",
        ),
      );
    }
    if (isCommonJsExportReference(node)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_MODEL_EXPORT",
          "Codex may not reference or mutate the CommonJS export surface",
        ),
      );
    }
    if (
      compiler.canHaveModifiers(node) &&
      compiler
        .getModifiers(node)
        ?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.ExportKeyword,
        )
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_MODEL_EXPORT",
          "Codex may not author exports",
        ),
      );
    }
    if (compiler.isModuleDeclaration(node)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_AMBIENT_AUGMENTATION",
          "Generated candidates may not declare namespaces, ambient modules, or global augmentations",
        ),
      );
    }
    if (
      compiler.canHaveModifiers(node) &&
      compiler
        .getModifiers(node)
        ?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.DeclareKeyword,
        )
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_AMBIENT_AUGMENTATION",
          "Generated candidates may not use ambient declare bindings",
        ),
      );
    }
    if (
      compiler.isInterfaceDeclaration(node) &&
      reservedNames.has(node.name.text)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_DECLARATION_MERGING",
          "Generated candidates may not merge interfaces into reserved implementation bindings",
        ),
      );
    }
    const topLevelTypeDeclaration =
      node.parent === sourceFile &&
      (compiler.isTypeAliasDeclaration(node) ||
        compiler.isInterfaceDeclaration(node) ||
        compiler.isClassDeclaration(node) ||
        compiler.isEnumDeclaration(node));
    if (
      topLevelTypeDeclaration &&
      node.name !== undefined &&
      publicTypeNames.has(node.name.text)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_PUBLIC_TYPE_SHADOW",
          `Generated candidates may not redeclare public API type ${node.name.text}; use a distinct private helper type name instead`,
        ),
      );
    }
    if (
      compiler.isImportTypeNode(node) &&
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteral(node.argument.literal)
    ) {
      const provenance = auditPackageImport(
        root,
        path,
        node.argument.literal.text,
        false,
        resolvePackageImport(node.argument.literal.text),
        false,
      );
      if (provenance) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            node.argument.literal,
            provenance.code,
            provenance.message,
          ),
        );
      }
    }
    if (
      compiler.isImportDeclaration(node) &&
      compiler.isStringLiteral(node.moduleSpecifier) &&
      /\.jaunt(?:-test)?(?:\.(?:js|ts|tsx))?$/.test(node.moduleSpecifier.text)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_SPEC_IMPORT",
          "Generated code may not import private spec modules",
        ),
      );
    }
    if (
      compiler.isImportDeclaration(node) &&
      compiler.isStringLiteral(node.moduleSpecifier)
    ) {
      for (const localName of importLocalNames(compiler, node)) {
        if (!publicTypeNames.has(localName)) continue;
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            node,
            "JAUNT_TS_PUBLIC_TYPE_SHADOW",
            `Generated candidates may not import a local binding named ${localName} because it shadows a public API type; use a distinct private alias instead`,
          ),
        );
      }
      const provenance = auditPackageImport(
        root,
        path,
        node.moduleSpecifier.text,
        false,
        resolvePackageImport(node.moduleSpecifier.text),
        false,
      );
      if (provenance) {
        diagnostics.push({ ...provenance, path: ir.implementationPath });
      }
    }
    if (
      compiler.isImportEqualsDeclaration(node) &&
      compiler.isExternalModuleReference(node.moduleReference) &&
      node.moduleReference.expression &&
      compiler.isStringLiteral(node.moduleReference.expression)
    ) {
      const specifier = node.moduleReference.expression.text;
      if (/\.jaunt(?:-test)?(?:\.(?:js|ts|tsx))?$/.test(specifier)) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            node,
            "JAUNT_TS_SPEC_IMPORT",
            "Generated code may not import private spec modules",
          ),
        );
      }
      const provenance = auditPackageImport(
        root,
        path,
        specifier,
        false,
        resolvePackageImport(specifier),
        false,
      );
      if (provenance) {
        diagnostics.push({ ...provenance, path: ir.implementationPath });
      }
    }
    if (
      compiler.isCallExpression(node) &&
      (node.expression.kind === compiler.SyntaxKind.ImportKeyword ||
        isRequireCallTarget(node.expression))
    ) {
      const argument = node.arguments[0];
      if (
        node.arguments.length !== 1 ||
        !argument ||
        !compiler.isStringLiteral(argument)
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            node,
            "JAUNT_TS_DYNAMIC_IMPORT",
            "Generated candidates may use import()/require() only with one statically auditable string literal",
          ),
        );
      } else {
        if (/\.jaunt(?:-test)?(?:\.(?:js|ts|tsx))?$/.test(argument.text)) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              node,
              "JAUNT_TS_SPEC_IMPORT",
              "Generated code may not dynamically import private spec modules",
            ),
          );
        }
        const provenance = auditPackageImport(
          root,
          path,
          argument.text,
          false,
          resolvePackageImport(argument.text),
          false,
        );
        if (provenance) {
          diagnostics.push({ ...provenance, path: ir.implementationPath });
        }
      }
    }
    if (
      (compiler.isAsExpression(node) ||
        compiler.isTypeAssertionExpression(node)) &&
      (compiler.isAsExpression(node.expression) ||
        compiler.isTypeAssertionExpression(node.expression)) &&
      (node.expression.type.kind === compiler.SyntaxKind.UnknownKeyword ||
        node.expression.type.kind === compiler.SyntaxKind.AnyKeyword)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_DOUBLE_ASSERTION",
          "Boundary double assertions such as `as unknown as T` are forbidden",
        ),
      );
    }
    if (
      compiler.isAsExpression(node) ||
      compiler.isTypeAssertionExpression(node) ||
      compiler.isNonNullExpression(node)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          node,
          "JAUNT_TS_BOUNDARY_ASSERTION",
          "Unchecked type assertions and non-null assertions are forbidden in generated candidates; narrow unknown values with runtime checks",
        ),
      );
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  validateClassSurface(compiler, root, sourceFile, ir, diagnostics);
  const names = declaredNames(compiler, sourceFile);
  for (const symbol of ir.symbols.filter((item) => item.kind === "class")) {
    if (
      symbol.heritage &&
      symbol.heritage.implementationName === symbol.heritage.baseName &&
      names.has(symbol.heritage.baseName)
    ) {
      diagnostics.push({
        code: "JAUNT_TS_CANDIDATE_HERITAGE_SHADOW",
        severity: "error",
        message: `Candidate may not shadow authored base class ${symbol.heritage.baseName}`,
        path: ir.implementationPath,
      });
    }
  }
  for (const symbol of ir.symbols) {
    const reserved = `__jaunt_impl_${symbol.name}`;
    if (!names.has(reserved)) {
      diagnostics.push({
        code: "JAUNT_TS_MISSING_BINDING",
        severity: "error",
        message: `Candidate must declare reserved binding ${reserved}`,
        path,
      });
    } else if (
      symbol.kind === "function" &&
      !hasFreshFunctionBinding(compiler, sourceFile, reserved)
    ) {
      diagnostics.push({
        code: "JAUNT_TS_FUNCTION_ALIAS",
        severity: "error",
        message: `Generated function ${symbol.name} must use a fresh function declaration, expression, or arrow binding`,
        path,
      });
    }
  }
  const preservedCandidate = applyPreservedBodies(
    compiler,
    root,
    ir,
    sourceFile,
    candidate,
    diagnostics,
  );
  const candidateSource = injectRuntimeImports(
    compiler,
    root,
    ir,
    sourceFile,
    preservedCandidate,
    diagnostics,
  );
  const api = relativeModuleSpecifier(ir.implementationPath, ir.apiMirrorPath);
  const boundary = renderedBoundary(ir);
  const header = [
    "// ⛓️ jaunt:generated — generated; do not edit.",
    "// jaunt:state=built",
    `// jaunt:module=${ir.moduleId}`,
    `// jaunt:structural=${ir.structuralDigest}`,
    `// jaunt:prose=${ir.proseDigest}`,
    `// jaunt:api=${ir.apiDigest}`,
    `import type * as __JauntApi from ${JSON.stringify(api)};`,
    "",
  ].join("\n");
  return {
    source: `${header}${candidateSource.trim()}\n\n${boundary.join("\n")}\n`,
    candidateSource,
    diagnostics: sortDiagnostics(diagnostics),
  };
}
