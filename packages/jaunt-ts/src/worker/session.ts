import { existsSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";
import { isAbsolute, relative, resolve } from "node:path";
import type ts from "@typescript/typescript6";
import { WorkerError } from "../protocol/errors.js";
import {
  PROTOCOL_VERSION,
  type AnalyzeContractsParams,
  type AnalyzeContractsResult,
  type AnalyzeWorkspaceResult,
  type AnalyzeWorkspaceParams,
  type CancelParams,
  type ContractAnalysisRecord,
  type FindOrphansParams,
  type FindOrphansResult,
  type InitializeParams,
  type InitializeResult,
  type InvalidateParams,
  type InvalidateResult,
  type ProjectContractParams,
  type ProjectContractResult,
  type ValidateOverlayParams,
  type ValidateOverlayResult,
} from "../protocol/messages.js";
import {
  assertLexicallyWithinRoot,
  assertWithinRoot,
  readOptional,
  toPosix,
} from "../analyzer/artifacts.js";
import { digestCanonical, sha256Bytes } from "../analyzer/canonical.js";
import {
  affectedProjectIds,
  loadProjectGraph,
  type ProjectGraph,
} from "../analyzer/config.js";
import {
  collapseProvenanceDiagnostics,
  discoverWorkspace,
  type DiscoveryResult,
} from "../analyzer/discovery.js";
import { buildContractIR, renderSidecar } from "../analyzer/ir.js";
import { decomposeGeneratedImplementation } from "../analyzer/composition.js";
import { projectContractDeclaration } from "../analyzer/contract_projection.js";
import { renderApiMirror } from "../analyzer/mirror.js";
import {
  OverlayProgramCache,
  type OverlayProgramState,
  validateModuleOverlay,
  validateProjectOverlayClosure,
  validateSyncOverlay,
} from "../analyzer/overlay.js";
import { resolveWorkspaceModuleSpecifier } from "../analyzer/dependencies.js";
import { renderPlaceholder } from "../analyzer/placeholders.js";
import {
  collectTypeEnvironment,
  type TypeEnvironmentSnapshot,
} from "../analyzer/type_environment.js";
import {
  AnalysisProgramCache,
  type AnalysisProgramState,
} from "../analyzer/program_cache.js";
import type {
  ArtifactRecord,
  DiagnosticRecord,
  OrphanRecord,
  SessionMetadata,
} from "../analyzer/types.js";

function packageVersion(): string {
  const metadata = JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ) as Record<string, unknown>;
  if (typeof metadata.version !== "string" || metadata.version.trim() === "") {
    throw new Error("@usejaunt/ts package.json has no version");
  }
  return metadata.version;
}

export const WORKER_VERSION = packageVersion();

export interface WorkerPhaseTiming {
  readonly phase: string;
  readonly state: "start" | "finish";
  readonly elapsedMs: number;
}

export type WorkerPhaseReporter = (timing: WorkerPhaseTiming) => void;

function packageManagerIdentity(root: string, toolOwner: string): string {
  const owners = [...new Set([resolve(root, toolOwner), root])];
  for (const owner of owners) {
    try {
      const manifest = JSON.parse(
        readFileSync(resolve(owner, "package.json"), "utf8"),
      ) as Record<string, unknown>;
      if (
        typeof manifest.packageManager === "string" &&
        manifest.packageManager.trim() !== ""
      ) {
        return manifest.packageManager;
      }
    } catch {
      // Lockfile detection below remains deterministic for missing/invalid metadata.
    }
    for (const [lockfile, name] of [
      ["pnpm-lock.yaml", "pnpm"],
      ["yarn.lock", "yarn"],
      ["bun.lock", "bun"],
      ["bun.lockb", "bun"],
      ["package-lock.json", "npm"],
      ["npm-shrinkwrap.json", "npm"],
    ] as const) {
      if (existsSync(resolve(owner, lockfile))) return name;
    }
  }
  return "unknown";
}

function candidateModuleSpecifiers(
  compiler: typeof import("@typescript/typescript6"),
  path: string,
  source: string,
): readonly string[] {
  const sourceFile = compiler.createSourceFile(
    path,
    source,
    compiler.ScriptTarget.Latest,
    true,
    path.endsWith(".tsx") ? compiler.ScriptKind.TSX : compiler.ScriptKind.TS,
  );
  const output = new Set<string>();
  function visit(node: ts.Node): void {
    if (
      (compiler.isImportDeclaration(node) ||
        compiler.isExportDeclaration(node)) &&
      node.moduleSpecifier &&
      compiler.isStringLiteral(node.moduleSpecifier)
    ) {
      output.add(node.moduleSpecifier.text);
    } else if (
      compiler.isImportEqualsDeclaration(node) &&
      compiler.isExternalModuleReference(node.moduleReference) &&
      node.moduleReference.expression &&
      compiler.isStringLiteral(node.moduleReference.expression)
    ) {
      output.add(node.moduleReference.expression.text);
    } else if (
      compiler.isCallExpression(node) &&
      (node.expression.kind === compiler.SyntaxKind.ImportKeyword ||
        (compiler.isIdentifier(node.expression) &&
          node.expression.text === "require")) &&
      node.arguments.length === 1 &&
      compiler.isStringLiteral(node.arguments[0]!)
    ) {
      output.add(node.arguments[0].text);
    }
    compiler.forEachChild(node, visit);
  }
  visit(sourceFile);
  return [...output].sort();
}

function moduleSelection(
  ids: readonly string[] | undefined,
): Set<string> | undefined {
  return ids ? new Set(ids.map((id) => id.split("#", 1)[0]!)) : undefined;
}

function moduleClosure(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  modules: DiscoveryResult["modules"],
  selected: ReadonlySet<string> | undefined,
): ReadonlySet<DiscoveryResult["modules"][number]> | undefined {
  if (!selected) return undefined;
  const moduleByPath = new Map<string, DiscoveryResult["modules"][number]>();
  for (const module of modules) {
    for (const path of [
      module.route.specPath,
      module.route.facadePath,
      module.route.apiMirrorPath,
      module.route.implementationPath,
    ]) {
      moduleByPath.set(resolve(root, path), module);
    }
  }
  const virtualPaths = new Set(moduleByPath.keys());
  const closure = new Set<DiscoveryResult["modules"][number]>();
  const pending = modules.filter((module) =>
    selected.has(module.route.moduleId),
  );
  while (pending.length > 0) {
    const module = pending.pop()!;
    if (closure.has(module)) continue;
    closure.add(module);
    pending.push(...module.dependencyModules);
    for (const specifier of candidateModuleSpecifiers(
      compiler,
      module.sourceFile.fileName,
      module.source,
    )) {
      const resolved = resolveWorkspaceModuleSpecifier(
        compiler,
        module.sourceFile.fileName,
        specifier,
        module.compilerOptions,
        virtualPaths,
      );
      const imported = resolved
        ? moduleByPath.get(resolve(resolved))
        : undefined;
      if (imported && imported !== module) pending.push(imported);
    }
  }
  return closure;
}

function record(value: unknown, label: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new WorkerError("INVALID_REQUEST", `${label} must be an object`);
  }
  return value as Record<string, unknown>;
}

function assertOnlyParamKeys(
  value: Record<string, unknown>,
  allowed: ReadonlySet<string>,
  label: string,
): void {
  const extras = Object.keys(value).filter((key) => !allowed.has(key));
  if (extras.length > 0) {
    throw new WorkerError(
      "INVALID_REQUEST",
      `${label} params contain unknown field(s): ${extras.sort().join(", ")}`,
    );
  }
}

function stringField(value: Record<string, unknown>, key: string): string {
  if (typeof value[key] !== "string" || value[key] === "") {
    throw new WorkerError(
      "INVALID_REQUEST",
      `${key} must be a non-empty string`,
    );
  }
  return value[key] as string;
}

function stringArray(value: Record<string, unknown>, key: string): string[] {
  const item = value[key];
  if (!Array.isArray(item) || item.some((entry) => typeof entry !== "string")) {
    throw new WorkerError(
      "INVALID_REQUEST",
      `${key} must be an array of strings`,
    );
  }
  return item as string[];
}

function workspacePath(
  value: string,
  key: string,
  allowCurrent = true,
): string {
  const parts = value.split("/");
  if (
    value.includes("\\") ||
    isAbsolute(value) ||
    parts.includes("..") ||
    value === "" ||
    (!allowCurrent && value === ".")
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      `${key} must be a safe root-relative POSIX path`,
    );
  }
  return value;
}

export function parseInitializeParams(value: unknown): InitializeParams {
  const input = record(value, "initialize params");
  assertOnlyParamKeys(
    input,
    new Set([
      "root",
      "projects",
      "testProjects",
      "sourceRoots",
      "testRoots",
      "generatedDir",
      "toolOwner",
      "compilerModulePath",
      "clientVersion",
      "toolVersion",
      "generationFingerprint",
    ]),
    "initialize",
  );
  const projects = stringArray(input, "projects").map((item, index) =>
    workspacePath(item, `projects[${index}]`),
  );
  const testProjects = stringArray(input, "testProjects").map((item, index) =>
    workspacePath(item, `testProjects[${index}]`),
  );
  const sourceRoots = stringArray(input, "sourceRoots").map((item, index) =>
    workspacePath(item, `sourceRoots[${index}]`),
  );
  const testRoots = stringArray(input, "testRoots").map((item, index) =>
    workspacePath(item, `testRoots[${index}]`),
  );
  return {
    root: stringField(input, "root"),
    projects,
    testProjects,
    sourceRoots,
    testRoots,
    generatedDir: workspacePath(
      stringField(input, "generatedDir"),
      "generatedDir",
      false,
    ),
    toolOwner: workspacePath(stringField(input, "toolOwner"), "toolOwner"),
    compilerModulePath: stringField(input, "compilerModulePath"),
    clientVersion: stringField(input, "clientVersion"),
    toolVersion: stringField(input, "toolVersion"),
    ...(input.generationFingerprint === undefined
      ? {}
      : { generationFingerprint: stringField(input, "generationFingerprint") }),
  };
}

async function loadCompiler(
  path: string,
): Promise<typeof import("@typescript/typescript6")> {
  if (!existsSync(path))
    throw new WorkerError(
      "COMPILER_NOT_FOUND",
      `TypeScript compiler not found: ${path}`,
    );
  try {
    const imported = (await import(pathToFileURL(path).href)) as Record<
      string,
      unknown
    >;
    const candidate = (imported.default ?? imported) as Partial<
      typeof import("@typescript/typescript6")
    >;
    const version =
      typeof candidate.version === "string"
        ? candidate.version
        : typeof imported.version === "string"
          ? imported.version
          : "unknown";
    const [majorText, minorText] = version.split(".");
    const major = Number.parseInt(majorText ?? "0", 10);
    const minor = Number.parseInt(minorText ?? "0", 10);
    if (major >= 7) {
      throw new WorkerError(
        "COMPILER_UNSUPPORTED",
        `TypeScript ${version} does not expose a stable programmatic API. Use a project-local TypeScript >=5.8 <7; TypeScript 7 support will use its stable API when available.`,
      );
    }
    if (major < 5 || (major === 5 && minor < 8)) {
      throw new WorkerError(
        "COMPILER_UNSUPPORTED",
        `TypeScript ${version} is outside the supported >=5.8 <7 range`,
      );
    }
    if (
      !candidate.sys ||
      !candidate.createProgram ||
      !candidate.createSourceFile
    ) {
      throw new WorkerError(
        "COMPILER_UNSUPPORTED",
        `TypeScript ${version} has no compatible compiler API`,
      );
    }
    return candidate as typeof import("@typescript/typescript6");
  } catch (error) {
    if (error instanceof WorkerError) throw error;
    throw new WorkerError(
      "COMPILER_NOT_FOUND",
      `Unable to load TypeScript compiler ${path}: ${error instanceof Error ? error.message : String(error)}`,
    );
  }
}

export class AnalyzerSession {
  readonly compiler: typeof import("@typescript/typescript6");
  readonly params: InitializeParams;
  readonly root: string;
  readonly sessionId = "session-1";
  #epoch = 0;
  #graph: ProjectGraph | undefined;
  #discovery: DiscoveryResult | undefined;
  readonly #analysisPrograms: AnalysisProgramCache;
  readonly #overlayPrograms = new OverlayProgramCache();
  #typeEnvironments = new Map<string, TypeEnvironmentSnapshot>();
  #inputHashes: Record<string, string> = {};
  #snapshot = sha256Bytes("");

  private constructor(
    compiler: typeof import("@typescript/typescript6"),
    params: InitializeParams,
  ) {
    this.compiler = compiler;
    this.params = params;
    this.root = resolve(params.root);
    this.#analysisPrograms = new AnalysisProgramCache(compiler, this.root);
  }

  static async create(params: InitializeParams): Promise<AnalyzerSession> {
    const root = resolve(params.root);
    if (!existsSync(root))
      throw new WorkerError(
        "CONFIG_INVALID",
        `Workspace root does not exist: ${root}`,
      );
    assertWithinRoot(root, resolve(root, params.toolOwner));
    // npm/pnpm may expose the project-local compiler through a node_modules
    // symlink whose physical store is elsewhere. The configured path itself must
    // still be workspace-local; application and artifact paths use the stricter
    // physical containment check.
    const compilerPath = assertLexicallyWithinRoot(
      root,
      resolve(params.compilerModulePath),
    );
    const compiler = await loadCompiler(compilerPath);
    const session = new AnalyzerSession(compiler, { ...params, root });
    session.refresh();
    return session;
  }

  private refresh(invalidatedPaths: readonly string[] = []): void {
    this.#overlayPrograms.clear();
    const previousDiscovery = this.#discovery;
    const previousTypeEnvironments = this.#typeEnvironments;
    this.#graph = loadProjectGraph(
      this.compiler,
      this.root,
      this.params.projects,
      this.params.testProjects,
      {
        compilerModulePath: this.params.compilerModulePath,
        toolOwner: this.params.toolOwner,
      },
    );
    this.#discovery = discoverWorkspace(
      this.compiler,
      this.root,
      this.params.sourceRoots,
      this.params.testRoots,
      this.params.generatedDir,
      this.#graph.projects,
      {
        programCache: this.#analysisPrograms,
        ...(previousDiscovery ? { previous: previousDiscovery } : {}),
        invalidatedPaths,
      },
    );
    this.#typeEnvironments = new Map(
      this.#discovery.modules.map((module) => {
        const project = this.#graph?.projects.find(
          (item) => item.id === module.route.project,
        );
        if (!project) {
          throw new WorkerError(
            "CONFIG_INVALID",
            `Missing owner project ${module.route.project}`,
          );
        }
        const previousModule = previousDiscovery?.modules.find(
          (item) => item.route.moduleId === module.route.moduleId,
        );
        const previousEnvironment = previousTypeEnvironments.get(
          module.route.moduleId,
        );
        return [
          module.route.moduleId,
          previousModule === module && previousEnvironment
            ? previousEnvironment
            : collectTypeEnvironment(
                this.compiler,
                this.root,
                module,
                project.parsed.options,
              ),
        ];
      }),
    );
    this.refreshHashes();
  }

  private refreshHashes(): void {
    const state = this.currentHashState();
    this.#inputHashes = state.inputHashes;
    this.#snapshot = state.snapshot;
  }

  private currentHashState(): {
    inputHashes: Record<string, string>;
    snapshot: string;
  } {
    const paths = new Set<string>();
    for (const project of this.graph.projects) {
      for (const configInput of project.configInputs) paths.add(configInput);
      for (const file of project.parsed.fileNames) paths.add(file);
    }
    for (const module of this.discovery.modules) {
      paths.add(resolve(this.root, module.route.specPath));
      if (module.route.contextPath)
        paths.add(resolve(this.root, module.route.contextPath));
      if (existsSync(resolve(this.root, module.route.facadePath))) {
        paths.add(resolve(this.root, module.route.facadePath));
      }
      for (const path of this.#typeEnvironments.get(module.route.moduleId)
        ?.inputPaths ?? []) {
        // Keep external package-store declarations in the internal snapshot.
        // The final hash pass below separately excludes physically external
        // paths from protocol inputHashes, so Python never treats them as
        // writable commit preconditions.
        paths.add(resolve(path));
      }
    }
    for (const testSpec of this.discovery.testSpecs) {
      paths.add(resolve(this.root, testSpec.path));
    }
    for (const contract of this.discovery.contracts) {
      paths.add(resolve(this.root, contract.path));
    }
    const inputHashes: Record<string, string> = {};
    const snapshotHashes: Record<string, string> = {};
    for (const path of [...paths].sort()) {
      if (!existsSync(path)) continue;
      const key = toPosix(relative(this.root, path));
      const digest = sha256Bytes(readFileSync(path));
      snapshotHashes[key] = digest;
      try {
        assertWithinRoot(this.root, path);
        inputHashes[key] = digest;
      } catch (error) {
        if (
          !(error instanceof WorkerError) ||
          error.payload.code !== "PATH_OUTSIDE_ROOT"
        ) {
          throw error;
        }
        // Package-manager symlinks may place extended configs and declarations
        // outside the physical checkout. Keep them in the worker snapshot without
        // exposing unsafe paths as Python-side commit preconditions.
      }
    }
    return {
      inputHashes,
      snapshot: digestCanonical(snapshotHashes),
    };
  }

  private get graph(): ProjectGraph {
    if (!this.#graph)
      throw new WorkerError(
        "NOT_INITIALIZED",
        "Analyzer session is not initialized",
      );
    return this.#graph;
  }

  private get discovery(): DiscoveryResult {
    if (!this.#discovery)
      throw new WorkerError(
        "NOT_INITIALIZED",
        "Analyzer session is not initialized",
      );
    return this.#discovery;
  }

  metadata(): SessionMetadata {
    return {
      sessionId: this.sessionId,
      epoch: this.#epoch,
      snapshot: this.#snapshot,
      inputHashes: { ...this.#inputHashes },
    };
  }

  /** Internal observability for deterministic cache-regression tests. */
  analysisProgramState(): readonly AnalysisProgramState[] {
    return this.#analysisPrograms.state();
  }

  /** Internal observability for deterministic cache-regression tests. */
  overlayProgramState(): readonly OverlayProgramState[] {
    return this.#overlayPrograms.state();
  }

  initializeResult(): InitializeResult {
    return {
      ...this.metadata(),
      workerVersion: WORKER_VERSION,
      protocol: PROTOCOL_VERSION,
      typescriptVersion: this.compiler.version,
      packageManager: packageManagerIdentity(this.root, this.params.toolOwner),
      capabilities: [
        "analyze",
        "overlay",
        "sync",
        "orphans",
        "invalidate",
        "contract-projection",
        "scoped-diagnostics",
        "scoped-analysis",
        "scoped-validation",
        "recompose",
        "baseline-unselected",
      ],
    };
  }

  analyzeWorkspace(
    params: AnalyzeWorkspaceParams = {},
  ): AnalyzeWorkspaceResult {
    const selected = moduleSelection(params.moduleIds);
    const selectedModules = moduleClosure(
      this.compiler,
      this.root,
      this.discovery.modules,
      selected,
    );
    let discoveryDiagnostics = this.discovery.diagnostics;
    if (selected && selectedModules) {
      const relevantPaths = new Set<string>();
      for (const module of selectedModules) {
        for (const path of [
          module.route.specPath,
          module.route.contextPath,
          module.route.facadePath,
          module.route.apiMirrorPath,
          module.route.implementationPath,
          module.route.sidecarPath,
        ]) {
          if (path) relevantPaths.add(resolve(this.root, path));
        }
      }
      for (const testSpec of this.discovery.testSpecs) {
        if (
          testSpec.targets.some((target) =>
            [...selectedModules].some((module) =>
              target.startsWith(`${module.route.moduleId}#`),
            ),
          )
        ) {
          relevantPaths.add(resolve(this.root, testSpec.path));
        }
      }
      const importPending = [...relevantPaths];
      while (importPending.length > 0) {
        const path = importPending.pop()!;
        for (const dependency of this.discovery.importAdjacency.get(path) ??
          []) {
          if (relevantPaths.has(dependency)) continue;
          relevantPaths.add(dependency);
          importPending.push(dependency);
        }
      }
      discoveryDiagnostics = collapseProvenanceDiagnostics(
        this.discovery.scopedDiagnostics.filter(
          (diagnostic) =>
            !diagnostic.path ||
            relevantPaths.has(resolve(this.root, diagnostic.path)),
        ),
      );
    }
    const moduleIds = new Set(
      [...(selectedModules ?? this.discovery.modules)].map(
        (module) => module.route.moduleId,
      ),
    );
    return {
      ...this.metadata(),
      projects: this.graph.records,
      routes: this.discovery.routes.filter((route) =>
        moduleIds.has(route.moduleId),
      ),
      specs: this.discovery.specs.filter((spec) =>
        moduleIds.has(spec.moduleId),
      ),
      testSpecs: selected
        ? this.discovery.testSpecs.filter((testSpec) =>
            testSpec.targets.some((target) =>
              moduleIds.has(target.split("#", 1)[0]!),
            ),
          )
        : this.discovery.testSpecs,
      contracts: selected
        ? this.discovery.contracts.filter((contract) =>
            selected.has(`ts:${contract.path.replace(/\.(?:ts|tsx)$/u, "")}`),
          )
        : this.discovery.contracts,
      diagnostics: [...this.graph.diagnostics, ...discoveryDiagnostics],
    };
  }

  analyzeContracts(
    params: AnalyzeContractsParams = {},
  ): AnalyzeContractsResult {
    const selected = moduleSelection(params.moduleIds);
    const selectedModules = moduleClosure(
      this.compiler,
      this.root,
      this.discovery.modules,
      selected,
    );
    const modules: ContractAnalysisRecord[] = this.discovery.modules
      .filter((module) => !selectedModules || selectedModules.has(module))
      .map((module) => {
        const ir = this.contractIr(module);
        const contextSource = ir.contextPath
          ? readOptional(resolve(this.root, ir.contextPath))
          : undefined;
        return {
          ...ir,
          routes: module.route,
          apiSource: renderApiMirror(ir),
          placeholderSource: renderPlaceholder(ir),
          sidecar: renderSidecar(ir),
          specSource: module.source,
          ...(contextSource === undefined ? {} : { contextSource }),
        };
      });
    return { ...this.metadata(), modules };
  }

  projectContract(params: ProjectContractParams): ProjectContractResult {
    return projectContractDeclaration(
      this.compiler,
      params.source,
      params.symbol,
      params.fileName,
    );
  }

  validateOverlay(
    params: ValidateOverlayParams,
    reportPhase?: WorkerPhaseReporter,
  ): ValidateOverlayResult {
    const startedAt = performance.now();
    const timed = <T>(phase: string, operation: () => T): T => {
      reportPhase?.({
        phase,
        state: "start",
        elapsedMs: performance.now() - startedAt,
      });
      try {
        return operation();
      } finally {
        reportPhase?.({
          phase,
          state: "finish",
          elapsedMs: performance.now() - startedAt,
        });
      }
    };
    const currentSnapshot = timed(
      "snapshot",
      () => this.currentHashState().snapshot,
    );
    if (
      params.sessionId !== this.sessionId ||
      params.expectedEpoch !== this.#epoch ||
      params.expectedSnapshot !== this.#snapshot ||
      currentSnapshot !== this.#snapshot
    ) {
      throw new WorkerError(
        "STALE_SESSION",
        "Workspace changed after analysis; analyze again before validation",
      );
    }
    const selected = moduleSelection(params.moduleIds);
    if (params.scopeToModuleIds && !selected) {
      throw new WorkerError(
        "INVALID_REQUEST",
        "scopeToModuleIds requires a non-empty moduleIds selection",
      );
    }
    if (params.baselineUnselected && !selected) {
      throw new WorkerError(
        "INVALID_REQUEST",
        "baselineUnselected requires a non-empty moduleIds selection",
      );
    }
    const syncModules =
      moduleSelection(params.syncModuleIds) ?? new Set<string>();
    const restampModules =
      moduleSelection(params.restampModuleIds) ?? new Set<string>();
    const recomposeModules =
      moduleSelection(params.recomposeModuleIds) ?? new Set<string>();
    const modes = [
      new Set(Object.keys(params.candidates)),
      syncModules,
      restampModules,
      recomposeModules,
    ];
    const conflicting = [...new Set(modes.flatMap((mode) => [...mode]))].filter(
      (moduleId) => modes.filter((mode) => mode.has(moduleId)).length > 1,
    );
    if (conflicting.length > 0) {
      throw new WorkerError(
        "INVALID_REQUEST",
        `Modules cannot request more than one overlay mode: ${conflicting.sort().join(", ")}`,
      );
    }
    const requestedModules = new Set([
      ...Object.keys(params.candidates),
      ...syncModules,
      ...restampModules,
      ...recomposeModules,
    ]);
    const knownModules = new Set(
      this.discovery.modules.map((module) => module.route.moduleId),
    );
    const unknownModules = [...requestedModules]
      .filter((moduleId) => !knownModules.has(moduleId))
      .sort();
    if (unknownModules.length > 0) {
      throw new WorkerError(
        "INVALID_REQUEST",
        `Overlay names unknown TypeScript modules: ${unknownModules.join(", ")}`,
      );
    }
    if (
      selected &&
      [...requestedModules].some((moduleId) => !selected.has(moduleId))
    ) {
      throw new WorkerError(
        "INVALID_REQUEST",
        "Every candidate, sync, or restamp module must also appear in moduleIds",
      );
    }
    if (
      this.graph.diagnostics.some(
        (diagnostic) => diagnostic.severity === "error",
      )
    ) {
      throw new WorkerError(
        "VALIDATION_FAILED",
        "Workspace project graph has blocking diagnostics",
        {
          diagnostics: this.graph.diagnostics,
        },
      );
    }
    const artifacts: ArtifactRecord[] = [];
    const diagnostics: DiagnosticRecord[] = [];
    const changedProjects = new Set<string>();
    const validationModules = params.scopeToModuleIds
      ? [
          ...(moduleClosure(
            this.compiler,
            this.root,
            this.discovery.modules,
            selected,
          ) ?? []),
        ]
      : this.discovery.modules;
    const allIrs = timed("contract-ir", () =>
      validationModules.map((module) => this.contractIr(module)),
    );
    const irByModule = new Map(allIrs.map((ir) => [ir.moduleId, ir] as const));
    const candidates: Record<string, string> = { ...params.candidates };
    const failedRecompositions = new Set<string>();
    for (const moduleId of recomposeModules) {
      const ir = irByModule.get(moduleId);
      if (!ir) continue;
      const existing = readOptional(resolve(this.root, ir.implementationPath));
      if (existing === undefined) {
        diagnostics.push({
          code: "JAUNT_TS_RECOMPOSE_MISSING",
          severity: "error",
          message: "Cannot recompose a missing generated implementation",
          path: ir.implementationPath,
        });
        failedRecompositions.add(moduleId);
        continue;
      }
      const decomposed = decomposeGeneratedImplementation(
        this.compiler,
        ir,
        existing,
      );
      diagnostics.push(...decomposed.diagnostics);
      if (decomposed.candidateSource === undefined) {
        failedRecompositions.add(moduleId);
        continue;
      }
      candidates[moduleId] = decomposed.candidateSource;
    }
    timed("module-overlays", () => {
      for (const module of validationModules) {
        if (selected && !selected.has(module.route.moduleId)) continue;
        if (failedRecompositions.has(module.route.moduleId)) continue;
        const candidate = candidates[module.route.moduleId];
        if (
          candidate === undefined &&
          !syncModules.has(module.route.moduleId) &&
          !restampModules.has(module.route.moduleId)
        )
          continue;
        const ir = irByModule.get(module.route.moduleId)!;
        const project = this.graph.projects.find(
          (item) => item.id === ir.project,
        );
        if (!project)
          throw new WorkerError(
            "CONFIG_INVALID",
            `Missing owner project ${ir.project}`,
          );
        if (candidate !== undefined) {
          const specifiers = candidateModuleSpecifiers(
            this.compiler,
            resolve(this.root, ir.implementationPath),
            candidate,
          );
          const virtualPaths = new Set(
            this.discovery.routes.flatMap((route) =>
              [
                route.specPath,
                route.facadePath,
                route.apiMirrorPath,
                route.implementationPath,
                route.contextPath,
              ]
                .filter((path): path is string => path !== undefined)
                .map((path) => resolve(this.root, path)),
            ),
          );
          const imported = specifiers.flatMap((specifier) => {
            const resolved = resolveWorkspaceModuleSpecifier(
              this.compiler,
              resolve(this.root, ir.implementationPath),
              specifier,
              project.parsed.options,
              virtualPaths,
            );
            return resolved === undefined ? [] : [resolved];
          });
          const privateSpecs = this.discovery.routes.filter((route) =>
            imported.includes(resolve(this.root, route.specPath)),
          );
          if (privateSpecs.length > 0) {
            diagnostics.push({
              code: "JAUNT_TS_SPEC_IMPORT",
              severity: "error",
              message: `Candidate imports private Jaunt spec module(s): ${privateSpecs
                .map((route) => route.moduleId)
                .sort()
                .join(", ")}`,
              path: ir.implementationPath,
            });
            continue;
          }
          const generatedPrivate = this.discovery.routes.filter((route) =>
            [route.apiMirrorPath, route.implementationPath]
              .map((path) => resolve(this.root, path))
              .some((path) => imported.includes(path)),
          );
          if (generatedPrivate.length > 0) {
            diagnostics.push({
              code: "JAUNT_TS_GENERATED_PRIVATE_IMPORT",
              severity: "error",
              message: `Candidate imports generated-private Jaunt artifact(s): ${generatedPrivate
                .map((route) => route.moduleId)
                .sort()
                .join(", ")}`,
              path: ir.implementationPath,
            });
            continue;
          }
          if (
            imported.includes(
              resolve(
                this.root,
                this.discovery.routes.find(
                  (route) => route.moduleId === ir.moduleId,
                )?.facadePath ?? ir.facadePath,
              ),
            )
          ) {
            diagnostics.push({
              code: "JAUNT_TS_CANDIDATE_SELF_IMPORT",
              severity: "error",
              message: `Candidate may not import its own public facade ${ir.moduleId}`,
              path: ir.implementationPath,
            });
            continue;
          }
          const foreignImports = this.discovery.routes.filter(
            (route) =>
              route.moduleId !== ir.moduleId &&
              imported.includes(resolve(this.root, route.facadePath)),
          );
          const declaredModules = new Set(
            ir.dependencies.map((dependency) => dependency.split("#", 1)[0]),
          );
          const undeclared = foreignImports.filter(
            (route) => !declaredModules.has(route.moduleId),
          );
          if (undeclared.length > 0) {
            diagnostics.push({
              code: "JAUNT_TS_UNDECLARED_DEPENDENCY_IMPORT",
              severity: "error",
              message: `Candidate imports undeclared Jaunt facade(s): ${undeclared
                .map((route) => route.moduleId)
                .sort()
                .join(", ")}`,
              path: ir.implementationPath,
            });
            continue;
          }
        }
        const preflightModules = validationModules
          .filter(
            (other) =>
              other.route.moduleId !== ir.moduleId &&
              (!params.baselineUnselected ||
                selected?.has(other.route.moduleId)),
          )
          .map((other) => irByModule.get(other.route.moduleId)!);
        const result =
          candidate === undefined
            ? validateSyncOverlay(
                this.compiler,
                this.root,
                project,
                ir,
                preflightModules,
                restampModules.has(module.route.moduleId),
                candidates,
                this.#overlayPrograms,
                params.scopeToModuleIds ?? false,
              )
            : validateModuleOverlay(
                this.compiler,
                this.root,
                project,
                ir,
                candidate,
                preflightModules,
                candidates,
                this.#overlayPrograms,
                params.scopeToModuleIds ?? false,
              );
        artifacts.push(...result.artifacts);
        diagnostics.push(...result.diagnostics);
        changedProjects.add(ir.project);
      }
    });
    const affectedProjects = affectedProjectIds(
      this.graph.projects,
      changedProjects,
    );
    if (!diagnostics.some((item) => item.severity === "error")) {
      diagnostics.push(
        ...timed("project-closure", () =>
          validateProjectOverlayClosure(
            this.compiler,
            this.root,
            this.graph.projects,
            params.baselineUnselected && selected
              ? allIrs.filter((ir) => selected.has(ir.moduleId))
              : allIrs,
            candidates,
            artifacts,
            affectedProjects,
            this.#overlayPrograms,
            params.scopeToModuleIds ?? false,
          ),
        ),
      );
    }
    const endSnapshot = timed(
      "final-snapshot",
      () => this.currentHashState().snapshot,
    );
    if (endSnapshot !== this.#snapshot) {
      throw new WorkerError(
        "STALE_SESSION",
        "Workspace changed during validation; analyze again before committing artifacts",
      );
    }
    const valid = !diagnostics.some((item) => item.severity === "error");
    return {
      ...this.metadata(),
      valid,
      artifacts: valid ? artifacts : [],
      diagnostics: diagnostics.sort((left, right) =>
        JSON.stringify(left).localeCompare(JSON.stringify(right)),
      ),
      affectedProjects,
    };
  }

  private contractIr(
    module: DiscoveryResult["modules"][number],
  ): ReturnType<typeof buildContractIR> {
    const base = buildContractIR(
      this.compiler,
      module,
      this.#typeEnvironments.get(module.route.moduleId),
    );
    const project = this.graph.records.find((item) => item.id === base.project);
    return {
      ...base,
      fingerprint: {
        toolVersion: this.params.toolVersion,
        workerVersion: WORKER_VERSION,
        typescriptVersion: this.compiler.version,
        compilerOptionsHash: project?.compilerOptionsHash ?? sha256Bytes(""),
        generationFingerprint:
          this.params.generationFingerprint ?? "unspecified",
        protocol: PROTOCOL_VERSION,
        ir: base.schema,
      },
    };
  }

  findOrphans(_params: FindOrphansParams = {}): FindOrphansResult {
    const selected = moduleSelection(_params.moduleIds);
    const expected = new Map<string, string>();
    for (const route of this.discovery.routes) {
      if (selected && !selected.has(route.moduleId)) continue;
      expected.set(route.apiMirrorPath, route.moduleId);
      expected.set(route.implementationPath, route.moduleId);
      expected.set(route.sidecarPath, route.moduleId);
    }
    const generated = this.compiler.sys
      .readDirectory(
        this.root,
        [".ts", ".tsx", ".json"],
        ["**/node_modules/**", "**/dist/**"],
        this.params.sourceRoots.map(
          (sourceRoot) =>
            `${sourceRoot.replace(/\/$/, "")}/**/${this.params.generatedDir}/*`,
        ),
      )
      .map((path) => toPosix(relative(this.root, path)))
      .sort();
    const artifacts: OrphanRecord[] = [];
    for (const path of generated) {
      if (expected.has(path)) continue;
      const content = readOptional(resolve(this.root, path));
      if (!content) continue;
      // Generated Vitest batteries are owned by the Python test/contract
      // lifecycle, which validates their provenance against authored test
      // intent. They may be co-located under a configured source root, but
      // are never implementation artifacts owned by this worker scan.
      if (
        /\.(?:example|derived|contract)\.test\.(?:ts|tsx)$/.test(path) &&
        (content.startsWith(
          "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.\n",
        ) ||
          content.startsWith(
            "// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with `jaunt reconcile`.\n",
          ))
      )
        continue;
      const sidecar = path.endsWith(".jaunt.json");
      if (
        !content.includes("jaunt:") &&
        !(sidecar && content.includes('"schema": "contract-ir/'))
      )
        continue;
      const headerModule = /jaunt:module=([^\r\n]+)/.exec(content)?.[1];
      let sidecarModule: string | undefined;
      if (sidecar) {
        try {
          const value = JSON.parse(content) as { moduleId?: unknown };
          if (typeof value.moduleId === "string")
            sidecarModule = value.moduleId;
        } catch {
          // A malformed provenance sidecar is still an orphan candidate.
        }
      }
      const moduleId = headerModule ?? sidecarModule;
      if (selected && (!moduleId || !selected.has(moduleId))) continue;
      const kind = path.endsWith(".api.ts")
        ? "api-mirror"
        : sidecar
          ? "sidecar"
          : content.includes("jaunt:state=unbuilt")
            ? "placeholder"
            : "implementation";
      artifacts.push({ path, kind, ...(moduleId ? { moduleId } : {}) });
    }
    return { ...this.metadata(), artifacts };
  }

  invalidate(params: InvalidateParams): InvalidateResult {
    const invalidated = params.paths
      .map((path) =>
        toPosix(
          relative(
            this.root,
            assertWithinRoot(this.root, resolve(this.root, path)),
          ),
        ),
      )
      .sort();
    this.#epoch += 1;
    this.refresh(invalidated);
    return { ...this.metadata(), invalidated };
  }
}

export function parseAnalyzeContractsParams(
  value: unknown,
): AnalyzeContractsParams {
  const input = record(value, "analyzeContracts params");
  assertOnlyParamKeys(input, new Set(["moduleIds"]), "analyzeContracts");
  return input.moduleIds === undefined
    ? {}
    : { moduleIds: stringArray(input, "moduleIds") };
}

export function parseAnalyzeWorkspaceParams(
  value: unknown,
): AnalyzeWorkspaceParams {
  const input = record(value, "analyzeWorkspace params");
  assertOnlyParamKeys(input, new Set(["moduleIds"]), "analyzeWorkspace");
  return input.moduleIds === undefined
    ? {}
    : { moduleIds: stringArray(input, "moduleIds") };
}

export function parseProjectContractParams(
  value: unknown,
): ProjectContractParams {
  const input = record(value, "projectContract params");
  assertOnlyParamKeys(
    input,
    new Set(["source", "symbol", "fileName"]),
    "projectContract",
  );
  return {
    source: stringField(input, "source"),
    symbol: stringField(input, "symbol"),
    fileName: workspacePath(stringField(input, "fileName"), "fileName", false),
  };
}

export function parseValidateOverlayParams(
  value: unknown,
): ValidateOverlayParams {
  const input = record(value, "validateOverlay params");
  assertOnlyParamKeys(
    input,
    new Set([
      "sessionId",
      "expectedEpoch",
      "expectedSnapshot",
      "candidates",
      "moduleIds",
      "syncModuleIds",
      "restampModuleIds",
      "recomposeModuleIds",
      "scopeToModuleIds",
      "baselineUnselected",
    ]),
    "validateOverlay",
  );
  if (!Number.isInteger(input.expectedEpoch)) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "expectedEpoch must be an integer",
    );
  }
  const candidates = record(input.candidates, "candidates");
  if (
    Object.values(candidates).some((candidate) => typeof candidate !== "string")
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "candidate values must be strings",
    );
  }
  if (
    input.scopeToModuleIds !== undefined &&
    typeof input.scopeToModuleIds !== "boolean"
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "scopeToModuleIds must be a boolean",
    );
  }
  if (
    input.baselineUnselected !== undefined &&
    typeof input.baselineUnselected !== "boolean"
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "baselineUnselected must be a boolean",
    );
  }
  return {
    sessionId: stringField(input, "sessionId"),
    expectedEpoch: input.expectedEpoch as number,
    expectedSnapshot: stringField(input, "expectedSnapshot"),
    candidates: candidates as Record<string, string>,
    ...(input.moduleIds === undefined
      ? {}
      : { moduleIds: stringArray(input, "moduleIds") }),
    ...(input.syncModuleIds === undefined
      ? {}
      : { syncModuleIds: stringArray(input, "syncModuleIds") }),
    ...(input.restampModuleIds === undefined
      ? {}
      : { restampModuleIds: stringArray(input, "restampModuleIds") }),
    ...(input.recomposeModuleIds === undefined
      ? {}
      : { recomposeModuleIds: stringArray(input, "recomposeModuleIds") }),
    ...(input.scopeToModuleIds === undefined
      ? {}
      : { scopeToModuleIds: input.scopeToModuleIds as boolean }),
    ...(input.baselineUnselected === undefined
      ? {}
      : { baselineUnselected: input.baselineUnselected as boolean }),
  };
}

export function parseFindOrphansParams(value: unknown): FindOrphansParams {
  const input = record(value, "findOrphans params");
  assertOnlyParamKeys(input, new Set(["moduleIds"]), "findOrphans");
  return input.moduleIds === undefined
    ? {}
    : { moduleIds: stringArray(input, "moduleIds") };
}

export function parseInvalidateParams(value: unknown): InvalidateParams {
  const input = record(value, "invalidate params");
  assertOnlyParamKeys(input, new Set(["paths"]), "invalidate");
  return { paths: stringArray(input, "paths") };
}

export function parseCancelParams(value: unknown): CancelParams {
  const input = record(value, "cancel params");
  assertOnlyParamKeys(input, new Set(["requestId"]), "cancel");
  const requestId = input.requestId;
  if (typeof requestId !== "string") {
    throw new WorkerError("INVALID_REQUEST", "requestId must be a string");
  }
  return { requestId };
}

export function parseEmptyParams(value: unknown, method: string): void {
  const input = record(value, `${method} params`);
  assertOnlyParamKeys(input, new Set(), method);
}
