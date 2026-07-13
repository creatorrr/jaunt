import { readFileSync } from "node:fs";
import { basename, dirname, join, relative, resolve } from "node:path";
import type ts from "@typescript/typescript6";
import type { LoadedProject } from "./config.js";
import {
  ownerForPath,
  projectReferencesProject,
  testOwnerForPath,
} from "./config.js";
import {
  diagnosticAt,
  fromTypeScriptDiagnostic,
  sortDiagnostics,
} from "./diagnostics.js";
import { docsForNode, type ParsedDocs } from "./docs.js";
import { makeModuleRoute, toPosix } from "./artifacts.js";
import type {
  DiagnosticRecord,
  DiscoveredContract,
  DiscoveredSpec,
  DiscoveredTestSpec,
  ModuleRoute,
} from "./types.js";
import { analyzeImportGraph } from "./dependencies.js";
import { AnalysisProgramCache } from "./program_cache.js";

export interface MarkerBindings {
  readonly namespaces: ReadonlySet<string>;
  readonly magicModule: ReadonlySet<string>;
  readonly magic: ReadonlySet<string>;
  readonly testSpec: ReadonlySet<string>;
}

export interface ParsedJauntOptions {
  readonly deps?: readonly string[];
  readonly prompt?: string;
  readonly inferDeps?: boolean;
  readonly test?: boolean;
}

export interface DiscoveredFunction {
  readonly kind: "function";
  readonly name: string;
  readonly declarations: readonly ts.FunctionDeclaration[];
  readonly docs: ParsedDocs;
  /** Identifier spellings as authored in the marker options. */
  readonly dependencies: readonly string[];
  /** Stable `ts:<module>#<symbol>` IDs populated by workspace discovery. */
  readonly resolvedDependencies: string[];
  readonly options: ParsedJauntOptions;
}

export interface DiscoveredClass {
  readonly kind: "class";
  readonly name: string;
  readonly declaration: ts.ClassDeclaration;
  readonly docs: ParsedDocs;
  /** The one representable concrete base class, when authored. */
  readonly heritage?: {
    readonly baseName: string;
    readonly typeArguments: readonly ts.TypeNode[];
    /** Imported/local governed base IDs populated by workspace discovery. */
    readonly resolvedBaseIds: string[];
  };
  /** Context/runtime import bindings needed by preserved members or heritage. */
  readonly runtimeImportNames: readonly string[];
  /** Identifier spellings as authored in the marker options. */
  readonly dependencies: readonly string[];
  /** Stable `ts:<module>#<symbol>` IDs populated by workspace discovery. */
  readonly resolvedDependencies: string[];
  readonly options: ParsedJauntOptions;
}

export type DiscoveredSymbol = DiscoveredFunction | DiscoveredClass;

export interface DiscoveredModule {
  readonly route: ModuleRoute;
  readonly sourceFile: ts.SourceFile;
  readonly source: string;
  readonly markerBindings: MarkerBindings;
  readonly moduleOptions: ParsedJauntOptions;
  /** Stable dependencies from `magicModule({ deps: [...] })`. */
  readonly resolvedModuleDependencies: string[];
  /** True after the workspace-wide binding pass has handled every dependency. */
  dependencyResolutionComplete: boolean;
  /** Foreign direct dependencies; kept internal and never serialized. */
  readonly dependencyModules: DiscoveredModule[];
  /** Effective owner options used to reproduce a dependency's exact API IR. */
  readonly compilerOptions: ts.CompilerOptions;
  readonly symbols: readonly DiscoveredSymbol[];
  readonly typeDeclarations: readonly (
    ts.InterfaceDeclaration | ts.TypeAliasDeclaration
  )[];
  readonly diagnostics: readonly DiagnosticRecord[];
}

export interface DiscoveryResult {
  readonly modules: readonly DiscoveredModule[];
  readonly routes: readonly ModuleRoute[];
  readonly specs: readonly DiscoveredSpec[];
  readonly testSpecs: readonly DiscoveredTestSpec[];
  readonly contracts: readonly DiscoveredContract[];
  readonly diagnostics: readonly DiagnosticRecord[];
}

interface TestSpecDiscovery {
  readonly record: DiscoveredTestSpec;
  readonly diagnostics: readonly DiagnosticRecord[];
}

function syntaxDiagnostics(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  program: ts.Program,
  sourceFile: ts.SourceFile,
): DiagnosticRecord[] {
  return program
    .getSyntacticDiagnostics(sourceFile)
    .map((diagnostic) => fromTypeScriptDiagnostic(compiler, root, diagnostic));
}

function validateTypedParameters(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceFile: ts.SourceFile,
  parameters: ts.NodeArray<ts.ParameterDeclaration>,
  diagnostics: DiagnosticRecord[],
): void {
  for (const parameter of parameters) {
    if (!compiler.isIdentifier(parameter.name)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          parameter.name,
          "JAUNT_TS_BINDING_PATTERN",
          "Governed parameters must use identifier names; destructuring belongs in generated code",
        ),
      );
    }
    if (!parameter.type) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          parameter,
          "JAUNT_TS_EXPLICIT_TYPE_REQUIRED",
          "Every governed parameter requires an explicit TypeScript type",
        ),
      );
    }
    if (parameter.initializer) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          parameter.initializer,
          "JAUNT_TS_PARAMETER_INITIALIZER",
          "Governed parameter initializers are unsupported; use an optional parameter and TSDoc",
        ),
      );
    }
  }
}

function requireReturnType(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceFile: ts.SourceFile,
  node:
    ts.FunctionDeclaration | ts.MethodDeclaration | ts.GetAccessorDeclaration,
  diagnostics: DiagnosticRecord[],
): void {
  if (!node.type) {
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        node,
        "JAUNT_TS_EXPLICIT_TYPE_REQUIRED",
        "Every governed function, method, and getter requires an explicit return type",
      ),
    );
  }
}

function hasExport(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): boolean {
  return compiler.canHaveModifiers(node)
    ? (compiler
        .getModifiers(node)
        ?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.ExportKeyword,
        ) ?? false)
    : false;
}

export function markerBindingsFor(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): MarkerBindings {
  const namespaces = new Set<string>();
  const magicModule = new Set<string>();
  const magic = new Set<string>();
  const testSpec = new Set<string>();
  for (const statement of sourceFile.statements) {
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier)
    )
      continue;
    if (!/^@usejaunt\/ts(?:\/spec)?$/.test(statement.moduleSpecifier.text))
      continue;
    const clause = statement.importClause;
    if (!clause?.namedBindings) continue;
    if (compiler.isNamespaceImport(clause.namedBindings)) {
      namespaces.add(clause.namedBindings.name.text);
      continue;
    }
    for (const element of clause.namedBindings.elements) {
      const imported = element.propertyName?.text ?? element.name.text;
      if (imported === "magicModule") magicModule.add(element.name.text);
      if (imported === "magic") magic.add(element.name.text);
      if (imported === "testSpec") testSpec.add(element.name.text);
    }
  }
  return { namespaces, magicModule, magic, testSpec };
}

function isMarkerCall(
  compiler: typeof import("@typescript/typescript6"),
  call: ts.CallExpression,
  marker: "magicModule" | "magic" | "testSpec",
  bindings: MarkerBindings,
): boolean {
  if (compiler.isIdentifier(call.expression))
    return bindings[marker].has(call.expression.text);
  return (
    compiler.isPropertyAccessExpression(call.expression) &&
    compiler.isIdentifier(call.expression.expression) &&
    bindings.namespaces.has(call.expression.expression.text) &&
    call.expression.name.text === marker
  );
}

function magicCallFromBody(
  compiler: typeof import("@typescript/typescript6"),
  body: ts.Block | undefined,
  bindings: MarkerBindings,
  marker: "magic" | "testSpec" = "magic",
): ts.CallExpression | undefined {
  if (!body || body.statements.length !== 1) return undefined;
  const statement = body.statements[0];
  if (!statement) return undefined;
  const expression = compiler.isReturnStatement(statement)
    ? statement.expression
    : compiler.isExpressionStatement(statement)
      ? statement.expression
      : undefined;
  return expression &&
    compiler.isCallExpression(expression) &&
    isMarkerCall(compiler, expression, marker, bindings)
    ? expression
    : undefined;
}

function parseOptions(
  compiler: typeof import("@typescript/typescript6"),
  call: ts.CallExpression | undefined,
  root: string,
  sourceFile: ts.SourceFile,
  diagnostics: DiagnosticRecord[],
  allowed: ReadonlySet<string> = new Set([
    "deps",
    "prompt",
    "inferDeps",
    "test",
  ]),
): ParsedJauntOptions {
  const options = call?.arguments[0];
  if (call && call.arguments.length > 1) {
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        call,
        "JAUNT_TS_OPTIONS_ARITY",
        "Jaunt marker calls accept at most one options object",
      ),
    );
  }
  if (!options) return {};
  if (!compiler.isObjectLiteralExpression(options)) {
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        options,
        "JAUNT_TS_OPTIONS_LITERAL",
        "Jaunt options must be an object literal",
      ),
    );
    return {};
  }
  const result: {
    deps?: string[];
    prompt?: string;
    inferDeps?: boolean;
    test?: boolean;
  } = {};
  const seen = new Set<string>();
  for (const property of options.properties) {
    if (!compiler.isPropertyAssignment(property)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          property,
          "JAUNT_TS_OPTIONS_LITERAL",
          "Jaunt options do not allow spreads or shorthand properties",
        ),
      );
      continue;
    }
    const key =
      compiler.isIdentifier(property.name) ||
      compiler.isStringLiteral(property.name)
        ? property.name.text
        : "";
    if (!allowed.has(key)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          property.name,
          "JAUNT_TS_OPTION_UNKNOWN",
          `Unknown Jaunt option ${JSON.stringify(key)}`,
        ),
      );
      continue;
    }
    if (seen.has(key)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          property.name,
          "JAUNT_TS_OPTION_DUPLICATE",
          `Duplicate Jaunt option ${key}`,
        ),
      );
      continue;
    }
    seen.add(key);
    if (key === "deps") {
      if (
        !compiler.isArrayLiteralExpression(property.initializer) ||
        property.initializer.elements.some(
          (element) => !compiler.isIdentifier(element),
        )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property.initializer,
            "JAUNT_TS_DEPS_LITERAL",
            "deps must be an array literal of identifiers",
          ),
        );
      } else {
        result.deps = property.initializer.elements.map(
          (element) => (element as ts.Identifier).text,
        );
      }
    } else if (key === "prompt") {
      if (
        compiler.isStringLiteral(property.initializer) ||
        compiler.isNoSubstitutionTemplateLiteral(property.initializer)
      ) {
        result.prompt = property.initializer.text;
      } else {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property.initializer,
            "JAUNT_TS_PROMPT_LITERAL",
            "prompt must be a string literal",
          ),
        );
      }
    } else {
      if (
        property.initializer.kind !== compiler.SyntaxKind.TrueKeyword &&
        property.initializer.kind !== compiler.SyntaxKind.FalseKeyword
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property.initializer,
            "JAUNT_TS_BOOLEAN_LITERAL",
            `${key} must be a boolean literal`,
          ),
        );
      } else {
        const bool =
          property.initializer.kind === compiler.SyntaxKind.TrueKeyword;
        if (key === "inferDeps") result.inferDeps = bool;
        if (key === "test") result.test = bool;
      }
    }
  }
  return result;
}

function mergeOptions(
  moduleOptions: ParsedJauntOptions,
  local: ParsedJauntOptions,
): ParsedJauntOptions {
  return {
    ...(local.deps !== undefined
      ? { deps: local.deps }
      : moduleOptions.deps !== undefined
        ? { deps: moduleOptions.deps }
        : {}),
    ...(local.prompt !== undefined
      ? { prompt: local.prompt }
      : moduleOptions.prompt !== undefined
        ? { prompt: moduleOptions.prompt }
        : {}),
    ...(local.inferDeps !== undefined
      ? { inferDeps: local.inferDeps }
      : moduleOptions.inferDeps !== undefined
        ? { inferDeps: moduleOptions.inferDeps }
        : {}),
    ...(local.test !== undefined
      ? { test: local.test }
      : moduleOptions.test !== undefined
        ? { test: moduleOptions.test }
        : {}),
  };
}

function generatedExampleTestPath(path: string, generatedDir: string): string {
  return join(
    dirname(path),
    generatedDir,
    `${basename(path).replace(/\.jaunt-test\.(?:ts|tsx)$/, "")}.example.test.ts`,
  );
}

function discoverTestSpecFile(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  path: string,
  generatedDir: string,
  projects: readonly LoadedProject[],
  modules: readonly DiscoveredModule[],
  analysisProgram: ts.Program,
): TestSpecDiscovery {
  const source = readFileSync(path, "utf8");
  const sourceFile =
    analysisProgram.getSourceFile(path) ??
    compiler.createSourceFile(
      path,
      source,
      compiler.ScriptTarget.Latest,
      true,
      path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
    );
  const bindings = markerBindingsFor(compiler, sourceFile);
  const diagnostics: DiagnosticRecord[] = syntaxDiagnostics(
    compiler,
    root,
    analysisProgram,
    sourceFile,
  );
  const targetBindings = new Map<string, string>();
  for (const statement of sourceFile.statements) {
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier) ||
      !statement.moduleSpecifier.text.startsWith(".") ||
      !statement.importClause?.namedBindings ||
      !compiler.isNamedImports(statement.importClause.namedBindings)
    ) {
      continue;
    }
    const importedPath = resolve(dirname(path), statement.moduleSpecifier.text);
    const pathCandidates = new Set([
      importedPath,
      importedPath.replace(/\.js$/, ".ts"),
      importedPath.replace(/\.jsx$/, ".tsx"),
    ]);
    const targetModule = modules.find((module) =>
      [module.route.specPath, module.route.facadePath]
        .map((candidate) => resolve(root, candidate))
        .some((candidate) => pathCandidates.has(candidate)),
    );
    if (!targetModule) continue;
    const exported = new Set(targetModule.symbols.map((symbol) => symbol.name));
    for (const element of statement.importClause.namedBindings.elements) {
      const importedName = element.propertyName?.text ?? element.name.text;
      if (!exported.has(importedName)) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            element,
            "JAUNT_TS_TEST_TARGET_UNKNOWN",
            `${importedName} is not a governed export of ${targetModule.route.moduleId}`,
          ),
        );
        continue;
      }
      targetBindings.set(
        element.name.text,
        `${targetModule.route.moduleId}#${importedName}`,
      );
    }
  }
  const moduleCalls = sourceFile.statements.filter(
    (statement): statement is ts.ExpressionStatement =>
      compiler.isExpressionStatement(statement) &&
      compiler.isCallExpression(statement.expression) &&
      isMarkerCall(compiler, statement.expression, "magicModule", bindings),
  );
  if (moduleCalls.length !== 1) {
    diagnostics.push({
      code: "JAUNT_TS_TEST_MAGIC_MODULE_COUNT",
      severity: "error",
      message:
        "A test-spec file must contain exactly one top-level magicModule() call",
      path: toPosix(relative(root, path)),
    });
  } else {
    const expression = moduleCalls[0]?.expression;
    parseOptions(
      compiler,
      expression && compiler.isCallExpression(expression)
        ? expression
        : undefined,
      root,
      sourceFile,
      diagnostics,
    );
  }
  const targets: string[] = [];
  for (const statement of sourceFile.statements) {
    if (
      !compiler.isFunctionDeclaration(statement) ||
      !statement.name ||
      !hasExport(compiler, statement)
    )
      continue;
    const call = magicCallFromBody(
      compiler,
      statement.body,
      bindings,
      "testSpec",
    );
    if (!call) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          statement,
          "JAUNT_TS_TEST_SPEC_STUB",
          `Test spec ${statement.name.text} must contain exactly one jaunt.testSpec(...) call`,
        ),
      );
      continue;
    }
    const options = call.arguments[0];
    if (!options || !compiler.isObjectLiteralExpression(options)) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          call,
          "JAUNT_TS_TEST_OPTIONS",
          "testSpec requires an options object literal",
        ),
      );
      continue;
    }
    const allowed = new Set(["targets", "prompt"]);
    for (const property of options.properties) {
      if (!compiler.isPropertyAssignment(property)) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property,
            "JAUNT_TS_TEST_OPTIONS",
            "testSpec options do not allow spreads",
          ),
        );
        continue;
      }
      const key =
        compiler.isIdentifier(property.name) ||
        compiler.isStringLiteral(property.name)
          ? property.name.text
          : "";
      if (!allowed.has(key)) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property.name,
            "JAUNT_TS_OPTION_UNKNOWN",
            `Unknown testSpec option ${JSON.stringify(key)}`,
          ),
        );
        continue;
      }
      if (key === "targets") {
        if (
          !compiler.isArrayLiteralExpression(property.initializer) ||
          property.initializer.elements.length === 0 ||
          property.initializer.elements.some(
            (element) => !compiler.isIdentifier(element),
          )
        ) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              property.initializer,
              "JAUNT_TS_TEST_TARGETS",
              "targets must be a non-empty array literal of identifiers",
            ),
          );
        } else {
          for (const element of property.initializer
            .elements as ts.NodeArray<ts.Identifier>) {
            const targetId = targetBindings.get(element.text);
            if (targetId) {
              targets.push(targetId);
            } else {
              diagnostics.push(
                diagnosticAt(
                  root,
                  sourceFile,
                  element,
                  "JAUNT_TS_TEST_TARGET_UNRESOLVED",
                  `Test target ${element.text} must be a named import from a discovered Jaunt spec or facade`,
                ),
              );
            }
          }
        }
      } else if (
        !compiler.isStringLiteral(property.initializer) &&
        !compiler.isNoSubstitutionTemplateLiteral(property.initializer)
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            property.initializer,
            "JAUNT_TS_PROMPT_LITERAL",
            "prompt must be a string literal",
          ),
        );
      }
    }
  }
  return {
    record: {
      path: toPosix(relative(root, path)),
      project: testOwnerForPath(
        root,
        projects,
        path,
        generatedExampleTestPath(path, generatedDir),
      ).id,
      targets: [...new Set(targets)].sort(),
    },
    diagnostics: sortDiagnostics(diagnostics),
  };
}

function validateBoundaryType(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceFile: ts.SourceFile,
  node: ts.Node,
  diagnostics: DiagnosticRecord[],
): void {
  function visit(child: ts.Node): void {
    if (child.kind === compiler.SyntaxKind.AnyKeyword) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          child,
          "JAUNT_TS_ANY_BOUNDARY",
          "`any` is forbidden in governed public boundaries",
        ),
      );
    }
    compiler.forEachChild(child, visit);
  }
  visit(node);
}

function importDeclarationFor(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
): ts.ImportDeclaration | undefined {
  let current: ts.Node | undefined = node;
  while (current) {
    if (compiler.isImportDeclaration(current)) return current;
    current = current.parent;
  }
  return undefined;
}

function relativeImportCandidates(
  containingFile: string,
  specifier: string,
): ReadonlySet<string> {
  if (!specifier.startsWith(".")) return new Set();
  const base = resolve(dirname(containingFile), specifier);
  const output = new Set<string>([base]);
  if (base.endsWith(".js")) {
    output.add(base.slice(0, -3) + ".ts");
    output.add(base.slice(0, -3) + ".tsx");
  } else if (base.endsWith(".jsx")) {
    output.add(base.slice(0, -4) + ".tsx");
  }
  return output;
}

function isDeclarationName(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Identifier,
): boolean {
  const parent = node.parent;
  if (
    (compiler.isPropertyAccessExpression(parent) &&
      parent.name === node &&
      !(
        compiler.isIdentifier(parent.expression) &&
        parent.expression.text === "globalThis"
      )) ||
    (compiler.isPropertyAssignment(parent) && parent.name === node) ||
    (compiler.isMethodDeclaration(parent) && parent.name === node) ||
    (compiler.isPropertyDeclaration(parent) && parent.name === node) ||
    (compiler.isGetAccessorDeclaration(parent) && parent.name === node) ||
    (compiler.isSetAccessorDeclaration(parent) && parent.name === node) ||
    (compiler.isBindingElement(parent) && parent.propertyName === node) ||
    (compiler.isLabeledStatement(parent) && parent.label === node) ||
    (compiler.isBreakOrContinueStatement(parent) && parent.label === node)
  ) {
    return true;
  }
  return (
    "name" in parent &&
    (parent as ts.NamedDeclaration).name === node &&
    !compiler.isShorthandPropertyAssignment(parent)
  );
}

function isInsideTypeNode(
  compiler: typeof import("@typescript/typescript6"),
  node: ts.Node,
  body: ts.Block,
): boolean {
  let current: ts.Node | undefined = node.parent;
  while (current && current !== body) {
    if (compiler.isTypeNode(current)) return true;
    current = current.parent;
  }
  return false;
}

function validatePreservedBody(
  compiler: typeof import("@typescript/typescript6"),
  checker: ts.TypeChecker,
  root: string,
  sourceFile: ts.SourceFile,
  body: ts.Block,
  contextPath: string | undefined,
  diagnostics: DiagnosticRecord[],
): readonly string[] {
  const context = contextPath ? resolve(root, contextPath) : undefined;
  const imports = new Set<string>();
  const reported = new Set<number>();
  const standardWithoutSymbols = new Set(["undefined", "NaN", "Infinity"]);

  function report(node: ts.Node, name: string, message?: string): void {
    if (reported.has(node.getStart(sourceFile))) return;
    reported.add(node.getStart(sourceFile));
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        node,
        "JAUNT_TS_PRESERVE_REFERENCE",
        message ??
          `@jauntPreserve may reference only parameters, this, local bindings, standard globals, and paired-context imports; ${name} is outside that closure`,
      ),
    );
  }

  function visit(node: ts.Node): void {
    if (node.kind === compiler.SyntaxKind.SuperKeyword) {
      report(node, "super", "@jauntPreserve may not reference super");
      return;
    }
    if (
      compiler.isCallExpression(node) &&
      node.expression.kind === compiler.SyntaxKind.ImportKeyword
    ) {
      report(
        node,
        "import()",
        "@jauntPreserve may not dynamically import modules; use a static paired-context import",
      );
      return;
    }
    if (
      compiler.isIdentifier(node) &&
      !isDeclarationName(compiler, node) &&
      !isInsideTypeNode(compiler, node, body)
    ) {
      const symbol = checker.getSymbolAtLocation(node);
      if (!symbol) {
        if (!standardWithoutSymbols.has(node.text)) report(node, node.text);
      } else {
        const declarations = symbol.declarations ?? [];
        const local = declarations.some(
          (declaration) =>
            declaration.getSourceFile() === sourceFile &&
            declaration.pos >= body.pos &&
            declaration.end <= body.end,
        );
        const parameter = declarations.some(
          (declaration) =>
            declaration.getSourceFile() === sourceFile &&
            compiler.isParameter(declaration) &&
            declaration.parent === body.parent,
        );
        const standard = declarations.some((declaration) => {
          const file = declaration.getSourceFile();
          return (
            file.isDeclarationFile &&
            /(?:^|[/\\])lib\.[^/\\]+\.d\.ts$/.test(file.fileName)
          );
        });
        if (!local && !parameter && !standard) {
          const imported = declarations
            .map((declaration) => importDeclarationFor(compiler, declaration))
            .find((declaration) => declaration !== undefined);
          if (
            imported &&
            compiler.isStringLiteral(imported.moduleSpecifier) &&
            context &&
            relativeImportCandidates(
              sourceFile.fileName,
              imported.moduleSpecifier.text,
            ).has(context)
          ) {
            imports.add(node.text);
          } else {
            report(
              node,
              node.text,
              imported
                ? `@jauntPreserve runtime import ${JSON.stringify(imported.moduleSpecifier.getText(sourceFile))} is not the paired context module`
                : undefined,
            );
          }
        }
      }
    }
    compiler.forEachChild(node, visit);
  }
  visit(body);
  return [...imports].sort();
}

function classHeritage(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceFile: ts.SourceFile,
  declaration: ts.ClassDeclaration,
  diagnostics: DiagnosticRecord[],
):
  | {
      readonly baseName: string;
      readonly typeArguments: readonly ts.TypeNode[];
      readonly resolvedBaseIds: string[];
    }
  | undefined {
  const clauses = declaration.heritageClauses ?? [];
  const implemented = clauses.find(
    (clause) => clause.token === compiler.SyntaxKind.ImplementsKeyword,
  );
  if (implemented) {
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        implemented,
        "JAUNT_TS_UNSUPPORTED_HERITAGE",
        "Governed concrete classes do not support implements clauses; express the public members directly",
      ),
    );
  }
  const extended = clauses.find(
    (clause) => clause.token === compiler.SyntaxKind.ExtendsKeyword,
  );
  if (!extended) return undefined;
  const base = extended.types[0];
  if (
    extended.types.length !== 1 ||
    !base ||
    !compiler.isIdentifier(base.expression)
  ) {
    diagnostics.push(
      diagnosticAt(
        root,
        sourceFile,
        extended,
        "JAUNT_TS_UNSUPPORTED_HERITAGE",
        "A governed class may extend one statically named class; mixin expressions and qualified bases are unsupported",
      ),
    );
    return undefined;
  }
  return {
    baseName: base.expression.text,
    typeArguments: [...(base.typeArguments ?? [])],
    resolvedBaseIds: [],
  };
}

export function discoverSpecModule(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  path: string,
  generatedDir: string,
  projects: readonly LoadedProject[],
  cachedProgram?: ts.Program,
): DiscoveredModule {
  const source = readFileSync(path, "utf8");
  const rawPaths = makeModuleRoute(root, path, generatedDir, "");
  const owner = ownerForPath(
    root,
    projects,
    resolve(root, rawPaths.facadePath),
  );
  const analysisProgram =
    cachedProgram ??
    compiler.createProgram({
      rootNames: [...new Set([...owner.parsed.fileNames, path])],
      options: { ...owner.parsed.options, noEmit: true },
      ...(owner.parsed.projectReferences
        ? { projectReferences: owner.parsed.projectReferences }
        : {}),
    });
  const sourceFile =
    analysisProgram.getSourceFile(path) ??
    compiler.createSourceFile(
      path,
      source,
      compiler.ScriptTarget.Latest,
      true,
      path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
    );
  const checker = analysisProgram.getTypeChecker();
  const bindings = markerBindingsFor(compiler, sourceFile);
  const diagnostics: DiagnosticRecord[] = syntaxDiagnostics(
    compiler,
    root,
    analysisProgram,
    sourceFile,
  );
  const moduleCalls = sourceFile.statements.filter(
    (statement): statement is ts.ExpressionStatement =>
      compiler.isExpressionStatement(statement) &&
      compiler.isCallExpression(statement.expression) &&
      isMarkerCall(compiler, statement.expression, "magicModule", bindings),
  );
  if (moduleCalls.length !== 1) {
    diagnostics.push({
      code: "JAUNT_TS_MAGIC_MODULE_COUNT",
      severity: "error",
      message:
        "A spec must contain exactly one statically resolved top-level magicModule() call",
      path: toPosix(relative(root, path)),
    });
  }
  const moduleOptions = parseOptions(
    compiler,
    moduleCalls[0] && compiler.isCallExpression(moduleCalls[0].expression)
      ? moduleCalls[0].expression
      : undefined,
    root,
    sourceFile,
    diagnostics,
  );

  const symbols: DiscoveredSymbol[] = [];
  const functions = new Map<string, ts.FunctionDeclaration[]>();
  const typeDeclarations: (
    ts.InterfaceDeclaration | ts.TypeAliasDeclaration
  )[] = [];
  for (const statement of sourceFile.statements) {
    if (
      compiler.canHaveDecorators(statement) &&
      (compiler.getDecorators(statement)?.length ?? 0) > 0
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          statement,
          "JAUNT_TS_DECORATOR",
          "Decorators on governed declarations are unsupported",
        ),
      );
    }
    if (
      compiler.isInterfaceDeclaration(statement) ||
      compiler.isTypeAliasDeclaration(statement)
    ) {
      validateBoundaryType(compiler, root, sourceFile, statement, diagnostics);
      if (!hasExport(compiler, statement)) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            statement,
            "JAUNT_TS_TYPE_EXPORT_REQUIRED",
            "Type declarations used by a Jaunt public contract must be exported",
          ),
        );
      } else {
        typeDeclarations.push(statement);
      }
      continue;
    }
    if (
      compiler.isFunctionDeclaration(statement) &&
      statement.name &&
      hasExport(compiler, statement)
    ) {
      if (
        compiler.canHaveModifiers(statement) &&
        compiler
          .getModifiers(statement)
          ?.some(
            (modifier) => modifier.kind === compiler.SyntaxKind.DefaultKeyword,
          )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            statement,
            "JAUNT_TS_DEFAULT_EXPORT",
            "Governed declarations must use named exports, not default exports",
          ),
        );
      }
      const declarations = functions.get(statement.name.text) ?? [];
      declarations.push(statement);
      functions.set(statement.name.text, declarations);
      validateBoundaryType(compiler, root, sourceFile, statement, diagnostics);
      validateTypedParameters(
        compiler,
        root,
        sourceFile,
        statement.parameters,
        diagnostics,
      );
      requireReturnType(compiler, root, sourceFile, statement, diagnostics);
      continue;
    }
    if (
      compiler.isClassDeclaration(statement) &&
      statement.name &&
      hasExport(compiler, statement)
    ) {
      const classDocs = docsForNode(compiler, sourceFile, statement);
      const designPending = classDocs.tags.jauntDesign !== undefined;
      const heritage = classHeritage(
        compiler,
        root,
        sourceFile,
        statement,
        diagnostics,
      );
      const runtimeImportNames = new Set<string>();
      const classModifiers = compiler.canHaveModifiers(statement)
        ? compiler.getModifiers(statement)
        : undefined;
      if (
        classModifiers?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.DefaultKeyword,
        )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            statement,
            "JAUNT_TS_DEFAULT_EXPORT",
            "Governed declarations must use named exports, not default exports",
          ),
        );
      }
      if (
        classModifiers?.some(
          (modifier) => modifier.kind === compiler.SyntaxKind.AbstractKeyword,
        )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            statement,
            "JAUNT_TS_ABSTRACT_CLASS",
            "Abstract governed classes are not supported",
          ),
        );
      }
      validateBoundaryType(compiler, root, sourceFile, statement, diagnostics);
      if (heritage) runtimeImportNames.add(heritage.baseName);
      const memberOptions: ParsedJauntOptions[] = [];
      for (const member of statement.members) {
        if (
          member.name &&
          !compiler.isIdentifier(member.name) &&
          !compiler.isStringLiteral(member.name) &&
          !compiler.isNumericLiteral(member.name) &&
          !compiler.isPrivateIdentifier(member.name)
        ) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member.name,
              "JAUNT_TS_UNSUPPORTED_MEMBER_NAME",
              "Computed governed class member names cannot be represented by strict adapters",
            ),
          );
        }
        if (
          compiler.canHaveDecorators(member) &&
          (compiler.getDecorators(member)?.length ?? 0) > 0
        ) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_DECORATOR",
              "Decorators on governed class members are unsupported",
            ),
          );
        }
        if (compiler.isClassStaticBlockDeclaration(member)) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_STATIC_BLOCK",
              "Static blocks are unsupported in spec classes",
            ),
          );
          continue;
        }
        if (compiler.isPropertyDeclaration(member) && member.initializer) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member.initializer,
              "JAUNT_TS_RUNTIME_INITIALIZER",
              "Runtime field initializers are unsupported in specs",
            ),
          );
        }
        if (compiler.isPropertyDeclaration(member) && !member.type) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_EXPLICIT_TYPE_REQUIRED",
              "Every governed public property requires an explicit type",
            ),
          );
        }
        if (
          compiler.isConstructorDeclaration(member) ||
          compiler.isMethodDeclaration(member) ||
          compiler.isGetAccessorDeclaration(member) ||
          compiler.isSetAccessorDeclaration(member)
        ) {
          validateTypedParameters(
            compiler,
            root,
            sourceFile,
            member.parameters,
            diagnostics,
          );
          if (
            member.parameters.some((parameter) =>
              (compiler.getModifiers(parameter) ?? []).some((modifier) =>
                [
                  compiler.SyntaxKind.PublicKeyword,
                  compiler.SyntaxKind.PrivateKeyword,
                  compiler.SyntaxKind.ProtectedKeyword,
                  compiler.SyntaxKind.ReadonlyKeyword,
                ].includes(modifier.kind),
              ),
            )
          ) {
            diagnostics.push(
              diagnosticAt(
                root,
                sourceFile,
                member,
                "JAUNT_TS_PARAMETER_PROPERTY",
                "Constructor parameter properties are unsupported; declare the public field separately",
              ),
            );
          }
          if (
            compiler.isMethodDeclaration(member) &&
            member.typeParameters?.some((parameter) =>
              statement.typeParameters?.some(
                (classParameter) =>
                  classParameter.name.text === parameter.name.text,
              ),
            )
          ) {
            diagnostics.push(
              diagnosticAt(
                root,
                sourceFile,
                member,
                "JAUNT_TS_GENERIC_SHADOW",
                "A governed method type parameter may not shadow a class type parameter",
              ),
            );
          }
          if (
            compiler.isMethodDeclaration(member) ||
            compiler.isGetAccessorDeclaration(member)
          ) {
            requireReturnType(compiler, root, sourceFile, member, diagnostics);
          }
        }
        const modifiers = compiler.canHaveModifiers(member)
          ? compiler.getModifiers(member)
          : undefined;
        if (
          modifiers?.some(
            (modifier) => modifier.kind === compiler.SyntaxKind.AbstractKeyword,
          )
        ) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_ABSTRACT_MEMBER",
              "Abstract members are unsupported in governed concrete classes",
            ),
          );
        }
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
              "JAUNT_TS_NOMINAL_MEMBER",
              "Authored private/protected class members are unsupported",
            ),
          );
        }
        const memberCall =
          compiler.isConstructorDeclaration(member) ||
          compiler.isMethodDeclaration(member) ||
          compiler.isGetAccessorDeclaration(member) ||
          compiler.isSetAccessorDeclaration(member)
            ? magicCallFromBody(compiler, member.body, bindings)
            : undefined;
        const memberDocs = docsForNode(compiler, sourceFile, member);
        const preserved = memberDocs.tags.jauntPreserve !== undefined;
        if (preserved) {
          if (
            !compiler.isMethodDeclaration(member) &&
            !compiler.isGetAccessorDeclaration(member) &&
            !compiler.isSetAccessorDeclaration(member)
          ) {
            diagnostics.push(
              diagnosticAt(
                root,
                sourceFile,
                member,
                "JAUNT_TS_PRESERVE_SHAPE",
                "@jauntPreserve is supported only on concrete methods and accessors",
              ),
            );
          } else if (!member.body || memberCall) {
            diagnostics.push(
              diagnosticAt(
                root,
                sourceFile,
                member,
                "JAUNT_TS_PRESERVE_SHAPE",
                "@jauntPreserve requires one real authored body and cannot also call jaunt.magic()",
              ),
            );
          } else {
            for (const name of validatePreservedBody(
              compiler,
              checker,
              root,
              sourceFile,
              member.body,
              rawPaths.contextPath,
              diagnostics,
            )) {
              runtimeImportNames.add(name);
            }
          }
        }
        if (memberCall) {
          memberOptions.push(
            parseOptions(compiler, memberCall, root, sourceFile, diagnostics),
          );
        }
        if (
          (compiler.isConstructorDeclaration(member) ||
            compiler.isMethodDeclaration(member) ||
            compiler.isGetAccessorDeclaration(member) ||
            compiler.isSetAccessorDeclaration(member)) &&
          member.body !== undefined &&
          !preserved &&
          !magicCallFromBody(compiler, member.body, bindings)
        ) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              member,
              "JAUNT_TS_CLASS_MEMBER_STUB",
              "Governed class members must contain exactly one jaunt.magic() call or carry @jauntPreserve",
            ),
          );
        }
      }
      const overloadGroups = new Map<
        string,
        (ts.ConstructorDeclaration | ts.MethodDeclaration)[]
      >();
      for (const member of statement.members) {
        if (
          !compiler.isConstructorDeclaration(member) &&
          !compiler.isMethodDeclaration(member)
        )
          continue;
        const name = compiler.isConstructorDeclaration(member)
          ? "constructor"
          : compiler.isIdentifier(member.name) ||
              compiler.isStringLiteral(member.name)
            ? member.name.text
            : member.name.getText(sourceFile);
        const staticMember =
          compiler.canHaveModifiers(member) &&
          (compiler
            .getModifiers(member)
            ?.some(
              (modifier) => modifier.kind === compiler.SyntaxKind.StaticKeyword,
            ) ??
            false);
        const key = `${staticMember ? "static" : "instance"}:${name}`;
        const members = overloadGroups.get(key) ?? [];
        members.push(member);
        overloadGroups.set(key, members);
      }
      for (const [name, members] of overloadGroups) {
        if (
          !designPending &&
          members.filter((member) => member.body !== undefined).length !== 1
        ) {
          diagnostics.push({
            code: "JAUNT_TS_CLASS_OVERLOAD_IMPLEMENTATION",
            severity: "error",
            message: `Class overload group ${name} must have exactly one canonical jaunt.magic() implementation`,
            path: toPosix(relative(root, path)),
          });
        }
        if (
          members.length > 1 &&
          members.some(
            (member) =>
              docsForNode(compiler, sourceFile, member).tags.jauntPreserve !==
              undefined,
          )
        ) {
          diagnostics.push({
            code: "JAUNT_TS_PRESERVE_OVERLOAD",
            severity: "error",
            message: `@jauntPreserve on overloaded class member ${name} is unsupported; move the preserved behavior behind a non-overloaded member`,
            path: toPosix(relative(root, path)),
          });
        }
      }
      const memberPrompt = memberOptions.find(
        (options) => options.prompt !== undefined,
      )?.prompt;
      const membersWithDependencies = memberOptions.filter(
        (options) => options.deps !== undefined,
      );
      const memberDependencies =
        membersWithDependencies.length === 0
          ? undefined
          : membersWithDependencies
              .flatMap((options) => options.deps ?? [])
              .filter(
                (value, index, values) => values.indexOf(value) === index,
              );
      const options = mergeOptions(moduleOptions, {
        ...(memberDependencies === undefined
          ? {}
          : { deps: memberDependencies }),
        ...(memberPrompt === undefined ? {} : { prompt: memberPrompt }),
      });
      symbols.push({
        kind: "class",
        name: statement.name.text,
        declaration: statement,
        docs: classDocs,
        ...(heritage ? { heritage } : {}),
        runtimeImportNames: [...runtimeImportNames].sort(),
        dependencies: options.deps ?? [],
        resolvedDependencies: [],
        options,
      });
      continue;
    }
    if (
      (compiler.isFunctionDeclaration(statement) ||
        compiler.isClassDeclaration(statement)) &&
      !hasExport(compiler, statement)
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          statement,
          "JAUNT_TS_RUNTIME_DECLARATION",
          "Spec modules may not contain non-exported runtime functions or classes",
        ),
      );
      continue;
    }
    const isMagicModuleStatement =
      compiler.isExpressionStatement(statement) &&
      compiler.isCallExpression(statement.expression) &&
      isMarkerCall(compiler, statement.expression, "magicModule", bindings);
    if (
      !compiler.isImportDeclaration(statement) &&
      !compiler.isExportDeclaration(statement) &&
      !compiler.isInterfaceDeclaration(statement) &&
      !compiler.isTypeAliasDeclaration(statement) &&
      !compiler.isFunctionDeclaration(statement) &&
      !compiler.isClassDeclaration(statement) &&
      !isMagicModuleStatement
    ) {
      diagnostics.push(
        diagnosticAt(
          root,
          sourceFile,
          statement,
          "JAUNT_TS_EXECUTABLE_SPEC_DECLARATION",
          "Spec modules may contain only imports, magicModule(), governed functions/classes, and type-only interfaces/type aliases",
        ),
      );
    }
  }

  for (const [name, declarations] of functions) {
    const implementations = declarations.filter(
      (declaration) => declaration.body !== undefined,
    );
    const implementation = implementations[0];
    const call = implementation
      ? magicCallFromBody(compiler, implementation.body, bindings)
      : undefined;
    const localOptions = parseOptions(
      compiler,
      call,
      root,
      sourceFile,
      diagnostics,
    );
    const options = mergeOptions(moduleOptions, localOptions);
    const docs = docsForNode(
      compiler,
      sourceFile,
      declarations[0] ?? implementation ?? sourceFile,
    );
    const designPending = docs.tags.jauntDesign !== undefined;
    if (!designPending && (implementations.length !== 1 || !call)) {
      diagnostics.push({
        code: "JAUNT_TS_FUNCTION_STUB",
        severity: "error",
        message: `Governed function ${name} must have exactly one implementation containing only jaunt.magic()`,
        path: toPosix(relative(root, path)),
      });
    }
    symbols.push({
      kind: "function",
      name,
      declarations,
      docs,
      dependencies: options.deps ?? [],
      resolvedDependencies: [],
      options,
    });
  }

  symbols.sort((left, right) => left.name.localeCompare(right.name));
  for (const symbol of symbols) {
    if (!symbol.docs.text.trim()) {
      diagnostics.push({
        code: "JAUNT_TS_DOCS_REQUIRED",
        severity: "error",
        message: `Governed ${symbol.kind} ${symbol.name} requires a TSDoc behavioral contract`,
        path: toPosix(relative(root, path)),
      });
    }
  }
  const route = { ...rawPaths, project: owner.id };
  return {
    route,
    sourceFile,
    source,
    markerBindings: bindings,
    moduleOptions,
    resolvedModuleDependencies: [],
    dependencyResolutionComplete: false,
    dependencyModules: [],
    compilerOptions: owner.parsed.options,
    symbols,
    typeDeclarations,
    diagnostics: sortDiagnostics(diagnostics),
  };
}

function includePatterns(roots: readonly string[], suffix: string): string[] {
  return roots.map((root) => `${root.replace(/\/$/, "")}/**/*${suffix}`);
}

interface DependencyImportBinding {
  readonly localName: string;
  readonly importedName: string;
  readonly specifier: string;
  readonly typeOnly: boolean;
  readonly declaration: ts.ImportDeclaration;
  readonly node: ts.ImportSpecifier | ts.Identifier;
}

interface WorkspaceDependencyResolution {
  readonly diagnostics: readonly DiagnosticRecord[];
  readonly allowedImportDiagnostics: ReadonlySet<string>;
}

function importBindingsForDependencies(
  compiler: typeof import("@typescript/typescript6"),
  module: DiscoveredModule,
): ReadonlyMap<string, readonly DependencyImportBinding[]> {
  const bindings = new Map<string, DependencyImportBinding[]>();
  for (const statement of module.sourceFile.statements) {
    if (
      !compiler.isImportDeclaration(statement) ||
      !compiler.isStringLiteral(statement.moduleSpecifier) ||
      !statement.importClause
    ) {
      continue;
    }
    if (statement.importClause.name) {
      bindings.set(statement.importClause.name.text, [
        {
          localName: statement.importClause.name.text,
          importedName: "default",
          specifier: statement.moduleSpecifier.text,
          typeOnly: statement.importClause.isTypeOnly,
          declaration: statement,
          node: statement.importClause.name,
        },
      ]);
    }
    if (
      !statement.importClause.namedBindings ||
      !compiler.isNamedImports(statement.importClause.namedBindings)
    )
      continue;
    for (const element of statement.importClause.namedBindings.elements) {
      const binding: DependencyImportBinding = {
        localName: element.name.text,
        importedName: element.propertyName?.text ?? element.name.text,
        specifier: statement.moduleSpecifier.text,
        typeOnly:
          statement.importClause.isTypeOnly || element.isTypeOnly === true,
        declaration: statement,
        node: element,
      };
      const existing = bindings.get(binding.localName) ?? [];
      existing.push(binding);
      bindings.set(binding.localName, existing);
    }
  }
  return bindings;
}

function importPathCandidates(
  containingFile: string,
  specifier: string,
): ReadonlySet<string> {
  if (!specifier.startsWith(".")) return new Set();
  const base = resolve(dirname(containingFile), specifier);
  const candidates = new Set<string>([base]);
  if (base.endsWith(".js")) {
    candidates.add(base.replace(/\.js$/, ".ts"));
    candidates.add(base.replace(/\.js$/, ".tsx"));
  } else if (base.endsWith(".jsx")) {
    candidates.add(base.replace(/\.jsx$/, ".tsx"));
  } else if (!/\.[A-Za-z0-9]+$/.test(base)) {
    candidates.add(`${base}.ts`);
    candidates.add(`${base}.tsx`);
    candidates.add(join(base, "index.ts"));
    candidates.add(join(base, "index.tsx"));
  }
  return candidates;
}

function diagnosticKey(
  record: Pick<DiagnosticRecord, "path" | "start">,
): string {
  return `${record.path ?? ""}:${record.start ?? -1}`;
}

function modulesForImport(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  containingFile: string,
  specifier: string,
  modules: readonly DiscoveredModule[],
  compilerOptions: ts.CompilerOptions,
): readonly DiscoveredModule[] {
  const paths = new Set(importPathCandidates(containingFile, specifier));
  const virtualPaths = new Set(
    modules.flatMap((module) =>
      [
        module.route.specPath,
        module.route.facadePath,
        module.route.apiMirrorPath,
        module.route.implementationPath,
      ].map((path) => resolve(root, path)),
    ),
  );
  const host: ts.ModuleResolutionHost = {
    ...compiler.sys,
    fileExists: (path) =>
      virtualPaths.has(resolve(path)) || compiler.sys.fileExists(path),
  };
  const resolved = compiler.resolveModuleName(
    specifier,
    containingFile,
    compilerOptions,
    host,
  ).resolvedModule?.resolvedFileName;
  if (resolved) paths.add(resolve(resolved));
  return modules.filter((candidate) =>
    [candidate.route.specPath, candidate.route.facadePath]
      .map((path) => resolve(root, path))
      .some((path) => paths.has(path)),
  );
}

/**
 * Resolve marker-option identifiers against statically parsed named imports.
 * Nothing here loads or evaluates a spec module: imports are syntax edges only.
 */
function resolveWorkspaceDependencies(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  modules: readonly DiscoveredModule[],
  projects: readonly LoadedProject[],
): WorkspaceDependencyResolution {
  const diagnostics: DiagnosticRecord[] = [];
  const allowedImportDiagnostics = new Set<string>();
  // Unchanged modules may be retained across worker invalidations.  The
  // workspace binding pass is global, so clear only its mutable projections
  // before resolving the new module set.
  for (const module of modules) {
    module.dependencyModules.splice(0, module.dependencyModules.length);
    module.resolvedModuleDependencies.splice(
      0,
      module.resolvedModuleDependencies.length,
    );
    module.dependencyResolutionComplete = false;
    for (const symbol of module.symbols) {
      symbol.resolvedDependencies.splice(0, symbol.resolvedDependencies.length);
      if (symbol.kind === "class" && symbol.heritage) {
        symbol.heritage.resolvedBaseIds.splice(
          0,
          symbol.heritage.resolvedBaseIds.length,
        );
      }
    }
  }
  const moduleById = new Map(
    modules.map((module) => [module.route.moduleId, module] as const),
  );
  const symbolById = new Map<string, DiscoveredModule>();
  for (const module of modules) {
    for (const symbol of module.symbols) {
      symbolById.set(`${module.route.moduleId}#${symbol.name}`, module);
    }
  }

  for (const module of modules) {
    const imported = importBindingsForDependencies(compiler, module);
    const localSymbols = new Set(module.symbols.map((symbol) => symbol.name));
    const resolvedByName = new Map<string, string | undefined>();
    for (const statement of module.sourceFile.statements) {
      if (
        !compiler.isImportDeclaration(statement) ||
        !compiler.isStringLiteral(statement.moduleSpecifier) ||
        statement.importClause?.isTypeOnly === true
      ) {
        continue;
      }
      const hasValueBinding =
        statement.importClause?.name !== undefined ||
        (statement.importClause?.namedBindings !== undefined &&
          (compiler.isNamespaceImport(statement.importClause.namedBindings) ||
            statement.importClause.namedBindings.elements.some(
              (element) => !element.isTypeOnly,
            )));
      if (!hasValueBinding) continue;
      const targets = modulesForImport(
        compiler,
        root,
        module.sourceFile.fileName,
        statement.moduleSpecifier.text,
        modules,
        module.compilerOptions,
      );
      if (
        targets.length === 1 &&
        targets[0]!.route.moduleId !== module.route.moduleId &&
        projectReferencesProject(
          projects,
          module.route.project,
          targets[0]!.route.project,
        )
      ) {
        const importDiagnostic = diagnosticAt(
          root,
          module.sourceFile,
          statement,
          "",
          "",
        );
        allowedImportDiagnostics.add(diagnosticKey(importDiagnostic));
      }
    }
    const allNames = new Set([
      ...(module.moduleOptions.deps ?? []),
      ...module.symbols.flatMap((symbol) => symbol.dependencies),
    ]);

    for (const name of [...allNames].sort()) {
      const local = localSymbols.has(name);
      const candidates = imported.get(name) ?? [];
      const diagnosticNode = candidates[0]?.node ?? module.sourceFile;
      if ((local ? 1 : 0) + candidates.length > 1) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            diagnosticNode,
            "JAUNT_TS_DEPENDENCY_AMBIGUOUS",
            `Dependency identifier ${name} is ambiguous in ${module.route.moduleId}`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      if (local) {
        resolvedByName.set(name, `${module.route.moduleId}#${name}`);
        continue;
      }
      const binding = candidates[0];
      if (!binding) {
        diagnostics.push({
          code: "JAUNT_TS_DEPENDENCY_UNKNOWN",
          severity: "error",
          message: `Dependency ${name} is neither a governed local symbol nor a named import from a discovered Jaunt module`,
          path: module.route.specPath,
        });
        resolvedByName.set(name, undefined);
        continue;
      }
      if (binding.typeOnly) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            binding.node,
            "JAUNT_TS_DEPENDENCY_VALUE_IMPORT_REQUIRED",
            `Dependency ${name} must be a value import so the marker expression is valid TypeScript`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      const targets = modulesForImport(
        compiler,
        root,
        module.sourceFile.fileName,
        binding.specifier,
        modules,
        module.compilerOptions,
      );
      if (targets.length > 1) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            binding.node,
            "JAUNT_TS_DEPENDENCY_AMBIGUOUS",
            `Dependency import ${binding.specifier} matches several Jaunt modules: ${targets
              .map((target) => target.route.moduleId)
              .sort()
              .join(", ")}`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      const target = targets[0];
      if (!target) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            binding.node,
            "JAUNT_TS_DEPENDENCY_UNKNOWN",
            `Dependency ${name} does not resolve to a discovered Jaunt spec or facade`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      if (
        !projectReferencesProject(
          projects,
          module.route.project,
          target.route.project,
        )
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            binding.node,
            "JAUNT_TS_DEPENDENCY_CROSS_PROJECT",
            `Dependency ${name} crosses TypeScript projects without a project-reference path (${module.route.project} -> ${target.route.project})`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      if (
        !target.symbols.some((symbol) => symbol.name === binding.importedName)
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            module.sourceFile,
            binding.node,
            "JAUNT_TS_DEPENDENCY_UNKNOWN",
            `${binding.importedName} is not a governed export of ${target.route.moduleId}`,
          ),
        );
        resolvedByName.set(name, undefined);
        continue;
      }
      const id = `${target.route.moduleId}#${binding.importedName}`;
      resolvedByName.set(name, id);
      if (target.route.moduleId !== module.route.moduleId) {
        if (!module.dependencyModules.includes(target)) {
          module.dependencyModules.push(target);
        }
        const importDiagnostic = diagnosticAt(
          root,
          module.sourceFile,
          binding.declaration,
          "",
          "",
        );
        allowedImportDiagnostics.add(diagnosticKey(importDiagnostic));
      }
    }

    const resolveNames = (names: readonly string[]): string[] =>
      names.flatMap((name) => {
        const dependency = resolvedByName.get(name);
        return dependency === undefined ? [] : [dependency];
      });
    const heritageDependencies = new Map<DiscoveredClass, string[]>();
    const standardBases = new Set([
      "Array",
      "Error",
      "Map",
      "RegExp",
      "Set",
      "WeakMap",
      "WeakSet",
    ]);
    for (const symbol of module.symbols) {
      if (symbol.kind !== "class" || !symbol.heritage) continue;
      const baseName = symbol.heritage.baseName;
      const localBase = module.symbols.find(
        (candidate) => candidate.name === baseName,
      );
      let resolvedBase: string | undefined;
      if (localBase) {
        if (localBase.kind !== "class") {
          diagnostics.push({
            code: "JAUNT_TS_UNSUPPORTED_HERITAGE",
            severity: "error",
            message: `${symbol.name} extends governed non-class symbol ${baseName}`,
            path: module.route.specPath,
          });
        } else {
          resolvedBase = `${module.route.moduleId}#${baseName}`;
        }
      } else {
        const candidates = imported.get(baseName) ?? [];
        if (candidates.length > 1) {
          diagnostics.push(
            diagnosticAt(
              root,
              module.sourceFile,
              candidates[0]!.node,
              "JAUNT_TS_UNSUPPORTED_HERITAGE",
              `Base class ${baseName} is imported ambiguously`,
            ),
          );
        } else if (candidates[0]) {
          const binding = candidates[0];
          if (binding.typeOnly) {
            diagnostics.push(
              diagnosticAt(
                root,
                module.sourceFile,
                binding.node,
                "JAUNT_TS_HERITAGE_VALUE_IMPORT_REQUIRED",
                `Base class ${baseName} requires a runtime value import`,
              ),
            );
          } else {
            const targets = modulesForImport(
              compiler,
              root,
              module.sourceFile.fileName,
              binding.specifier,
              modules,
              module.compilerOptions,
            );
            if (targets.length === 1) {
              const target = targets[0]!;
              const targetSymbol = target.symbols.find(
                (candidate) => candidate.name === binding.importedName,
              );
              if (!targetSymbol || targetSymbol.kind !== "class") {
                diagnostics.push(
                  diagnosticAt(
                    root,
                    module.sourceFile,
                    binding.node,
                    "JAUNT_TS_UNSUPPORTED_HERITAGE",
                    `${binding.importedName} is not a governed class`,
                  ),
                );
              } else if (
                !projectReferencesProject(
                  projects,
                  module.route.project,
                  target.route.project,
                )
              ) {
                diagnostics.push(
                  diagnosticAt(
                    root,
                    module.sourceFile,
                    binding.node,
                    "JAUNT_TS_DEPENDENCY_CROSS_PROJECT",
                    `Base class ${baseName} crosses TypeScript projects without a project-reference path (${module.route.project} -> ${target.route.project})`,
                  ),
                );
              } else {
                resolvedBase = `${target.route.moduleId}#${binding.importedName}`;
                if (!module.dependencyModules.includes(target))
                  module.dependencyModules.push(target);
                allowedImportDiagnostics.add(
                  diagnosticKey(
                    diagnosticAt(
                      root,
                      module.sourceFile,
                      binding.declaration,
                      "",
                      "",
                    ),
                  ),
                );
              }
            } else if (
              targets.length > 1 ||
              /\.jaunt\.(?:js|ts|tsx)$/.test(binding.specifier)
            ) {
              diagnostics.push(
                diagnosticAt(
                  root,
                  module.sourceFile,
                  binding.node,
                  "JAUNT_TS_UNSUPPORTED_HERITAGE",
                  `Base class ${baseName} does not resolve to one representable governed class`,
                ),
              );
            }
            // Ordinary context/package classes are runtime-imported directly.
          }
        } else if (!standardBases.has(baseName)) {
          diagnostics.push({
            code: "JAUNT_TS_UNSUPPORTED_HERITAGE",
            severity: "error",
            message: `Base class ${baseName} must be a standard global, a named runtime import, or another governed class`,
            path: module.route.specPath,
          });
        }
      }
      if (resolvedBase) {
        symbol.heritage.resolvedBaseIds.splice(
          0,
          symbol.heritage.resolvedBaseIds.length,
          resolvedBase,
        );
        heritageDependencies.set(symbol, [resolvedBase]);
      }
    }
    module.resolvedModuleDependencies.splice(
      0,
      module.resolvedModuleDependencies.length,
      ...resolveNames(module.moduleOptions.deps ?? []),
    );
    for (const symbol of module.symbols) {
      symbol.resolvedDependencies.splice(
        0,
        symbol.resolvedDependencies.length,
        ...[
          ...resolveNames(symbol.dependencies),
          ...(symbol.kind === "class"
            ? (heritageDependencies.get(symbol) ?? [])
            : []),
        ].filter((value, index, values) => values.indexOf(value) === index),
      );
    }
    module.dependencyResolutionComplete = true;
  }

  const adjacency = new Map<string, readonly string[]>();
  for (const module of modules) {
    for (const symbol of module.symbols) {
      adjacency.set(
        `${module.route.moduleId}#${symbol.name}`,
        symbol.resolvedDependencies,
      );
    }
  }
  const state = new Map<string, "visiting" | "visited">();
  const stack: string[] = [];
  const reported = new Set<string>();
  function visit(id: string): void {
    const existing = state.get(id);
    if (existing === "visited") return;
    if (existing === "visiting") return;
    state.set(id, "visiting");
    stack.push(id);
    for (const dependency of adjacency.get(id) ?? []) {
      if (!adjacency.has(dependency)) continue;
      if (state.get(dependency) === "visiting") {
        const start = stack.indexOf(dependency);
        const cycle = [...stack.slice(Math.max(0, start)), dependency];
        const key = [...new Set(cycle)].sort().join("\0");
        if (!reported.has(key)) {
          reported.add(key);
          diagnostics.push({
            code: "JAUNT_TS_DEPENDENCY_CYCLE",
            severity: "error",
            message: `Jaunt dependency cycle: ${cycle.join(" -> ")}`,
            ...(symbolById.get(id)
              ? { path: symbolById.get(id)!.route.specPath }
              : {}),
          });
        }
        continue;
      }
      visit(dependency);
    }
    stack.pop();
    state.set(id, "visited");
  }
  for (const id of [...adjacency.keys()].sort()) visit(id);

  // Sanity-check every edge even if a future resolver starts synthesizing IDs.
  for (const [id, dependencies] of adjacency) {
    for (const dependency of dependencies) {
      if (!symbolById.has(dependency)) {
        diagnostics.push({
          code: "JAUNT_TS_DEPENDENCY_UNKNOWN",
          severity: "error",
          message: `${id} resolves to unknown dependency ${dependency}`,
          ...(symbolById.get(id)
            ? { path: symbolById.get(id)!.route.specPath }
            : {}),
        });
      }
    }
  }
  for (const module of modules) {
    module.dependencyModules.sort((left, right) =>
      left.route.moduleId.localeCompare(right.route.moduleId),
    );
  }
  // Keep this lookup live so accidental duplicate module IDs do not silently
  // turn one dependency into another before route-collision diagnostics run.
  if (moduleById.size !== modules.length) {
    diagnostics.push({
      code: "JAUNT_TS_DEPENDENCY_AMBIGUOUS",
      severity: "error",
      message:
        "Duplicate Jaunt module IDs make dependency resolution ambiguous",
    });
  }
  return {
    diagnostics: sortDiagnostics(diagnostics),
    allowedImportDiagnostics,
  };
}

export interface DiscoveryRefreshOptions {
  readonly programCache?: AnalysisProgramCache;
  readonly previous?: DiscoveryResult;
  readonly invalidatedPaths?: readonly string[];
}

export function discoverWorkspace(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  sourceRoots: readonly string[],
  testRoots: readonly string[],
  generatedDir: string,
  projects: readonly LoadedProject[],
  refresh: DiscoveryRefreshOptions = {},
): DiscoveryResult {
  const excludes = [
    "**/node_modules/**",
    "**/dist/**",
    `**/${generatedDir}/**`,
  ];
  const specFiles = compiler.sys
    .readDirectory(root, [".ts", ".tsx"], excludes, [
      ...includePatterns(sourceRoots, ".jaunt.ts"),
      ...includePatterns(sourceRoots, ".jaunt.tsx"),
    ])
    .sort();
  const testFiles = compiler.sys
    .readDirectory(root, [".ts", ".tsx"], excludes, [
      ...includePatterns(testRoots, ".jaunt-test.ts"),
      ...includePatterns(testRoots, ".jaunt-test.tsx"),
    ])
    .sort();
  const specOwners = new Map(
    specFiles.map((path) => {
      const route = makeModuleRoute(root, path, generatedDir, "");
      return [
        path,
        ownerForPath(root, projects, resolve(root, route.facadePath)).id,
      ] as const;
    }),
  );
  const testOwners = new Map(
    testFiles.map(
      (path) =>
        [
          path,
          testOwnerForPath(
            root,
            projects,
            path,
            generatedExampleTestPath(path, generatedDir),
          ).id,
        ] as const,
    ),
  );
  const extraRoots = new Map<string, string[]>();
  for (const [path, projectId] of [...specOwners, ...testOwners]) {
    const values = extraRoots.get(projectId) ?? [];
    values.push(path);
    extraRoots.set(projectId, values);
  }
  const programCache =
    refresh.programCache ?? new AnalysisProgramCache(compiler, root);
  programCache.prepare(projects, extraRoots, refresh.invalidatedPaths ?? []);
  const previousModules = new Map(
    (refresh.previous?.modules ?? []).map((module) => [
      resolve(root, module.route.specPath),
      module,
    ]),
  );
  const modules = specFiles.map((path) => {
    const projectId = specOwners.get(path)!;
    const previous = previousModules.get(resolve(path));
    if (
      previous &&
      programCache.reused(projectId) &&
      previous.source === readFileSync(path, "utf8")
    ) {
      return previous;
    }
    return discoverSpecModule(
      compiler,
      root,
      path,
      generatedDir,
      projects,
      programCache.programFor(projectId),
    );
  });
  const routeDiagnostics: DiagnosticRecord[] = [];
  const claimedRoutes = new Map<
    string,
    { moduleId: string; specPath: string; path: string }
  >();
  for (const module of modules) {
    for (const path of [
      module.route.specPath,
      module.route.facadePath,
      module.route.apiMirrorPath,
      module.route.implementationPath,
      module.route.sidecarPath,
    ]) {
      // Generated artifacts are committed and commonly consumed on a different
      // platform from the one running discovery. Reject case-only collisions
      // even on a case-sensitive host.
      const key = path.replaceAll("\\", "/").toLowerCase();
      const previous = claimedRoutes.get(key);
      if (previous && previous.specPath !== module.route.specPath) {
        routeDiagnostics.push({
          code: "JAUNT_TS_ROUTE_COLLISION",
          severity: "error",
          message: `${module.route.moduleId} and ${previous.moduleId} both claim ${path}`,
          path: module.route.specPath,
        });
      } else {
        claimedRoutes.set(key, {
          moduleId: module.route.moduleId,
          specPath: module.route.specPath,
          path,
        });
      }
    }
  }
  const dependencyResolution = resolveWorkspaceDependencies(
    compiler,
    root,
    modules,
    projects,
  );
  const discoveredTests = testFiles.map((path) =>
    discoverTestSpecFile(
      compiler,
      root,
      path,
      generatedDir,
      projects,
      modules,
      programCache.programFor(testOwners.get(path)!),
    ),
  );
  const testSpecs = discoveredTests.map((item) => item.record);
  const contractFiles = compiler.sys
    .readDirectory(
      root,
      [".ts", ".tsx"],
      excludes,
      sourceRoots.map((entry) => `${entry}/**/*`),
    )
    .filter((path) => !/\.jaunt(?:-test)?\.(?:ts|tsx)$/.test(path));
  const contracts: DiscoveredContract[] = [];
  for (const path of contractFiles) {
    const source = readFileSync(path, "utf8");
    if (!source.includes("@jauntContract")) continue;
    const sourceFile = compiler.createSourceFile(
      path,
      source,
      compiler.ScriptTarget.Latest,
      true,
    );
    const symbols = sourceFile.statements
      .filter(
        (node): node is ts.FunctionDeclaration | ts.ClassDeclaration =>
          (compiler.isFunctionDeclaration(node) ||
            compiler.isClassDeclaration(node)) &&
          docsForNode(compiler, sourceFile, node).tags.jauntContract !==
            undefined,
      )
      .flatMap((node) => (node.name ? [node.name.text] : []));
    contracts.push({
      path: toPosix(relative(root, path)),
      project: ownerForPath(root, projects, path).id,
      symbols,
    });
  }
  const importFiles = compiler.sys.readDirectory(
    root,
    [".ts", ".tsx"],
    excludes,
    [...sourceRoots, ...testRoots].map((entry) => `${entry}/**/*`),
  );
  const importDiagnostics = analyzeImportGraph(
    compiler,
    root,
    importFiles,
    modules.map((module) => module.route),
    projects,
    testRoots,
  ).filter(
    (diagnostic) =>
      diagnostic.code !== "JAUNT_TS_CROSS_MODULE_DEPENDENCY_UNSUPPORTED" ||
      !dependencyResolution.allowedImportDiagnostics.has(
        diagnosticKey(diagnostic),
      ),
  );
  const diagnostics = sortDiagnostics([
    ...modules.flatMap((module) => module.diagnostics),
    ...routeDiagnostics,
    ...dependencyResolution.diagnostics,
    ...discoveredTests.flatMap((item) => item.diagnostics),
    ...importDiagnostics,
  ]);
  if (diagnostics.some((item) => item.severity === "error")) {
    // Keep structured records in the result; callers decide whether this is fatal.
  }
  return {
    modules,
    routes: modules.map((module) => module.route),
    specs: modules.map((module) => ({
      moduleId: module.route.moduleId,
      specPath: module.route.specPath,
      project: module.route.project,
      packageOwner: module.route.packageOwner,
      symbols: module.symbols.map((symbol) => symbol.name),
    })),
    testSpecs,
    contracts,
    diagnostics,
  };
}
