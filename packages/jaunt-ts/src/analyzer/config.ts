import { existsSync, readFileSync, realpathSync } from "node:fs";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import type ts from "@typescript/typescript6";
import { WorkerError } from "../protocol/errors.js";
import {
  assertLexicallyWithinRoot,
  assertWithinRoot,
  toPosix,
} from "./artifacts.js";
import { digestCanonical, sha256Bytes } from "./canonical.js";
import { compilerOptionsHash } from "./compiler_options.js";
import { fromTypeScriptDiagnostic, sortDiagnostics } from "./diagnostics.js";
import type { DiagnosticRecord, ProjectRecord } from "./types.js";

export interface LoadedProject {
  readonly id: string;
  readonly configPath: string;
  readonly role: "production" | "test" | "solution";
  readonly parsed: ts.ParsedCommandLine;
  readonly references: readonly string[];
  /** Entry config plus every config/package manifest read while resolving extends. */
  readonly configInputs: readonly string[];
}

function globRegex(pattern: string): RegExp {
  let normalized = pattern.replaceAll("\\", "/").replace(/^\.\//, "");
  if (!/[?*]/.test(normalized) && !/\.[A-Za-z0-9]+$/.test(normalized)) {
    normalized = `${normalized.replace(/\/$/, "")}/**/*`;
  }
  let regex = "";
  for (let index = 0; index < normalized.length; index += 1) {
    const character = normalized[index]!;
    if (character === "*" && normalized[index + 1] === "*") {
      if (normalized[index + 2] === "/") {
        regex += "(?:.*/)?";
        index += 2;
      } else {
        regex += ".*";
        index += 1;
      }
    } else if (character === "*") {
      regex += "[^/]*";
    } else if (character === "?") {
      regex += "[^/]";
    } else {
      regex += /[.+^${}()|[\]\\]/.test(character)
        ? `\\${character}`
        : character;
    }
  }
  return new RegExp(`^${regex}$`);
}

function claimsPath(project: LoadedProject, path: string): boolean {
  const absolute = resolve(path);
  if (project.parsed.fileNames.some((file) => resolve(file) === absolute))
    return true;
  const rel = toPosix(relative(dirname(project.configPath), absolute));
  if (rel === ".." || rel.startsWith("../")) return false;
  const raw = project.parsed.raw as
    { files?: unknown; include?: unknown; exclude?: unknown } | undefined;
  const files = Array.isArray(raw?.files)
    ? raw.files.filter((item): item is string => typeof item === "string")
    : [];
  if (files.length > 0) return files.some((file) => toPosix(file) === rel);
  const includes = Array.isArray(raw?.include)
    ? raw.include.filter((item): item is string => typeof item === "string")
    : ["**/*"];
  const excludes = Array.isArray(raw?.exclude)
    ? raw.exclude.filter((item): item is string => typeof item === "string")
    : ["node_modules", "bower_components", "jspm_packages"];
  return (
    includes.some((pattern) => globRegex(pattern).test(rel)) &&
    !excludes.some((pattern) => globRegex(pattern).test(rel))
  );
}

export interface ProjectGraph {
  readonly projects: readonly LoadedProject[];
  readonly records: readonly ProjectRecord[];
  readonly diagnostics: readonly DiagnosticRecord[];
}

export interface ToolchainIdentity {
  readonly compilerModulePath: string;
  readonly toolOwner: string;
}

function allowedConfigInput(root: string, candidate: string): string {
  const absolute = resolve(candidate);
  let lexical: string;
  try {
    lexical = assertLexicallyWithinRoot(root, absolute);
  } catch {
    throw new WorkerError(
      "CONFIG_INVALID",
      `TypeScript config extends outside the workspace/package roots: ${absolute}`,
    );
  }
  const segments = toPosix(relative(root, lexical)).split("/");
  if (segments.includes("node_modules")) return lexical;
  try {
    return assertWithinRoot(root, lexical);
  } catch {
    throw new WorkerError(
      "CONFIG_INVALID",
      `TypeScript config extends through a path outside the workspace/package roots: ${toPosix(relative(root, lexical))}`,
    );
  }
}

function configHost(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  onRead: (path: string) => void,
): ts.ParseConfigFileHost {
  return {
    useCaseSensitiveFileNames: compiler.sys.useCaseSensitiveFileNames,
    readDirectory: compiler.sys.readDirectory,
    fileExists: compiler.sys.fileExists,
    readFile: (path) => {
      const allowed = allowedConfigInput(root, path);
      onRead(allowed);
      return compiler.sys.readFile(allowed);
    },
    getCurrentDirectory: compiler.sys.getCurrentDirectory,
    onUnRecoverableConfigFileDiagnostic: () => undefined,
  };
}

function expandProjectEntry(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  entry: string,
): string[] {
  if (!/[?*]/.test(entry)) {
    const path = assertWithinRoot(root, resolve(root, entry));
    if (!existsSync(path))
      throw new WorkerError(
        "CONFIG_INVALID",
        `TypeScript project not found: ${entry}`,
      );
    return [path];
  }
  return compiler.sys
    .readDirectory(root, [".json"], undefined, [entry])
    .map((path) => assertWithinRoot(root, path))
    .sort();
}

export function loadProjectGraph(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  productionEntries: readonly string[],
  testEntries: readonly string[],
  toolchain?: ToolchainIdentity,
): ProjectGraph {
  const production = new Set(
    productionEntries.flatMap((entry) =>
      expandProjectEntry(compiler, root, entry),
    ),
  );
  const tests = new Set(
    testEntries.flatMap((entry) => expandProjectEntry(compiler, root, entry)),
  );
  type Reachability = "production" | "test";
  const pending: Array<{ path: string; reachability: Reachability }> = [
    ...[...production].map((path) => ({
      path,
      reachability: "production" as const,
    })),
    ...[...tests].map((path) => ({
      path,
      reachability: "test" as const,
    })),
  ];
  const parsedByPath = new Map<
    string,
    {
      parsed: ts.ParsedCommandLine;
      references: readonly string[];
      configInputs: readonly string[];
    }
  >();
  const reachability = new Map<
    string,
    { production: boolean; test: boolean }
  >();
  const visited = new Set<string>();
  const diagnostics: DiagnosticRecord[] = [];
  let activeConfigInputs: Set<string> | undefined;
  const host = configHost(compiler, root, (path) =>
    activeConfigInputs?.add(path),
  );

  while (pending.length > 0) {
    const item = pending.shift();
    if (!item) continue;
    const configPath = resolve(item.path);
    const visitKey = `${item.reachability}\0${configPath}`;
    if (visited.has(visitKey)) continue;
    visited.add(visitKey);
    const reached = reachability.get(configPath) ?? {
      production: false,
      test: false,
    };
    reached[item.reachability] = true;
    reachability.set(configPath, reached);

    let parsedProject = parsedByPath.get(configPath);
    if (!parsedProject) {
      if (!existsSync(configPath)) {
        throw new WorkerError(
          "CONFIG_INVALID",
          `Referenced TypeScript project not found: ${toPosix(relative(root, configPath))}`,
        );
      }
      const configInputs = new Set<string>();
      activeConfigInputs = configInputs;
      let parsed: ts.ParsedCommandLine | undefined;
      try {
        parsed = compiler.getParsedCommandLineOfConfigFile(
          configPath,
          {},
          host,
        );
      } finally {
        activeConfigInputs = undefined;
      }
      if (!parsed)
        throw new WorkerError(
          "CONFIG_INVALID",
          `Unable to parse ${toPosix(relative(root, configPath))}`,
        );
      for (const file of parsed.fileNames) assertWithinRoot(root, file);
      diagnostics.push(
        ...parsed.errors
          .filter((diagnostic) => diagnostic.code !== 18003)
          .map((diagnostic) =>
            fromTypeScriptDiagnostic(compiler, root, diagnostic),
          ),
      );
      const references = (parsed.projectReferences ?? [])
        .map((reference) => compiler.resolveProjectReferencePath(reference))
        .map((path) => assertWithinRoot(root, path))
        .sort();
      parsedProject = {
        parsed,
        references,
        configInputs: [...configInputs].sort(),
      };
      parsedByPath.set(configPath, parsedProject);
    }
    pending.push(
      ...parsedProject.references.map((path) => ({
        path,
        reachability: item.reachability,
      })),
    );
  }

  const projects = [...parsedByPath.entries()]
    .map(
      ([configPath, { parsed, references, configInputs }]): LoadedProject => {
        const reached = reachability.get(configPath);
        const raw = parsed.raw as
          { files?: unknown; include?: unknown } | undefined;
        const solutionOnly =
          parsed.fileNames.length === 0 &&
          references.length > 0 &&
          Array.isArray(raw?.files) &&
          raw.files.length === 0 &&
          !Array.isArray(raw?.include);
        const role = solutionOnly
          ? "solution"
          : reached?.production
            ? "production"
            : "test";
        return {
          id: toPosix(relative(root, configPath)),
          configPath,
          role,
          parsed,
          references: references.map((path) => toPosix(relative(root, path))),
          configInputs,
        };
      },
    )
    .sort((left, right) => left.id.localeCompare(right.id));
  diagnostics.push(...projectReferenceCycleDiagnostics(projects));
  if (toolchain) {
    diagnostics.push(
      ...toolchainIdentityDiagnostics(root, projects, toolchain),
    );
  }
  for (const project of projects) {
    for (const file of project.parsed.fileNames) {
      if (/\.jaunt(?:-test)?\.(?:ts|tsx)$/.test(file)) {
        diagnostics.push({
          code: "JAUNT_TS_PRIVATE_INPUT_EMITTED",
          severity: "error",
          message:
            "Private Jaunt spec inputs must be excluded from emitting TypeScript projects; add **/*.jaunt.ts, **/*.jaunt.tsx, **/*.jaunt-test.ts, and **/*.jaunt-test.tsx to exclude.",
          path: toPosix(relative(root, file)),
        });
      }
    }
  }

  return {
    projects,
    records: projects.map((project) => ({
      id: project.id,
      configPath: project.id,
      role: project.role,
      references: project.references,
      rootFiles: project.parsed.fileNames
        .map((path) => toPosix(relative(root, path)))
        .sort(),
      compilerOptionsHash: digestCanonical({
        options: compilerOptionsHash(
          root,
          project.configPath,
          project.parsed.options,
        ),
        configInputs: project.configInputs.map((path) => ({
          path: toPosix(relative(root, path)),
          sha256: sha256Bytes(readFileSync(path)),
        })),
      }),
    })),
    diagnostics: sortDiagnostics(diagnostics),
  };
}

function projectReferenceCycleDiagnostics(
  projects: readonly LoadedProject[],
): DiagnosticRecord[] {
  const byId = new Map(projects.map((project) => [project.id, project]));
  const state = new Map<string, "visiting" | "visited">();
  const stack: string[] = [];
  const reported = new Set<string>();
  const diagnostics: DiagnosticRecord[] = [];
  function visit(id: string): void {
    if (state.get(id) === "visited") return;
    if (state.get(id) === "visiting") return;
    state.set(id, "visiting");
    stack.push(id);
    for (const dependency of byId.get(id)?.references ?? []) {
      if (!byId.has(dependency)) continue;
      if (state.get(dependency) === "visiting") {
        const start = stack.indexOf(dependency);
        const cycle = [...stack.slice(Math.max(0, start)), dependency];
        const key = [...new Set(cycle)].sort().join("\0");
        if (!reported.has(key)) {
          reported.add(key);
          diagnostics.push({
            code: "JAUNT_TS_PROJECT_REFERENCE_CYCLE",
            severity: "error",
            message: `TypeScript project-reference cycle: ${cycle.join(" -> ")}`,
            path: id,
          });
        }
        continue;
      }
      visit(dependency);
    }
    stack.pop();
    state.set(id, "visited");
  }
  for (const id of [...byId.keys()].sort()) visit(id);
  return diagnostics;
}

function nearestInstalledFile(
  root: string,
  start: string,
  packagePath: string,
): string | undefined {
  const boundary = resolve(root);
  let current = resolve(start);
  while (true) {
    const containment = relative(boundary, current);
    if (
      containment === ".." ||
      containment.startsWith(`..${sep}`) ||
      isAbsolute(containment)
    ) {
      return undefined;
    }
    const candidate = join(current, "node_modules", packagePath);
    if (existsSync(candidate)) return candidate;
    if (current === boundary) return undefined;
    current = dirname(current);
  }
}

function samePhysicalFile(left: string, right: string): boolean {
  try {
    return realpathSync(left) === realpathSync(right);
  } catch {
    return false;
  }
}

function relativeIdentityPath(root: string, path: string): string {
  const rendered = toPosix(relative(root, path));
  return rendered === "" ? "." : rendered;
}

function toolchainIdentityDiagnostics(
  root: string,
  projects: readonly LoadedProject[],
  toolchain: ToolchainIdentity,
): DiagnosticRecord[] {
  const diagnostics: DiagnosticRecord[] = [];
  const compiler = resolve(toolchain.compilerModulePath);
  const toolOwner = resolve(root, toolchain.toolOwner);
  const expectedWorker = nearestInstalledFile(
    root,
    toolOwner,
    "@usejaunt/ts/package.json",
  );
  for (const project of projects) {
    const directory = dirname(project.configPath);
    const projectCompiler = nearestInstalledFile(
      root,
      directory,
      "typescript/lib/typescript.js",
    );
    if (projectCompiler && !samePhysicalFile(projectCompiler, compiler)) {
      diagnostics.push({
        code: "JAUNT_TS_COMPILER_IDENTITY_MISMATCH",
        severity: "error",
        message: `${project.id} resolves ${relativeIdentityPath(root, projectCompiler)}, not the TypeScript compiler selected by target.ts.tool_owner (${relativeIdentityPath(root, compiler)})`,
        path: project.id,
      });
    }
    const projectWorker = nearestInstalledFile(
      root,
      directory,
      "@usejaunt/ts/package.json",
    );
    if (
      expectedWorker &&
      projectWorker &&
      !samePhysicalFile(projectWorker, expectedWorker)
    ) {
      diagnostics.push({
        code: "JAUNT_TS_WORKER_IDENTITY_MISMATCH",
        severity: "error",
        message: `${project.id} resolves ${relativeIdentityPath(root, projectWorker)}, not the @usejaunt/ts worker selected by target.ts.tool_owner (${relativeIdentityPath(root, expectedWorker)})`,
        path: project.id,
      });
    }
  }
  return diagnostics;
}

export function projectReferencesProject(
  projects: readonly LoadedProject[],
  consumerId: string,
  dependencyId: string,
): boolean {
  if (consumerId === dependencyId) return true;
  const byId = new Map(projects.map((project) => [project.id, project]));
  const seen = new Set<string>();
  const pending = [...(byId.get(consumerId)?.references ?? [])];
  while (pending.length > 0) {
    const id = pending.pop();
    if (!id || seen.has(id)) continue;
    if (id === dependencyId) return true;
    seen.add(id);
    pending.push(...(byId.get(id)?.references ?? []));
  }
  return false;
}

export function affectedProjectIds(
  projects: readonly LoadedProject[],
  changedProjectIds: ReadonlySet<string>,
): readonly string[] {
  const dependents = new Map<string, string[]>();
  for (const project of projects) {
    for (const dependency of project.references) {
      const values = dependents.get(dependency) ?? [];
      values.push(project.id);
      dependents.set(dependency, values);
    }
  }
  const affected = new Set(changedProjectIds);
  const pending = [...changedProjectIds];
  while (pending.length > 0) {
    const id = pending.shift();
    if (!id) continue;
    for (const dependent of (dependents.get(id) ?? []).sort()) {
      if (affected.has(dependent)) continue;
      affected.add(dependent);
      pending.push(dependent);
    }
  }

  const byId = new Map(projects.map((project) => [project.id, project]));
  const visited = new Set<string>();
  const ordered: string[] = [];
  function visit(id: string): void {
    if (visited.has(id)) return;
    visited.add(id);
    for (const dependency of [...(byId.get(id)?.references ?? [])].sort()) {
      if (affected.has(dependency)) visit(dependency);
    }
    if (affected.has(id) && byId.get(id)?.role !== "solution") ordered.push(id);
  }
  for (const id of [...affected].sort()) visit(id);
  return ordered;
}

export function ownerForPath(
  root: string,
  projects: readonly LoadedProject[],
  path: string,
): LoadedProject {
  const absolute = resolve(path);
  const candidates = projects
    .filter((project) => project.role === "production")
    .filter((project) => {
      const configDirectory = dirname(project.configPath);
      const rel = relative(configDirectory, absolute);
      return rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
    })
    .filter((project) => claimsPath(project, absolute))
    .sort(
      (left, right) =>
        dirname(right.configPath).length - dirname(left.configPath).length,
    );
  const winner = candidates[0];
  if (!winner) {
    throw new WorkerError(
      "CONFIG_INVALID",
      `No configured production TypeScript project owns ${toPosix(relative(root, path))}`,
    );
  }
  if (candidates.length > 1) {
    throw new WorkerError(
      "PROJECT_AMBIGUOUS",
      `Several TypeScript production projects claim ${toPosix(relative(root, path))}: ${candidates
        .map((item) => item.id)
        .sort()
        .join(", ")}`,
    );
  }
  return winner;
}

export function testOwnerForPath(
  root: string,
  projects: readonly LoadedProject[],
  path: string,
  generatedTestPath = path,
): LoadedProject {
  const testProjects = projects.filter((project) => project.role === "test");
  if (testProjects.length === 0)
    return ownerForPath(root, projects, generatedTestPath);
  const absolute = resolve(generatedTestPath);
  const candidates = testProjects
    .filter((project) => {
      const rel = relative(dirname(project.configPath), absolute);
      return rel !== ".." && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
    })
    .filter((project) => claimsPath(project, absolute))
    .sort(
      (left, right) =>
        dirname(right.configPath).length - dirname(left.configPath).length,
    );
  const winner = candidates[0];
  if (!winner) return ownerForPath(root, projects, generatedTestPath);
  if (candidates.length > 1) {
    throw new WorkerError(
      "PROJECT_AMBIGUOUS",
      `Several TypeScript test projects claim ${toPosix(relative(root, generatedTestPath))}: ${candidates
        .map((item) => item.id)
        .sort()
        .join(", ")}`,
    );
  }
  return winner;
}
