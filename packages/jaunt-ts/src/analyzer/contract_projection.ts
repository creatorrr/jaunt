import type ts from "@typescript/typescript6";
import { sha256Bytes } from "./canonical.js";
import { WorkerError } from "../protocol/errors.js";

export interface ContractProjection {
  readonly source: string;
  readonly sourceDigest: string;
  readonly symbol: string;
  readonly kind: "function" | "class";
  readonly declarationStart: number;
  readonly declarationEnd: number;
  readonly docsStart?: number;
  readonly docsEnd?: number;
}

interface SourceEdit {
  readonly start: number;
  readonly end: number;
  readonly replacement: string;
}

function fail(message: string): never {
  throw new WorkerError("INVALID_CONTRACT_SOURCE", message);
}

function parseDiagnostics(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): readonly ts.Diagnostic[] {
  return (
    (
      sourceFile as ts.SourceFile & {
        readonly parseDiagnostics?: readonly ts.Diagnostic[];
      }
    ).parseDiagnostics ?? []
  );
}

function assertParsed(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  label: string,
): void {
  const diagnostics = parseDiagnostics(compiler, sourceFile);
  if (diagnostics.length === 0) return;
  const detail = diagnostics
    .slice(0, 3)
    .map((diagnostic) =>
      compiler.flattenDiagnosticMessageText(diagnostic.messageText, " "),
    )
    .join("; ");
  fail(`${label} is not valid TypeScript: ${detail}`);
}

function hasModifier(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  kind: ts.SyntaxKind,
): boolean {
  return Boolean(
    compiler.canHaveModifiers(node) &&
    compiler.getModifiers(node)?.some((modifier) => modifier.kind === kind),
  );
}

function isExported(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): boolean {
  return hasModifier(compiler, node, compiler.SyntaxKind.ExportKeyword);
}

function assertNoDecorators(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  label: string,
): void {
  if (
    compiler.canHaveDecorators(node) &&
    (compiler.getDecorators(node)?.length ?? 0) > 0
  ) {
    fail(`${label} uses executable decorators and cannot be projected safely`);
  }
}

function safeReferenceExpression(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
): boolean {
  if (compiler.isIdentifier(expression)) return true;
  if (compiler.isParenthesizedExpression(expression)) {
    return safeReferenceExpression(compiler, expression.expression);
  }
  return (
    compiler.isPropertyAccessExpression(expression) &&
    safeReferenceExpression(compiler, expression.expression)
  );
}

function safeComputedNameExpression(
  compiler: typeof import("@typescript/typescript6"),
  expression: ts.Expression,
): boolean {
  if (
    compiler.isStringLiteral(expression) ||
    compiler.isNoSubstitutionTemplateLiteral(expression) ||
    compiler.isNumericLiteral(expression) ||
    compiler.isBigIntLiteral(expression) ||
    compiler.isPrivateIdentifier(expression) ||
    expression.kind === compiler.SyntaxKind.TrueKeyword ||
    expression.kind === compiler.SyntaxKind.FalseKeyword ||
    expression.kind === compiler.SyntaxKind.NullKeyword
  ) {
    return true;
  }
  if (compiler.isParenthesizedExpression(expression)) {
    return safeComputedNameExpression(compiler, expression.expression);
  }
  if (safeReferenceExpression(compiler, expression)) return true;
  return (
    compiler.isPrefixUnaryExpression(expression) &&
    (expression.operator === compiler.SyntaxKind.PlusToken ||
      expression.operator === compiler.SyntaxKind.MinusToken) &&
    (compiler.isNumericLiteral(expression.operand) ||
      compiler.isBigIntLiteral(expression.operand))
  );
}

function assertRetainedDeclarationSafe(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  label: string,
): void {
  function visit(current: ts.Node): void {
    assertNoDecorators(compiler, current, label);
    if (
      compiler.isComputedPropertyName(current) &&
      !safeComputedNameExpression(compiler, current.expression)
    ) {
      fail(`${label} retained an executable computed property name`);
    }
    if (
      compiler.isExpressionWithTypeArguments(current) &&
      !safeReferenceExpression(compiler, current.expression)
    ) {
      fail(`${label} retained an executable heritage expression`);
    }
    if (
      compiler.isExpression(current) &&
      !safeComputedNameExpression(compiler, current)
    ) {
      fail(`${label} retained an executable expression`);
    }
    const initializer = (
      current as ts.Node & { readonly initializer?: ts.Node }
    ).initializer;
    if (initializer) {
      fail(`${label} retained an executable initializer`);
    }
    compiler.forEachChild(current, visit);
  }
  visit(node);
}

type ContractDeclaration = ts.FunctionDeclaration | ts.ClassDeclaration;

function declarationDocs(node: ContractDeclaration): readonly ts.JSDoc[] {
  return (
    (node as ContractDeclaration & { readonly jsDoc?: readonly ts.JSDoc[] })
      .jsDoc ?? []
  );
}

function selectedDeclaration(
  source: string,
  sourceFile: ts.SourceFile,
  functions: readonly ts.FunctionDeclaration[],
  classes: readonly ts.ClassDeclaration[],
): ContractDeclaration {
  const declarations: readonly ContractDeclaration[] =
    functions.length > 0 ? functions : classes;
  const marked = declarations.find((declaration) =>
    declarationDocs(declaration).some((docs) =>
      source
        .slice(docs.getStart(sourceFile), docs.end)
        .includes("@jauntContract"),
    ),
  );
  if (marked) return marked;
  if (functions.length > 0) {
    return functions.find((declaration) => declaration.body) ?? functions[0]!;
  }
  return classes[0]!;
}

function declarationRanges(
  source: string,
  sourceFile: ts.SourceFile,
  declaration: ContractDeclaration,
): Pick<
  ContractProjection,
  "declarationStart" | "declarationEnd" | "docsStart" | "docsEnd"
> {
  const docs = declarationDocs(declaration);
  let selectedDocs = docs.at(-1);
  for (const candidate of docs) {
    if (
      source
        .slice(candidate.getStart(sourceFile), candidate.end)
        .includes("@jauntContract")
    ) {
      selectedDocs = candidate;
      break;
    }
  }
  return {
    declarationStart: declaration.getStart(sourceFile),
    declarationEnd: declaration.end,
    ...(selectedDocs
      ? {
          docsStart: selectedDocs.getStart(sourceFile),
          docsEnd: selectedDocs.end,
        }
      : {}),
  };
}

function declarationStart(sourceFile: ts.SourceFile, node: ts.Node): number {
  return node.getStart(sourceFile, true);
}

function assignmentStart(
  source: string,
  sourceFile: ts.SourceFile,
  owner: ts.Node,
  initializer: ts.Expression,
  label: string,
): number {
  const initializerStart = initializer.getStart(sourceFile);
  const prefix = source.slice(owner.getStart(sourceFile), initializerStart);
  const relative = prefix.lastIndexOf("=");
  if (relative < 0 || prefix[relative + 1] === ">") {
    fail(`Could not safely remove the initializer from ${label}`);
  }
  return owner.getStart(sourceFile) + relative;
}

function bindingInitializerEdits(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
  sourceFile: ts.SourceFile,
  name: ts.BindingName,
  edits: SourceEdit[],
  label: string,
): void {
  if (compiler.isIdentifier(name)) return;
  for (const element of name.elements) {
    if (compiler.isOmittedExpression(element)) continue;
    assertNoDecorators(compiler, element, label);
    if (element.initializer) {
      edits.push({
        start: assignmentStart(
          source,
          sourceFile,
          element,
          element.initializer,
          label,
        ),
        end: element.initializer.end,
        replacement: "",
      });
    }
    bindingInitializerEdits(
      compiler,
      source,
      sourceFile,
      element.name,
      edits,
      label,
    );
  }
}

function parameterEdits(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
  sourceFile: ts.SourceFile,
  parameters: readonly ts.ParameterDeclaration[],
  edits: SourceEdit[],
  label: string,
): void {
  for (const parameter of parameters) {
    assertNoDecorators(compiler, parameter, label);
    if (parameter.initializer) {
      if (!parameter.type) {
        fail(
          `${label} has a defaulted parameter without an explicit type; ` +
            "its full signature cannot be retained without the initializer",
        );
      }
      edits.push({
        start: assignmentStart(
          source,
          sourceFile,
          parameter,
          parameter.initializer,
          label,
        ),
        end: parameter.initializer.end,
        replacement: "",
      });
    }
    bindingInitializerEdits(
      compiler,
      source,
      sourceFile,
      parameter.name,
      edits,
      label,
    );
  }
}

function applyEdits(
  source: string,
  start: number,
  end: number,
  edits: readonly SourceEdit[],
): string {
  let projected = source.slice(start, end);
  const selected = edits
    .filter((edit) => edit.start >= start && edit.end <= end)
    .sort((left, right) => right.start - left.start || right.end - left.end);
  let previousStart = end;
  for (const edit of selected) {
    if (edit.start < start || edit.end > end || edit.start >= edit.end) {
      fail("The declaration projection produced an invalid source edit");
    }
    if (edit.end > previousStart) {
      fail("The declaration projection produced overlapping source edits");
    }
    const localStart = edit.start - start;
    const localEnd = edit.end - start;
    projected =
      projected.slice(0, localStart) +
      edit.replacement +
      projected.slice(localEnd);
    previousStart = edit.start;
  }
  return projected.trim();
}

function stripNonTsdocComments(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
): string {
  const scanner = compiler.createScanner(
    compiler.ScriptTarget.Latest,
    false,
    compiler.LanguageVariant.Standard,
    source,
  );
  const edits: SourceEdit[] = [];
  for (
    let token = scanner.scan();
    token !== compiler.SyntaxKind.EndOfFileToken;
    token = scanner.scan()
  ) {
    if (
      token !== compiler.SyntaxKind.SingleLineCommentTrivia &&
      token !== compiler.SyntaxKind.MultiLineCommentTrivia
    ) {
      continue;
    }
    const text = scanner.getTokenText();
    if (text.startsWith("/**")) continue;
    edits.push({
      start: scanner.getTokenPos(),
      end: scanner.getTextPos(),
      replacement: "",
    });
  }
  return applyEdits(source, 0, source.length, edits);
}

function projectFunction(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
  sourceFile: ts.SourceFile,
  declaration: ts.FunctionDeclaration,
): string {
  const label = `function ${declaration.name?.text ?? "<anonymous>"}`;
  assertNoDecorators(compiler, declaration, label);
  const edits: SourceEdit[] = [];
  parameterEdits(
    compiler,
    source,
    sourceFile,
    declaration.parameters,
    edits,
    label,
  );
  if (declaration.body) {
    edits.push({
      start: declaration.body.pos,
      end: declaration.body.end,
      replacement: ";",
    });
  }
  return applyEdits(
    source,
    declarationStart(sourceFile, declaration),
    declaration.end,
    edits,
  );
}

function assertSafeHeritage(
  compiler: typeof import("@typescript/typescript6"),
  declaration: ts.ClassDeclaration,
): void {
  for (const clause of declaration.heritageClauses ?? []) {
    for (const type of clause.types) {
      if (!safeReferenceExpression(compiler, type.expression)) {
        fail(
          `class ${declaration.name?.text ?? "<anonymous>"} has an executable ` +
            "heritage expression and cannot be projected safely",
        );
      }
    }
  }
}

function projectClass(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
  sourceFile: ts.SourceFile,
  declaration: ts.ClassDeclaration,
): string {
  const label = `class ${declaration.name?.text ?? "<anonymous>"}`;
  assertNoDecorators(compiler, declaration, label);
  assertSafeHeritage(compiler, declaration);
  const edits: SourceEdit[] = [];
  for (const member of declaration.members) {
    assertNoDecorators(compiler, member, `${label} member`);
    if (compiler.isClassStaticBlockDeclaration(member)) {
      edits.push({
        start: member.pos,
        end: member.end,
        replacement: "",
      });
      continue;
    }
    if (
      compiler.isConstructorDeclaration(member) ||
      compiler.isMethodDeclaration(member) ||
      compiler.isGetAccessorDeclaration(member) ||
      compiler.isSetAccessorDeclaration(member)
    ) {
      parameterEdits(
        compiler,
        source,
        sourceFile,
        member.parameters,
        edits,
        `${label} member`,
      );
      if (member.body) {
        edits.push({
          start: member.body.pos,
          end: member.body.end,
          replacement: ";",
        });
      }
      continue;
    }
    if (compiler.isPropertyDeclaration(member) && member.initializer) {
      if (!member.type) {
        fail(
          `${label} has an initialized property without an explicit type; ` +
            "its full signature cannot be retained without the initializer",
        );
      }
      edits.push({
        start: assignmentStart(
          source,
          sourceFile,
          member,
          member.initializer,
          `${label} property`,
        ),
        end: member.end,
        replacement: ";",
      });
    }
  }
  return applyEdits(
    source,
    declarationStart(sourceFile, declaration),
    declaration.end,
    edits,
  );
}

function assertNoBindingInitializers(
  compiler: typeof import("@typescript/typescript6"),
  name: ts.BindingName,
): void {
  if (compiler.isIdentifier(name)) return;
  for (const element of name.elements) {
    if (compiler.isOmittedExpression(element)) continue;
    if (element.initializer)
      fail("Projected parameter retained a binding initializer");
    assertNoBindingInitializers(compiler, element.name);
  }
}

function assertParametersDeclarationOnly(
  compiler: typeof import("@typescript/typescript6"),
  parameters: readonly ts.ParameterDeclaration[],
): void {
  for (const parameter of parameters) {
    if (parameter.initializer)
      fail("Projected declaration retained a parameter initializer");
    assertNoBindingInitializers(compiler, parameter.name);
  }
}

function assertProjection(
  compiler: typeof import("@typescript/typescript6"),
  projected: string,
  fileName: string,
  symbol: string,
  kind: "function" | "class",
): void {
  const sourceFile = compiler.createSourceFile(
    fileName,
    projected,
    compiler.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx")
      ? compiler.ScriptKind.TSX
      : compiler.ScriptKind.TS,
  );
  assertParsed(compiler, sourceFile, "Projected contract declaration");
  let matches = 0;
  for (const statement of sourceFile.statements) {
    if (
      compiler.isInterfaceDeclaration(statement) ||
      compiler.isTypeAliasDeclaration(statement)
    ) {
      if (!isExported(compiler, statement))
        fail("Projected type context retained a non-exported declaration");
      assertRetainedDeclarationSafe(
        compiler,
        statement,
        "Projected type context",
      );
      continue;
    }
    if (
      kind === "function" &&
      compiler.isFunctionDeclaration(statement) &&
      statement.name?.text === symbol
    ) {
      matches += 1;
      if (statement.body)
        fail("Projected function retained an executable body");
      assertParametersDeclarationOnly(compiler, statement.parameters);
      assertRetainedDeclarationSafe(
        compiler,
        statement,
        `Projected function ${symbol}`,
      );
      continue;
    }
    if (
      kind === "class" &&
      compiler.isClassDeclaration(statement) &&
      statement.name?.text === symbol
    ) {
      matches += 1;
      for (const member of statement.members) {
        if (compiler.isClassStaticBlockDeclaration(member))
          fail("Projected class retained an executable static block");
        if (
          (compiler.isConstructorDeclaration(member) ||
            compiler.isMethodDeclaration(member) ||
            compiler.isGetAccessorDeclaration(member) ||
            compiler.isSetAccessorDeclaration(member)) &&
          member.body
        ) {
          fail("Projected class retained an executable member body");
        }
        if (
          compiler.isConstructorDeclaration(member) ||
          compiler.isMethodDeclaration(member) ||
          compiler.isGetAccessorDeclaration(member) ||
          compiler.isSetAccessorDeclaration(member)
        ) {
          assertParametersDeclarationOnly(compiler, member.parameters);
        }
        if (compiler.isPropertyDeclaration(member) && member.initializer) {
          fail("Projected class retained a property initializer");
        }
      }
      assertRetainedDeclarationSafe(
        compiler,
        statement,
        `Projected class ${symbol}`,
      );
      continue;
    }
    fail("Projected contract contains an unexpected top-level statement");
  }
  if (matches < 1)
    fail(`Projected contract does not contain ${kind} ${symbol}`);
}

/**
 * Produce the only source view that the independent contract-test model may see.
 *
 * The TypeScript parser, rather than text/brace heuristics, identifies executable
 * spans. The projection keeps TSDoc and every type-level token byte-for-byte while
 * removing function/member bodies, static blocks, and parameter/property
 * initializers. Any syntax or declaration shape we cannot prove safe fails closed.
 */
export function projectContractDeclaration(
  compiler: typeof import("@typescript/typescript6"),
  source: string,
  symbol: string,
  fileName = "contract.ts",
): ContractProjection {
  const sourceFile = compiler.createSourceFile(
    fileName,
    source,
    compiler.ScriptTarget.Latest,
    true,
    fileName.endsWith(".tsx")
      ? compiler.ScriptKind.TSX
      : compiler.ScriptKind.TS,
  );
  assertParsed(compiler, sourceFile, "Contract source");

  const types = sourceFile.statements.filter(
    (
      statement,
    ): statement is ts.InterfaceDeclaration | ts.TypeAliasDeclaration =>
      isExported(compiler, statement) &&
      (compiler.isInterfaceDeclaration(statement) ||
        compiler.isTypeAliasDeclaration(statement)),
  );
  const functions = sourceFile.statements.filter(
    (statement): statement is ts.FunctionDeclaration =>
      compiler.isFunctionDeclaration(statement) &&
      isExported(compiler, statement) &&
      statement.name?.text === symbol,
  );
  const classes = sourceFile.statements.filter(
    (statement): statement is ts.ClassDeclaration =>
      compiler.isClassDeclaration(statement) &&
      isExported(compiler, statement) &&
      statement.name?.text === symbol,
  );
  if (functions.length > 0 && classes.length > 0) {
    fail(`Contract symbol ${symbol} is both a function and a class`);
  }
  if (functions.length === 0 && classes.length !== 1) {
    fail(
      `Expected one exported class or an overload group named ${symbol}; ` +
        `found ${functions.length + classes.length} declarations`,
    );
  }

  const typeContext = types.map((declaration) =>
    source
      .slice(declarationStart(sourceFile, declaration), declaration.end)
      .trim(),
  );
  const kind = functions.length > 0 ? "function" : "class";
  const rangeOwner = selectedDeclaration(
    source,
    sourceFile,
    functions,
    classes,
  );
  const declarations =
    kind === "function"
      ? functions.map((declaration) =>
          projectFunction(compiler, source, sourceFile, declaration),
        )
      : [projectClass(compiler, source, sourceFile, classes[0]!)];
  const projected =
    stripNonTsdocComments(
      compiler,
      [...typeContext, ...declarations].join("\n\n"),
    ) + "\n";
  assertProjection(compiler, projected, fileName, symbol, kind);
  return {
    source: projected,
    sourceDigest: sha256Bytes(source),
    symbol,
    kind,
    ...declarationRanges(source, sourceFile, rangeOwner),
  };
}
