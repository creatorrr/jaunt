import { readFileSync } from "node:fs";
import { dirname, isAbsolute, relative, resolve, sep } from "node:path";
import type ts from "@typescript/typescript6";
import type { LoadedProject } from "./config.js";
import { diagnosticAt, sortDiagnostics } from "./diagnostics.js";
import {
  auditPackageImport,
  type PackageImportResolution,
} from "./provenance.js";
import { toPosix } from "./artifacts.js";
import type { DiagnosticRecord, ModuleRoute } from "./types.js";

interface ImportEdge {
  readonly node: ts.Node;
  readonly specifier: string;
  readonly typeOnly: boolean;
}

export interface ImportGraphAnalysis {
  readonly diagnostics: readonly DiagnosticRecord[];
  readonly adjacency: ReadonlyMap<string, readonly string[]>;
}

function importEdges(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
): ImportEdge[] {
  const output: ImportEdge[] = [];
  function visit(node: ts.Node): void {
    if (
      compiler.isImportDeclaration(node) &&
      compiler.isStringLiteral(node.moduleSpecifier)
    ) {
      const clause = node.importClause;
      const namedTypeOnly =
        clause?.namedBindings && compiler.isNamedImports(clause.namedBindings)
          ? clause.namedBindings.elements.every((element) => element.isTypeOnly)
          : false;
      output.push({
        node,
        specifier: node.moduleSpecifier.text,
        typeOnly: clause?.isTypeOnly ?? namedTypeOnly,
      });
    } else if (
      compiler.isExportDeclaration(node) &&
      node.moduleSpecifier &&
      compiler.isStringLiteral(node.moduleSpecifier)
    ) {
      output.push({
        node,
        specifier: node.moduleSpecifier.text,
        typeOnly: node.isTypeOnly,
      });
    } else if (
      compiler.isImportEqualsDeclaration(node) &&
      compiler.isExternalModuleReference(node.moduleReference) &&
      node.moduleReference.expression &&
      compiler.isStringLiteral(node.moduleReference.expression)
    ) {
      output.push({
        node,
        specifier: node.moduleReference.expression.text,
        typeOnly: node.isTypeOnly,
      });
    } else if (
      compiler.isCallExpression(node) &&
      (node.expression.kind === compiler.SyntaxKind.ImportKeyword ||
        (compiler.isIdentifier(node.expression) &&
          node.expression.text === "require")) &&
      node.arguments.length === 1 &&
      compiler.isStringLiteral(node.arguments[0]!)
    ) {
      output.push({ node, specifier: node.arguments[0].text, typeOnly: false });
    } else if (
      compiler.isImportTypeNode(node) &&
      compiler.isLiteralTypeNode(node.argument) &&
      compiler.isStringLiteral(node.argument.literal)
    ) {
      output.push({
        node,
        specifier: node.argument.literal.text,
        typeOnly: true,
      });
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return output;
}

function resolveRelative(
  containingFile: string,
  specifier: string,
  compiler?: typeof import("@typescript/typescript6"),
  virtualPaths: ReadonlySet<string> = new Set(),
): string | undefined {
  if (!specifier.startsWith(".")) return undefined;
  const base = resolve(dirname(containingFile), specifier);
  const candidates = /\.jsx?$/.test(base)
    ? [base.replace(/\.js$/, ".ts"), base.replace(/\.jsx$/, ".tsx")]
    : /\.[A-Za-z0-9]+$/.test(base)
      ? [base]
      : [
          base,
          `${base}.ts`,
          `${base}.tsx`,
          resolve(base, "index.ts"),
          resolve(base, "index.tsx"),
        ];
  return (
    candidates.find(
      (path) =>
        virtualPaths.has(resolve(path)) || compiler?.sys.fileExists(path),
    ) ?? candidates[0]
  );
}

export function resolveWorkspaceModuleSpecifier(
  compiler: typeof import("@typescript/typescript6"),
  containingFile: string,
  specifier: string,
  compilerOptions: ts.CompilerOptions,
  virtualPaths: ReadonlySet<string> = new Set(),
): string | undefined {
  const canonicalVirtual = new Set(
    [...virtualPaths].map((path) => resolve(path)),
  );
  const host: ts.ModuleResolutionHost = {
    ...compiler.sys,
    fileExists: (path) =>
      canonicalVirtual.has(resolve(path)) || compiler.sys.fileExists(path),
  };
  const resolved = compiler.resolveModuleName(
    specifier,
    containingFile,
    compilerOptions,
    host,
  ).resolvedModule?.resolvedFileName;
  if (resolved) return resolve(resolved);
  return resolveRelative(containingFile, specifier, compiler, canonicalVirtual);
}

function projectForFile(
  root: string,
  file: string,
  routes: readonly ModuleRoute[],
  projects: readonly LoadedProject[],
): LoadedProject | undefined {
  const absolute = resolve(file);
  const route = routes.find((item) =>
    [
      item.specPath,
      item.facadePath,
      item.apiMirrorPath,
      item.implementationPath,
      item.contextPath,
    ]
      .filter((path): path is string => path !== undefined)
      .map((path) => resolve(root, path))
      .includes(absolute),
  );
  if (route) {
    return projects.find((project) => project.id === route.project);
  }
  const exact = projects
    .filter((project) =>
      project.parsed.fileNames.some((path) => resolve(path) === absolute),
    )
    .sort(
      (left, right) =>
        dirname(right.configPath).length - dirname(left.configPath).length,
    )[0];
  if (exact) return exact;
  return projects
    .filter((project) => {
      const containment = relative(dirname(project.configPath), absolute);
      return (
        containment !== ".." &&
        !containment.startsWith(`..${sep}`) &&
        !isAbsolute(containment)
      );
    })
    .sort(
      (left, right) =>
        dirname(right.configPath).length - dirname(left.configPath).length,
    )[0];
}

function isWorkspaceSource(root: string, path: string): boolean {
  const containment = relative(resolve(root), resolve(path));
  return (
    containment !== ".." &&
    !containment.startsWith(`..${sep}`) &&
    !isAbsolute(containment) &&
    !toPosix(containment).split("/").includes("node_modules")
  );
}

/**
 * Resolve only the physical fact that is safe to use for package ownership.
 * External npm and pnpm store locations are intentionally omitted: dependency
 * authorization follows the logical specifier/package-import target instead.
 */
export function resolvePackageImportResolution(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  containingFile: string,
  specifier: string,
  compilerOptions: ts.CompilerOptions,
  virtualPaths: ReadonlySet<string> = new Set(),
): PackageImportResolution | undefined {
  const resolved = resolveWorkspaceModuleSpecifier(
    compiler,
    containingFile,
    specifier,
    compilerOptions,
    virtualPaths,
  );
  return resolved && isWorkspaceSource(root, resolved)
    ? { resolvedWorkspaceFile: resolved }
    : undefined;
}

function belongsToConfiguredRoot(
  root: string,
  file: string,
  configuredRoots: readonly string[],
): boolean {
  const relativePath = toPosix(relative(root, file));
  return configuredRoots.some((configured) => {
    const normalized = configured.replaceAll("\\", "/").replace(/^\.\//, "");
    let expression = "";
    for (let index = 0; index < normalized.length; index += 1) {
      const character = normalized[index]!;
      if (character === "*" && normalized[index + 1] === "*") {
        expression += ".*";
        index += 1;
      } else if (character === "*") {
        expression += "[^/]*";
      } else if (character === "?") {
        expression += "[^/]";
      } else {
        expression += /[.+^${}()|[\]\\]/.test(character)
          ? `\\${character}`
          : character;
      }
    }
    return new RegExp(`^${expression}(?:/|$)`).test(relativePath);
  });
}

export function analyzeImportGraph(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  files: readonly string[],
  routes: readonly ModuleRoute[],
  projects: readonly LoadedProject[] = [],
  testRoots: readonly string[] = [],
): ImportGraphAnalysis {
  const diagnostics: DiagnosticRecord[] = [];
  const routeByContext = new Map(
    routes.flatMap((route) =>
      route.contextPath
        ? [[resolve(root, route.contextPath), route] as const]
        : [],
    ),
  );
  const routeByFacade = new Map(
    routes.map((route) => [resolve(root, route.facadePath), route]),
  );
  const generatedPaths = new Set(
    routes.flatMap((route) =>
      [route.apiMirrorPath, route.implementationPath].map((path) =>
        resolve(root, path),
      ),
    ),
  );
  const virtualPaths = new Set(
    routes.flatMap((route) =>
      [
        route.specPath,
        route.facadePath,
        route.apiMirrorPath,
        route.implementationPath,
        route.contextPath,
      ]
        .filter((path): path is string => path !== undefined)
        .map((path) => resolve(root, path)),
    ),
  );
  const adjacency = new Map<string, Set<string>>();
  const scopeAdjacency = new Map<string, Set<string>>();
  for (const file of files) {
    const source = readFileSync(file, "utf8");
    const sourceFile = compiler.createSourceFile(
      file,
      source,
      compiler.ScriptTarget.Latest,
      true,
      file.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
    );
    const isSpec = /\.jaunt(?:-test)?\.(?:ts|tsx)$/.test(file);
    const ownerProject = projectForFile(root, file, routes, projects);
    const isTest =
      (isSpec && file.includes(".jaunt-test.")) ||
      /\.(?:test|spec)\.(?:ts|tsx)$/.test(file) ||
      ownerProject?.role === "test" ||
      belongsToConfiguredRoot(root, file, testRoots);
    const contextRoute = routeByContext.get(resolve(file));
    const facadeRoute = routeByFacade.get(resolve(file));
    const compilerOptions = ownerProject?.parsed.options ?? {};
    for (const edge of importEdges(compiler, sourceFile)) {
      const resolved = resolveWorkspaceModuleSpecifier(
        compiler,
        file,
        edge.specifier,
        compilerOptions,
        virtualPaths,
      );
      if (resolved) {
        const values = scopeAdjacency.get(resolve(file)) ?? new Set<string>();
        values.add(resolved);
        scopeAdjacency.set(resolve(file), values);
      }
      if (!edge.typeOnly && resolved) {
        const values = adjacency.get(resolve(file)) ?? new Set<string>();
        values.add(resolved);
        adjacency.set(resolve(file), values);
      }
      if (
        !edge.typeOnly &&
        (/\.jaunt(?:-test)?\.(?:js|ts|tsx)$/.test(edge.specifier) ||
          (resolved !== undefined &&
            /\.jaunt(?:-test)?\.(?:ts|tsx)$/.test(resolved))) &&
        !isSpec
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            edge.node,
            "JAUNT_TS_RUNTIME_SPEC_IMPORT",
            "Runtime imports of private Jaunt spec inputs are forbidden",
          ),
        );
      }
      // Private spec and test-spec inputs are never emitted or executed. Their
      // value imports are static identifier bindings used by deps=/targets=;
      // discovery resolves and validates those edges against the governed graph.
      if (contextRoute && !edge.typeOnly && resolved) {
        const forbidden = new Set([
          resolve(root, contextRoute.specPath),
          resolve(root, contextRoute.facadePath),
          resolve(root, contextRoute.implementationPath),
        ]);
        if (forbidden.has(resolved)) {
          diagnostics.push(
            diagnosticAt(
              root,
              sourceFile,
              edge.node,
              "JAUNT_TS_CONTEXT_CYCLE",
              "A context module may not value-import its own spec, facade, or implementation",
            ),
          );
        }
      }
      const ownFacadeArtifact =
        resolved !== undefined &&
        facadeRoute !== undefined &&
        [facadeRoute.apiMirrorPath, facadeRoute.implementationPath]
          .map((path) => resolve(root, path))
          .includes(resolved);
      const ownContextTypeMirror =
        resolved !== undefined &&
        edge.typeOnly &&
        contextRoute !== undefined &&
        resolve(root, contextRoute.apiMirrorPath) === resolved;
      if (
        resolved &&
        generatedPaths.has(resolved) &&
        !ownFacadeArtifact &&
        !ownContextTypeMirror
      ) {
        diagnostics.push(
          diagnosticAt(
            root,
            sourceFile,
            edge.node,
            "JAUNT_TS_GENERATED_PRIVATE_IMPORT",
            "Generated-private modules may be imported only by their own public facade",
          ),
        );
      }
      const packageResolution = resolvePackageImportResolution(
        compiler,
        root,
        file,
        edge.specifier,
        compilerOptions,
        virtualPaths,
      );
      const packageDiagnostic = auditPackageImport(
        root,
        file,
        edge.specifier,
        isTest,
        packageResolution,
      );
      if (packageDiagnostic) {
        diagnostics.push({
          ...packageDiagnostic,
          path: toPosix(relative(root, file)),
        });
      }
    }
  }
  for (const [contextPath, route] of routeByContext) {
    const forbidden = new Set([
      resolve(root, route.specPath),
      resolve(root, route.facadePath),
      resolve(root, route.implementationPath),
    ]);
    const seen = new Set<string>();
    const pending = [...(adjacency.get(contextPath) ?? [])];
    let cycle = false;
    while (pending.length > 0 && !cycle) {
      const path = pending.pop();
      if (!path || seen.has(path)) continue;
      seen.add(path);
      if (forbidden.has(path)) {
        cycle = true;
        break;
      }
      pending.push(...(adjacency.get(path) ?? []));
    }
    if (
      cycle &&
      !diagnostics.some(
        (item) =>
          item.code === "JAUNT_TS_CONTEXT_CYCLE" &&
          item.path === route.contextPath,
      )
    ) {
      diagnostics.push({
        code: "JAUNT_TS_CONTEXT_CYCLE",
        severity: "error",
        message:
          "Context runtime imports transitively reach its own spec/facade/generated layer",
        path: route.contextPath!,
      });
    }
  }
  return {
    diagnostics: sortDiagnostics(diagnostics),
    adjacency: new Map(
      [...scopeAdjacency.entries()].map(([path, dependencies]) => [
        resolve(path),
        [...dependencies].map((dependency) => resolve(dependency)).sort(),
      ]),
    ),
  };
}
