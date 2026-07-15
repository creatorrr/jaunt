import { existsSync, readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import type ts from "@typescript/typescript6";
import {
  auditBuiltImplementationPolicy,
  composeCandidate,
} from "./composition.js";
import {
  renderConformanceSource,
  renderFacadeConformanceSource,
  renderMirrorConformanceSource,
} from "./conformance.js";
import {
  diagnosticAt,
  fromTypeScriptDiagnostic,
  sortDiagnostics,
} from "./diagnostics.js";
import { projectReferencesProject, type LoadedProject } from "./config.js";
import type { ContractModuleIR } from "./ir.js";
import {
  canonicalFacadeSource,
  relativeModuleSpecifier,
  renderApiMirror,
} from "./mirror.js";
import { renderSidecar, serializeSignature } from "./ir.js";
import { canonicalJson, sha256Bytes } from "./canonical.js";
import { renderPlaceholder } from "./placeholders.js";
import type { ArtifactRecord, DiagnosticRecord } from "./types.js";
import { resolvePackageImportResolution } from "./dependencies.js";
import type { PackageImportResolution } from "./provenance.js";
import { reusableSourceFile } from "./source_file_reuse.js";

export interface OverlayValidation {
  readonly valid: boolean;
  readonly artifacts: readonly ArtifactRecord[];
  readonly diagnostics: readonly DiagnosticRecord[];
}

export interface OverlayProgramState {
  readonly key: string;
  readonly generation: number;
  readonly reusedSourceFiles: number;
}

interface OverlayProgramEntry {
  readonly program: ts.Program;
  readonly sources: ReadonlyMap<string, string>;
  readonly generation: number;
  readonly reusedSourceFiles: number;
}

/** Reuses compiler structure between fresh, immutable candidate overlays. */
export class OverlayProgramCache {
  #entries = new Map<string, OverlayProgramEntry>();
  #nextGeneration = 1;

  clear(): void {
    this.#entries.clear();
  }

  create(
    key: string,
    compiler: typeof import("@typescript/typescript6"),
    roots: readonly string[],
    options: ts.CompilerOptions,
    sources: ReadonlyMap<string, string>,
    projectReferences?: readonly ts.ProjectReference[],
  ): ts.Program {
    const previous = this.#entries.get(key);
    const program = compiler.createProgram({
      rootNames: [...new Set(roots)],
      options,
      ...(projectReferences ? { projectReferences } : {}),
      ...(previous ? { oldProgram: previous.program } : {}),
      host: overlayHost(
        compiler,
        options,
        sources,
        previous?.program,
        previous?.sources,
      ),
    });
    const oldSourceFiles = new Set(previous?.program.getSourceFiles() ?? []);
    this.#entries.set(key, {
      program,
      sources: new Map(
        [...sources].map(([path, source]) => [resolve(path), source]),
      ),
      generation: this.#nextGeneration++,
      reusedSourceFiles: program
        .getSourceFiles()
        .filter((sourceFile) => oldSourceFiles.has(sourceFile)).length,
    });
    return program;
  }

  state(): readonly OverlayProgramState[] {
    return [...this.#entries.entries()]
      .map(([key, entry]) => ({
        key,
        generation: entry.generation,
        reusedSourceFiles: entry.reusedSourceFiles,
      }))
      .sort((left, right) => left.key.localeCompare(right.key));
  }
}

function facadeDiagnostics(
  ir: ContractModuleIR,
  source: string,
): DiagnosticRecord[] {
  const diagnostics: DiagnosticRecord[] = [];
  if (
    /from\s+["'][^"']*\.jaunt(?:-test)?(?:\.(?:js|ts|tsx))?["']/.test(source)
  ) {
    diagnostics.push({
      code: "JAUNT_TS_FACADE_SPEC_LEAK",
      severity: "error",
      message:
        "Public facades may not import or re-export private spec modules",
      path: ir.facadePath,
    });
  }
  const allowedGenerated = new Set([
    relativeModuleSpecifier(ir.facadePath, ir.apiMirrorPath),
    relativeModuleSpecifier(ir.facadePath, ir.implementationPath),
  ]);
  for (const match of source.matchAll(
    /from\s+["']([^"']*__generated__[^"']*)["']/g,
  )) {
    const specifier = match[1];
    if (specifier && !allowedGenerated.has(specifier)) {
      diagnostics.push({
        code: "JAUNT_TS_FACADE_GENERATED_LEAK",
        severity: "error",
        message: `Facade imports unrelated generated-private module ${JSON.stringify(specifier)}`,
        path: ir.facadePath,
      });
    }
  }
  return diagnostics;
}

function globalDeclarationRoots(
  compiler: typeof import("@typescript/typescript6"),
  project: LoadedProject,
): readonly string[] {
  return project.parsed.fileNames.filter((path) => {
    if (/\.d\.[cm]?ts$/.test(path)) return true;
    const source = compiler.sys.readFile(path);
    if (source === undefined) return false;
    const moduleDetectionCompiler = compiler as typeof compiler & {
      getSetExternalModuleIndicator(
        options: ts.CompilerOptions,
      ): (file: ts.SourceFile) => void;
    };
    const sourceFile = compiler.createSourceFile(
      path,
      source,
      {
        languageVersion: compiler.ScriptTarget.Latest,
        impliedNodeFormat: compiler.getImpliedNodeFormatForFile(
          path,
          undefined,
          compiler.sys,
          project.parsed.options,
        ),
        setExternalModuleIndicator:
          moduleDetectionCompiler.getSetExternalModuleIndicator(
            project.parsed.options,
          ),
      },
      true,
    );
    const externalModule = (
      sourceFile as ts.SourceFile & { externalModuleIndicator?: ts.Node }
    ).externalModuleIndicator;
    if (externalModule === undefined) {
      return sourceFile.statements.some((statement) => {
        if (
          compiler.isClassDeclaration(statement) ||
          compiler.isFunctionDeclaration(statement) ||
          compiler.isInterfaceDeclaration(statement) ||
          compiler.isVariableStatement(statement) ||
          compiler.isTypeAliasDeclaration(statement) ||
          compiler.isModuleDeclaration(statement) ||
          compiler.isEnumDeclaration(statement)
        ) {
          return true;
        }
        return (
          compiler.canHaveModifiers(statement) &&
          (compiler
            .getModifiers(statement)
            ?.some(
              (modifier) =>
                modifier.kind === compiler.SyntaxKind.DeclareKeyword,
            ) ??
            false)
        );
      });
    }
    let declaresGlobal = false;
    const visit = (node: ts.Node): void => {
      if (
        compiler.isModuleDeclaration(node) &&
        (node.flags & compiler.NodeFlags.GlobalAugmentation) !== 0
      ) {
        declaresGlobal = true;
        return;
      }
      if (!declaresGlobal) compiler.forEachChild(node, visit);
    };
    visit(sourceFile);
    return declaresGlobal;
  });
}

function validateSources(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  sources: ReadonlyMap<string, string>,
  extraRoots: readonly string[] = [],
  overlayRoots: readonly string[] = [...sources.keys()],
  referencedOverlayPaths: ReadonlySet<string> = new Set(),
  programCache?: OverlayProgramCache,
  scopedRoots = false,
): DiagnosticRecord[] {
  function programFor(
    profile: "native" | "strict",
    options: ts.CompilerOptions,
    roots: readonly string[],
  ): ts.Program {
    if (programCache) {
      return programCache.create(
        `${project.id}:${profile}:${scopedRoots ? "scoped" : "full"}`,
        compiler,
        roots,
        options,
        sources,
        project.parsed.projectReferences,
      );
    }
    return compiler.createProgram({
      rootNames: [...new Set(roots)],
      options,
      ...(project.parsed.projectReferences
        ? { projectReferences: project.parsed.projectReferences }
        : {}),
      host: overlayHost(compiler, options, sources),
    });
  }
  const nativeOptions: ts.CompilerOptions = {
    ...project.parsed.options,
    noEmit: true,
    composite: false,
    // `oldProgram` structural reuse does not require incremental emit or a
    // .tsbuildinfo file; candidate SourceFiles are still recreated on changes.
    incremental: false,
  };
  const ambientRoots = globalDeclarationRoots(compiler, project);
  const nativeProgram = programFor("native", nativeOptions, [
    ...(scopedRoots ? ambientRoots : project.parsed.fileNames),
    ...overlayRoots,
    ...extraRoots,
  ]);
  const specPath = resolve(root, ir.specPath);
  const native = compiler
    .getPreEmitDiagnostics(nativeProgram)
    .filter((diagnostic) => {
      const message = compiler.flattenDiagnosticMessageText(
        diagnostic.messageText,
        "\n",
      );
      return (
        !(
          diagnostic.code === 2377 &&
          diagnostic.file &&
          resolve(diagnostic.file.fileName) === specPath
        ) &&
        !(
          diagnostic.code === 6133 &&
          diagnostic.file &&
          resolve(diagnostic.file.fileName) === specPath
        ) &&
        !(
          (diagnostic.code === 6059 ||
            diagnostic.code === 6305 ||
            diagnostic.code === 6307) &&
          ([...referencedOverlayPaths].some((path) =>
            diagnosticMentionsPath(compiler, message, path),
          ) ||
            (diagnostic.file &&
              [...referencedOverlayPaths].some((path) =>
                sameDiagnosticPath(compiler, diagnostic.file!.fileName, path),
              )))
        )
      );
    });
  const strictOptions: ts.CompilerOptions = {
    ...project.parsed.options,
    noEmit: true,
    composite: false,
    incremental: false,
    strict: true,
    strictFunctionTypes: true,
    noImplicitAny: true,
    exactOptionalPropertyTypes: true,
    skipLibCheck: false,
  };
  const protectedPaths = new Set(
    [...sources.keys(), ...extraRoots]
      .filter(
        (path) =>
          path === resolve(root, ir.apiMirrorPath) ||
          path === resolve(root, ir.implementationPath) ||
          path.includes(".jaunt-"),
      )
      .map((path) => resolve(path)),
  );
  const strictProgram = programFor("strict", strictOptions, [
    ...ambientRoots,
    ...overlayRoots,
    ...extraRoots,
  ]);
  const strict = compiler
    .getPreEmitDiagnostics(strictProgram)
    .filter((diagnostic) => {
      const message = compiler.flattenDiagnosticMessageText(
        diagnostic.messageText,
        "\n",
      );
      if (
        (diagnostic.code === 6059 ||
          diagnostic.code === 6305 ||
          diagnostic.code === 6307) &&
        [...referencedOverlayPaths].some((path) =>
          diagnosticMentionsPath(compiler, message, path),
        )
      ) {
        return false;
      }
      return (
        !diagnostic.file ||
        protectedPaths.has(resolve(diagnostic.file.fileName))
      );
    });
  const records = [...native, ...strict].map((diagnostic) =>
    fromTypeScriptDiagnostic(compiler, root, diagnostic),
  );
  records.push(...boundaryAnyDiagnostics(compiler, root, strictProgram, ir));
  return [
    ...new Map(
      records.map((diagnostic) => [JSON.stringify(diagnostic), diagnostic]),
    ).values(),
  ];
}

function referencedModulePaths(
  root: string,
  project: LoadedProject,
  modules: readonly ContractModuleIR[],
): ReadonlySet<string> {
  return new Set(
    modules
      .filter((module) => module.project !== project.id)
      .flatMap((module) =>
        [
          module.specPath,
          module.facadePath,
          module.apiMirrorPath,
          module.implementationPath,
          module.contextPath,
        ]
          .filter((path): path is string => path !== undefined)
          .map((path) => absolute(root, path)),
      ),
  );
}

function moduleValidationRoots(
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  sources: ReadonlyMap<string, string>,
  preflightModules: readonly ContractModuleIR[],
): readonly string[] {
  const roots = new Set<string>([
    absolute(root, ir.apiMirrorPath),
    absolute(root, ir.implementationPath),
    absolute(root, ir.facadePath),
  ]);
  for (const module of preflightModules) {
    if (module.project !== project.id) continue;
    roots.add(absolute(root, module.apiMirrorPath));
    roots.add(absolute(root, module.implementationPath));
    roots.add(absolute(root, module.facadePath));
  }
  const virtualDirectory = dirname(absolute(root, ir.implementationPath));
  for (const path of sources.keys()) {
    if (
      dirname(path) === virtualDirectory &&
      /[/\\]\.jaunt-[^/\\]+\.tsx?$/.test(path)
    ) {
      roots.add(path);
    }
  }
  return [...roots].filter((path) => sources.has(resolve(path))).sort();
}

function boundaryAnyDiagnostics(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  program: ts.Program,
  ir: ContractModuleIR,
): DiagnosticRecord[] {
  const sourceFile = program.getSourceFile(
    resolve(root, ir.implementationPath),
  );
  if (!sourceFile) return [];
  const candidateFile = sourceFile;
  const checker = program.getTypeChecker();
  const targetNames = new Set(
    ir.symbols.map((symbol) => `__jaunt_impl_${symbol.name}`),
  );
  const identifiers: ts.Identifier[] = [];
  function find(node: ts.Node): void {
    if (
      (compiler.isVariableDeclaration(node) ||
        compiler.isFunctionDeclaration(node) ||
        compiler.isClassDeclaration(node)) &&
      node.name &&
      compiler.isIdentifier(node.name) &&
      targetNames.has(node.name.text)
    ) {
      identifiers.push(node.name);
    }
    compiler.forEachChild(node, find);
  }
  find(sourceFile);
  function containsAny(
    type: ts.Type,
    seen: Set<ts.Type>,
    depth: number,
  ): boolean {
    if ((type.flags & compiler.TypeFlags.Any) !== 0) return true;
    const terminalFlags =
      compiler.TypeFlags.StringLike |
      compiler.TypeFlags.NumberLike |
      compiler.TypeFlags.BooleanLike |
      compiler.TypeFlags.BigIntLike |
      compiler.TypeFlags.ESSymbolLike |
      compiler.TypeFlags.Void |
      compiler.TypeFlags.Undefined |
      compiler.TypeFlags.Null |
      compiler.TypeFlags.Never |
      compiler.TypeFlags.Unknown;
    if ((type.flags & terminalFlags) !== 0) return false;
    if (depth > 12 || seen.has(type)) return false;
    seen.add(type);
    if (type.isUnionOrIntersection()) {
      return type.types.some((member) => containsAny(member, seen, depth + 1));
    }
    const aliasArguments = type.aliasTypeArguments ?? [];
    if (
      aliasArguments.some((argument) => containsAny(argument, seen, depth + 1))
    ) {
      return true;
    }
    if (
      (type.flags & compiler.TypeFlags.Object) !== 0 &&
      ((type as ts.ObjectType).objectFlags & compiler.ObjectFlags.Reference) !==
        0
    ) {
      const arguments_ = checker.getTypeArguments(type as ts.TypeReference);
      // Instantiated library containers such as Promise<string> are opaque
      // beyond their authored type arguments. Walking Promise.then and friends
      // would mistake deliberate `any` inside lib.d.ts internals for a candidate
      // boundary escape.
      return arguments_.some((argument) =>
        containsAny(argument, seen, depth + 1),
      );
    }
    const signatures = [
      ...checker.getSignaturesOfType(type, compiler.SignatureKind.Call),
      ...checker.getSignaturesOfType(type, compiler.SignatureKind.Construct),
    ];
    for (const signature of signatures) {
      if (
        containsAny(
          checker.getReturnTypeOfSignature(signature),
          seen,
          depth + 1,
        )
      )
        return true;
      for (const parameter of signature.parameters) {
        const location =
          parameter.valueDeclaration ??
          parameter.declarations?.[0] ??
          candidateFile;
        if (
          containsAny(
            checker.getTypeOfSymbolAtLocation(parameter, location),
            seen,
            depth + 1,
          )
        )
          return true;
      }
    }
    if (signatures.length > 0) return false;
    for (const property of checker.getPropertiesOfType(type)) {
      const location =
        property.valueDeclaration ??
        property.declarations?.[0] ??
        candidateFile;
      if (location.getSourceFile() !== candidateFile) continue;
      if (
        containsAny(
          checker.getTypeOfSymbolAtLocation(property, location),
          seen,
          depth + 1,
        )
      )
        return true;
    }
    return false;
  }
  const diagnostics: DiagnosticRecord[] = [];
  const checked = new Set<string>();
  for (const identifier of identifiers) {
    if (checked.has(identifier.text)) continue;
    checked.add(identifier.text);
    const type = checker.getTypeAtLocation(identifier);
    const hasAny = containsAny(type, new Set(), 0);
    if (hasAny) {
      diagnostics.push(
        diagnosticAt(
          root,
          candidateFile,
          identifier,
          "JAUNT_TS_BOUNDARY_ANY",
          `Reserved implementation boundary ${identifier.text} resolves to any`,
        ),
      );
    }
  }
  return diagnostics;
}

function absolute(root: string, path: string): string {
  return resolve(root, path);
}

function diagnosticPathValue(
  compiler: typeof import("@typescript/typescript6"),
  value: string,
): string {
  const normalized = value.replaceAll("\\", "/");
  return compiler.sys.useCaseSensitiveFileNames
    ? normalized
    : normalized.toLowerCase();
}

function diagnosticMentionsPath(
  compiler: typeof import("@typescript/typescript6"),
  message: string,
  path: string,
): boolean {
  return diagnosticPathValue(compiler, message).includes(
    diagnosticPathValue(compiler, path),
  );
}

function sameDiagnosticPath(
  compiler: typeof import("@typescript/typescript6"),
  left: string,
  right: string,
): boolean {
  return (
    diagnosticPathValue(compiler, resolve(left)) ===
    diagnosticPathValue(compiler, resolve(right))
  );
}

function candidatePackageImportResolver(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  modules: readonly ContractModuleIR[],
): (specifier: string) => PackageImportResolution | undefined {
  const path = absolute(root, ir.implementationPath);
  const virtualPaths = new Set(
    [ir, ...modules].flatMap((module) =>
      [
        module.specPath,
        module.facadePath,
        module.apiMirrorPath,
        module.implementationPath,
        module.contextPath,
      ]
        .filter((value): value is string => value !== undefined)
        .map((value) => absolute(root, value)),
    ),
  );
  return (specifier: string): PackageImportResolution | undefined =>
    resolvePackageImportResolution(
      compiler,
      root,
      path,
      specifier,
      project.parsed.options,
      virtualPaths,
    );
}

function restampBuiltImplementation(
  ir: ContractModuleIR,
  source: string,
): {
  readonly source: string;
  readonly diagnostics: readonly DiagnosticRecord[];
} {
  if (
    !source.includes("// ⛓️ jaunt:generated") ||
    !source.includes("// jaunt:state=built")
  ) {
    return {
      source,
      diagnostics: [
        {
          code: "JAUNT_TS_IMPLEMENTATION_PROVENANCE",
          severity: "error",
          message:
            "An existing implementation must have Jaunt built provenance before sync can restamp it",
          path: ir.implementationPath,
        },
      ],
    };
  }
  const values = {
    module: ir.moduleId,
    structural: ir.structuralDigest,
    prose: ir.proseDigest,
    api: ir.apiDigest,
  } as const;
  let restamped = source;
  const diagnostics: DiagnosticRecord[] = [];
  for (const [key, value] of Object.entries(values)) {
    const pattern = new RegExp(`^// jaunt:${key}=[^\\r\\n]*(\\r?)$`, "gm");
    const matches = [...restamped.matchAll(pattern)];
    if (matches.length !== 1) {
      diagnostics.push({
        code: "JAUNT_TS_IMPLEMENTATION_PROVENANCE",
        severity: "error",
        message: `Expected exactly one jaunt:${key} header in the existing implementation`,
        path: ir.implementationPath,
      });
      continue;
    }
    restamped = restamped.replace(
      pattern,
      (_line, carriageReturn: string) =>
        `// jaunt:${key}=${value}${carriageReturn}`,
    );
  }
  return { source: restamped, diagnostics };
}

function preserveBuiltSidecar(
  root: string,
  ir: ContractModuleIR,
  implementationContent: string,
  apiSource: string,
  facadeContent: string,
  facadeExisted: boolean,
): {
  readonly source?: string;
  readonly diagnostics: readonly DiagnosticRecord[];
} {
  const sidecarPath = absolute(
    root,
    ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
  );
  let value: Record<string, unknown>;
  try {
    const parsed = JSON.parse(readFileSync(sidecarPath, "utf8")) as unknown;
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed))
      throw new Error();
    value = parsed as Record<string, unknown>;
  } catch {
    return {
      diagnostics: [
        {
          code: "JAUNT_TS_SIDECAR_INVALID",
          severity: "error",
          message:
            "Plain sync cannot advance a built implementation without its valid existing sidecar",
          path: ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
        },
      ],
    };
  }
  const rawHashes = value.artifactHashes;
  if (
    value.state !== "built" ||
    !rawHashes ||
    typeof rawHashes !== "object" ||
    Array.isArray(rawHashes)
  ) {
    return {
      diagnostics: [
        {
          code: "JAUNT_TS_SIDECAR_INVALID",
          severity: "error",
          message: "Plain sync requires a built sidecar with artifact hashes",
          path: ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
        },
      ],
    };
  }
  const hashes = rawHashes as Record<string, unknown>;
  const protectedArtifacts = [
    [ir.implementationPath, implementationContent],
    ...(facadeExisted ? [[ir.facadePath, facadeContent]] : []),
  ] as const;
  const drifted = protectedArtifacts
    .filter(([path, content]) => hashes[path] !== sha256Bytes(content))
    .map(([path]) => path);
  if (drifted.length > 0) {
    return {
      diagnostics: [
        {
          code: "JAUNT_TS_ARTIFACT_DRIFT",
          severity: "error",
          message: `Plain sync will not bless edited built artifacts: ${drifted.join(", ")}`,
          path: ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
        },
      ],
    };
  }
  const next = {
    ...value,
    artifactHashes: {
      ...hashes,
      [ir.apiMirrorPath]: sha256Bytes(apiSource),
      [ir.facadePath]: sha256Bytes(facadeContent),
    },
  };
  return {
    source: `${JSON.stringify(JSON.parse(canonicalJson(next)), null, 2)}\n`,
    diagnostics: [],
  };
}

function overlayHost(
  compiler: typeof import("@typescript/typescript6"),
  options: ts.CompilerOptions,
  overlay: ReadonlyMap<string, string>,
  oldProgram?: ts.Program,
  previousOverlay: ReadonlyMap<string, string> = new Map(),
): ts.CompilerHost {
  const base = compiler.createCompilerHost(options, true);
  const canonical = (path: string) => resolve(path);
  const host: ts.CompilerHost = {
    ...base,
    fileExists: (path) => overlay.has(canonical(path)) || base.fileExists(path),
    readFile: (path) => overlay.get(canonical(path)) ?? base.readFile(path),
    getSourceFile: (
      path,
      languageVersion,
      onError,
      shouldCreateNewSourceFile,
    ) => {
      const absolute = canonical(path);
      const content = overlay.get(absolute);
      if (content !== undefined) {
        if (
          !shouldCreateNewSourceFile &&
          previousOverlay.get(absolute) === content
        ) {
          const previous = reusableSourceFile(
            oldProgram?.getSourceFile(path) ??
              oldProgram?.getSourceFile(absolute),
          );
          if (previous) return previous;
        }
        return compiler.createSourceFile(
          path,
          content,
          languageVersion,
          true,
          path.endsWith(".tsx")
            ? compiler.ScriptKind.TSX
            : compiler.ScriptKind.TS,
        );
      }
      if (!shouldCreateNewSourceFile && !previousOverlay.has(absolute)) {
        const previous = reusableSourceFile(
          oldProgram?.getSourceFile(path) ??
            oldProgram?.getSourceFile(absolute),
        );
        if (previous) return previous;
      }
      return base.getSourceFile(
        path,
        languageVersion,
        onError,
        shouldCreateNewSourceFile,
      );
    },
    realpath: (path) =>
      overlay.has(canonical(path))
        ? canonical(path)
        : (base.realpath?.(path) ?? path),
    writeFile: () => undefined,
  };
  host.resolveModuleNames = (moduleNames, containingFile) =>
    moduleNames.map((moduleName) => {
      if (moduleName.startsWith(".")) {
        const requested = resolve(dirname(containingFile), moduleName);
        const candidates = moduleName.endsWith(".js")
          ? [requested.slice(0, -3) + ".ts", requested.slice(0, -3) + ".tsx"]
          : [requested, `${requested}.ts`, `${requested}.tsx`];
        const matched = candidates.find((candidate) =>
          overlay.has(canonical(candidate)),
        );
        if (matched) {
          return {
            resolvedFileName: matched,
            extension: matched.endsWith(".tsx")
              ? compiler.Extension.Tsx
              : compiler.Extension.Ts,
            isExternalLibraryImport: false,
          };
        }
      }
      return compiler.resolveModuleName(
        moduleName,
        containingFile,
        options,
        host,
      ).resolvedModule;
    });
  return host;
}

export function validateModuleOverlay(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  candidate: string,
  preflightModules: readonly ContractModuleIR[] = [],
  preflightCandidates: Readonly<Record<string, string>> = {},
  programCache?: OverlayProgramCache,
  scopedRoots = false,
  consumerRoots: readonly string[] = [],
): OverlayValidation {
  const composed = composeCandidate(
    compiler,
    root,
    ir,
    candidate,
    candidatePackageImportResolver(
      compiler,
      root,
      project,
      ir,
      preflightModules,
    ),
  );
  const apiSource = renderApiMirror(compiler, ir);
  const overlay = new Map<string, string>();
  const apiPath = absolute(root, ir.apiMirrorPath);
  const implementationPath = absolute(root, ir.implementationPath);
  const facadePath = absolute(root, ir.facadePath);
  const specPath = absolute(root, ir.specPath);
  const conformancePath = resolve(
    dirname(implementationPath),
    implementationPath.endsWith(".tsx")
      ? ".jaunt-conformance.tsx"
      : ".jaunt-conformance.ts",
  );
  const mirrorCheckPath = resolve(
    dirname(implementationPath),
    ".jaunt-mirror-check.ts",
  );
  const facadeCheckPath = resolve(
    dirname(implementationPath),
    ".jaunt-facade-check.ts",
  );
  overlay.set(apiPath, apiSource);
  overlay.set(implementationPath, composed.source);
  if (!existsSync(facadePath))
    overlay.set(facadePath, canonicalFacadeSource(ir));
  overlay.set(
    conformancePath,
    renderConformanceSource(ir, composed.candidateSource),
  );
  overlay.set(mirrorCheckPath, renderMirrorConformanceSource(ir));
  overlay.set(facadeCheckPath, renderFacadeConformanceSource(ir));
  addPreflightModules(
    compiler,
    root,
    overlay,
    preflightModules,
    preflightCandidates,
  );
  if (!overlay.has(specPath) && existsSync(specPath))
    overlay.set(specPath, readFileSync(specPath, "utf8"));
  const effectiveFacade =
    overlay.get(facadePath) ?? readFileSync(facadePath, "utf8");

  const diagnostics = [
    ...composed.diagnostics,
    ...facadeDiagnostics(ir, effectiveFacade),
    ...mirrorShapeDiagnostics(compiler, ir, apiSource),
    ...validateSources(
      compiler,
      root,
      project,
      ir,
      overlay,
      consumerRoots,
      moduleValidationRoots(root, project, ir, overlay, preflightModules),
      referencedModulePaths(root, project, preflightModules),
      programCache,
      scopedRoots,
    ),
  ];
  const sorted = sortDiagnostics(diagnostics);
  if (sorted.some((diagnostic) => diagnostic.severity === "error")) {
    return { valid: false, artifacts: [], diagnostics: sorted };
  }
  const facadeContent =
    overlay.get(facadePath) ?? readFileSync(facadePath, "utf8");
  const artifactHashes = {
    [ir.apiMirrorPath]: sha256Bytes(apiSource),
    [ir.implementationPath]: sha256Bytes(composed.source),
    [ir.facadePath]: sha256Bytes(facadeContent),
  };
  const sidecar = renderSidecar(ir, { state: "built", artifactHashes });
  const artifacts: ArtifactRecord[] = [
    {
      path: ir.apiMirrorPath,
      content: apiSource,
      sha256: sha256Bytes(apiSource),
      kind: "api-mirror",
      moduleId: ir.moduleId,
    },
    {
      path: ir.implementationPath,
      content: composed.source,
      sha256: sha256Bytes(composed.source),
      kind: "implementation",
      moduleId: ir.moduleId,
    },
    {
      path: ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
      content: sidecar,
      sha256: sha256Bytes(sidecar),
      kind: "sidecar",
      moduleId: ir.moduleId,
    },
  ];
  if (!existsSync(facadePath)) {
    const facade = canonicalFacadeSource(ir);
    artifacts.push({
      path: ir.facadePath,
      content: facade,
      sha256: sha256Bytes(facade),
      kind: "facade",
      moduleId: ir.moduleId,
    });
  }
  return { valid: true, artifacts, diagnostics: [] };
}

export function validateSyncOverlay(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  preflightModules: readonly ContractModuleIR[] = [],
  restampBuilt = false,
  preflightCandidates: Readonly<Record<string, string>> = {},
  programCache?: OverlayProgramCache,
  scopedRoots = false,
  consumerRoots: readonly string[] = [],
): OverlayValidation {
  const apiSource = renderApiMirror(compiler, ir);
  const apiPath = absolute(root, ir.apiMirrorPath);
  const implementationPath = absolute(root, ir.implementationPath);
  const facadePath = absolute(root, ir.facadePath);
  const specPath = absolute(root, ir.specPath);
  const mirrorCheckPath = resolve(
    dirname(implementationPath),
    ".jaunt-mirror-check.ts",
  );
  const facadeCheckPath = resolve(
    dirname(implementationPath),
    ".jaunt-facade-check.ts",
  );
  const facadeExisted = existsSync(facadePath);
  const facadeContent = facadeExisted
    ? readFileSync(facadePath, "utf8")
    : canonicalFacadeSource(ir);
  const implementationExists = existsSync(implementationPath);
  const existingImplementation = implementationExists
    ? readFileSync(implementationPath, "utf8")
    : undefined;
  const implementationBuilt =
    existingImplementation?.includes("// jaunt:state=built") ?? false;
  const restampPolicyDiagnostics =
    implementationBuilt && restampBuilt
      ? auditBuiltImplementationPolicy(
          compiler,
          root,
          ir,
          existingImplementation!,
          candidatePackageImportResolver(
            compiler,
            root,
            project,
            ir,
            preflightModules,
          ),
        )
      : [];
  const preparedImplementation =
    implementationBuilt && restampBuilt
      ? restampBuiltImplementation(ir, existingImplementation!)
      : {
          source: existingImplementation ?? renderPlaceholder(ir),
          diagnostics: [],
        };
  const implementationContent = preparedImplementation.source;
  const preservedSidecar =
    implementationBuilt && !restampBuilt
      ? preserveBuiltSidecar(
          root,
          ir,
          implementationContent,
          apiSource,
          facadeContent,
          facadeExisted,
        )
      : undefined;
  const overlay = new Map<string, string>([
    [apiPath, apiSource],
    [implementationPath, implementationContent],
    [facadePath, facadeContent],
    [mirrorCheckPath, renderMirrorConformanceSource(ir)],
    [facadeCheckPath, renderFacadeConformanceSource(ir)],
  ]);
  addPreflightModules(
    compiler,
    root,
    overlay,
    preflightModules,
    preflightCandidates,
  );
  if (existsSync(specPath))
    overlay.set(specPath, readFileSync(specPath, "utf8"));
  const diagnostics = sortDiagnostics([
    ...(implementationExists &&
    !implementationBuilt &&
    !implementationContent.includes("// jaunt:state=unbuilt")
      ? [
          {
            code: "JAUNT_TS_IMPLEMENTATION_PROVENANCE",
            severity: "error" as const,
            message:
              "An existing implementation has neither built nor unbuilt Jaunt provenance; sync will not overwrite it",
            path: ir.implementationPath,
          },
        ]
      : []),
    ...preparedImplementation.diagnostics,
    ...restampPolicyDiagnostics,
    ...(preservedSidecar?.diagnostics ?? []),
    ...validateSources(
      compiler,
      root,
      project,
      ir,
      overlay,
      consumerRoots,
      moduleValidationRoots(root, project, ir, overlay, preflightModules),
      referencedModulePaths(root, project, preflightModules),
      programCache,
      scopedRoots,
    ),
    ...facadeDiagnostics(ir, facadeContent),
    ...mirrorShapeDiagnostics(compiler, ir, apiSource),
  ]);
  if (diagnostics.some((diagnostic) => diagnostic.severity === "error")) {
    return { valid: false, artifacts: [], diagnostics };
  }
  const artifactHashes = {
    [ir.apiMirrorPath]: sha256Bytes(apiSource),
    [ir.implementationPath]: sha256Bytes(implementationContent),
    [ir.facadePath]: sha256Bytes(facadeContent),
  };
  const sidecar =
    preservedSidecar?.source ??
    renderSidecar(ir, {
      state: implementationBuilt ? "built" : "unbuilt",
      artifactHashes,
    });
  const artifacts: ArtifactRecord[] = [
    {
      path: ir.apiMirrorPath,
      content: apiSource,
      sha256: artifactHashes[ir.apiMirrorPath]!,
      kind: "api-mirror",
      moduleId: ir.moduleId,
    },
    {
      path: ir.apiMirrorPath.replace(/\.api\.ts$/, ".jaunt.json"),
      content: sidecar,
      sha256: sha256Bytes(sidecar),
      kind: "sidecar",
      moduleId: ir.moduleId,
    },
  ];
  if (!implementationExists) {
    artifacts.push({
      path: ir.implementationPath,
      content: implementationContent,
      sha256: artifactHashes[ir.implementationPath]!,
      kind: "placeholder",
      moduleId: ir.moduleId,
    });
  } else if (
    implementationBuilt &&
    restampBuilt &&
    implementationContent !== existingImplementation
  ) {
    artifacts.push({
      path: ir.implementationPath,
      content: implementationContent,
      sha256: artifactHashes[ir.implementationPath]!,
      kind: "implementation",
      moduleId: ir.moduleId,
    });
  }
  if (!facadeExisted) {
    artifacts.push({
      path: ir.facadePath,
      content: facadeContent,
      sha256: artifactHashes[ir.facadePath]!,
      kind: "facade",
      moduleId: ir.moduleId,
    });
  }
  return { valid: true, artifacts, diagnostics: [] };
}

function addPreflightModules(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  overlay: Map<string, string>,
  modules: readonly ContractModuleIR[],
  candidates: Readonly<Record<string, string>>,
): void {
  for (const module of modules) {
    const apiPath = absolute(root, module.apiMirrorPath);
    const implementationPath = absolute(root, module.implementationPath);
    const facadePath = absolute(root, module.facadePath);
    overlay.set(apiPath, renderApiMirror(compiler, module));
    const candidate = candidates[module.moduleId];
    if (candidate !== undefined) {
      overlay.set(
        implementationPath,
        composeCandidate(compiler, root, module, candidate).source,
      );
    } else if (!existsSync(implementationPath)) {
      overlay.set(implementationPath, renderPlaceholder(module));
    }
    if (!existsSync(facadePath))
      overlay.set(facadePath, canonicalFacadeSource(module));
  }
}

/**
 * Compile the complete proposed Jaunt overlay through every affected project.
 * Referenced dependencies are visited before their downstream consumers by the
 * caller. The host is entirely in-memory: no declaration, build-info, or source
 * output is written while validating the transaction.
 */
export function validateProjectOverlayClosure(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  projects: readonly LoadedProject[],
  modules: readonly ContractModuleIR[],
  candidates: Readonly<Record<string, string>>,
  proposedArtifacts: readonly ArtifactRecord[],
  affectedIds: readonly string[],
  programCache?: OverlayProgramCache,
  scopedRoots = false,
  consumerRoots: readonly string[] = [],
): readonly DiagnosticRecord[] {
  const overlay = new Map<string, string>();
  for (const module of modules) {
    overlay.set(
      absolute(root, module.apiMirrorPath),
      renderApiMirror(compiler, module),
    );
    const implementationPath = absolute(root, module.implementationPath);
    const candidate = candidates[module.moduleId];
    overlay.set(
      implementationPath,
      candidate !== undefined
        ? composeCandidate(compiler, root, module, candidate).source
        : existsSync(implementationPath)
          ? readFileSync(implementationPath, "utf8")
          : renderPlaceholder(module),
    );
    const facadePath = absolute(root, module.facadePath);
    overlay.set(
      facadePath,
      existsSync(facadePath)
        ? readFileSync(facadePath, "utf8")
        : canonicalFacadeSource(module),
    );
  }
  for (const artifact of proposedArtifacts) {
    if (/\.(?:ts|tsx)$/.test(artifact.path)) {
      overlay.set(absolute(root, artifact.path), artifact.content);
    }
  }

  const byId = new Map(projects.map((project) => [project.id, project]));
  const projectByPath = new Map<string, string>();
  for (const project of projects) {
    for (const path of project.parsed.fileNames) {
      projectByPath.set(resolve(path), project.id);
    }
  }
  for (const module of modules) {
    for (const path of [
      module.specPath,
      module.facadePath,
      module.apiMirrorPath,
      module.implementationPath,
      module.contextPath,
    ]) {
      if (path) projectByPath.set(absolute(root, path), module.project);
    }
  }
  const diagnostics: DiagnosticRecord[] = [];
  for (const id of affectedIds) {
    const project = byId.get(id);
    if (!project || project.role === "solution") continue;
    const options: ts.CompilerOptions = {
      ...project.parsed.options,
      noEmit: true,
      disableSourceOfProjectReferenceRedirect: false,
    };
    const ownedOverlayRoots = modules
      .filter((module) => module.project === project.id)
      .flatMap((module) => [
        absolute(root, module.apiMirrorPath),
        absolute(root, module.implementationPath),
        absolute(root, module.facadePath),
      ]);
    const ambientRoots = globalDeclarationRoots(compiler, project);
    const projectFiles = new Set(
      project.parsed.fileNames.map((path) => resolve(path)),
    );
    const ownedConsumerRoots = consumerRoots.filter((path) =>
      projectFiles.has(resolve(path)),
    );
    const roots = [
      ...new Set(
        scopedRoots
          ? [...ambientRoots, ...ownedOverlayRoots, ...ownedConsumerRoots]
          : [...project.parsed.fileNames, ...ownedOverlayRoots],
      ),
    ];
    const program = programCache
      ? programCache.create(
          `${project.id}:closure:${scopedRoots ? "scoped" : "full"}`,
          compiler,
          roots,
          options,
          overlay,
          project.parsed.projectReferences,
        )
      : compiler.createProgram({
          rootNames: roots,
          options,
          ...(project.parsed.projectReferences
            ? { projectReferences: project.parsed.projectReferences }
            : {}),
          host: overlayHost(compiler, options, overlay),
        });
    diagnostics.push(
      ...compiler
        .getPreEmitDiagnostics(program)
        .filter((diagnostic) => {
          if (
            (diagnostic.code !== 6059 &&
              diagnostic.code !== 6305 &&
              diagnostic.code !== 6307) ||
            !diagnostic.file
          ) {
            return true;
          }
          const message = compiler.flattenDiagnosticMessageText(
            diagnostic.messageText,
            "\n",
          );
          for (const [path, owner] of projectByPath) {
            if (
              owner !== project.id &&
              projectReferencesProject(projects, project.id, owner) &&
              (diagnosticMentionsPath(compiler, message, path) ||
                sameDiagnosticPath(compiler, diagnostic.file.fileName, path))
            ) {
              return false;
            }
          }
          return true;
        })
        .map((diagnostic) =>
          fromTypeScriptDiagnostic(compiler, root, diagnostic),
        ),
    );
  }
  return sortDiagnostics([
    ...new Map(
      diagnostics.map((diagnostic) => [JSON.stringify(diagnostic), diagnostic]),
    ).values(),
  ]);
}

export function validateApiMirrorEquivalence(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  project: LoadedProject,
  ir: ContractModuleIR,
  apiSource: string,
  programCache?: OverlayProgramCache,
): readonly DiagnosticRecord[] {
  const apiPath = absolute(root, ir.apiMirrorPath);
  const specPath = absolute(root, ir.specPath);
  const checkPath = resolve(dirname(apiPath), ".jaunt-mirror-check.ts");
  const overlay = new Map<string, string>([
    [apiPath, apiSource],
    [specPath, readFileSync(specPath, "utf8")],
    [checkPath, renderMirrorConformanceSource(ir)],
  ]);
  const options: ts.CompilerOptions = {
    ...project.parsed.options,
    noEmit: true,
    strict: true,
    strictFunctionTypes: true,
    noImplicitAny: true,
    exactOptionalPropertyTypes: true,
  };
  const program = programCache
    ? programCache.create(
        `${project.id}:mirror`,
        compiler,
        [...overlay.keys()],
        options,
        overlay,
      )
    : compiler.createProgram({
        rootNames: [...overlay.keys()],
        options,
        host: overlayHost(compiler, options, overlay),
      });
  return sortDiagnostics([
    ...mirrorShapeDiagnostics(compiler, ir, apiSource),
    ...compiler
      .getPreEmitDiagnostics(program)
      .filter(
        (diagnostic) =>
          !(
            diagnostic.code === 6133 &&
            diagnostic.file &&
            resolve(diagnostic.file.fileName) === specPath
          ),
      )
      .map((diagnostic) =>
        fromTypeScriptDiagnostic(compiler, root, diagnostic),
      ),
  ]);
}

function mirrorShapeDiagnostics(
  compiler: typeof import("@typescript/typescript6"),
  ir: ContractModuleIR,
  source: string,
): DiagnosticRecord[] {
  const file = compiler.createSourceFile(
    ir.apiMirrorPath,
    source,
    compiler.ScriptTarget.Latest,
    true,
    compiler.ScriptKind.TS,
  );
  const diagnostics: DiagnosticRecord[] = [];
  const declarations = new Map<string, ts.FunctionDeclaration[]>();
  for (const statement of file.statements) {
    if (compiler.isFunctionDeclaration(statement) && statement.name) {
      const values = declarations.get(statement.name.text) ?? [];
      values.push(statement);
      declarations.set(statement.name.text, values);
    }
  }
  for (const symbol of ir.symbols.filter((item) => item.kind === "function")) {
    const actual = (declarations.get(symbol.name) ?? []).map((declaration) =>
      serializeSignature(compiler, declaration),
    );
    if (JSON.stringify(actual) !== JSON.stringify(symbol.signatures)) {
      diagnostics.push({
        code: "JAUNT_TS_API_MIRROR_MISMATCH",
        severity: "error",
        message: `API mirror declaration for ${symbol.name} does not match canonical contract IR`,
        path: ir.apiMirrorPath,
      });
    }
  }
  return diagnostics;
}
