import {
  basename,
  dirname,
  isAbsolute,
  relative,
  resolve,
  sep,
} from "node:path";
import type ts from "@typescript/typescript6";
import { reusableSourceFile } from "./source_file_reuse.js";
import { digestCanonical } from "./canonical.js";
import { affectedProjectIds, type LoadedProject } from "./config.js";
import { compilerOptionsHash } from "./compiler_options.js";

interface ProjectProgramEntry {
  readonly project: LoadedProject;
  readonly program: ts.Program;
  readonly roots: ReadonlySet<string>;
  readonly configKey: string;
  readonly generation: number;
  readonly rebuilds: number;
  readonly reusedSourceFiles: number;
}

export interface AnalysisProgramState {
  readonly projectId: string;
  readonly generation: number;
  readonly rebuilds: number;
  readonly rootCount: number;
  readonly sourceFileCount: number;
  readonly reusedSourceFiles: number;
  readonly reused: boolean;
}

function isWithin(parent: string, child: string): boolean {
  const value = relative(resolve(parent), resolve(child));
  return value !== ".." && !value.startsWith(`..${sep}`) && !isAbsolute(value);
}

function projectConfigKey(root: string, project: LoadedProject): string {
  return digestCanonical({
    configPath: project.id,
    options: compilerOptionsHash(
      root,
      project.configPath,
      project.parsed.options,
    ),
    references: project.references,
    configInputs: project.configInputs.map((path) => resolve(path)),
    role: project.role,
  });
}

function rootNamesFor(
  project: LoadedProject,
  extraRoots: ReadonlyMap<string, readonly string[]>,
): string[] {
  return [
    ...new Set([
      ...project.parsed.fileNames.map((path) => resolve(path)),
      ...(extraRoots.get(project.id) ?? []).map((path) => resolve(path)),
    ]),
  ].sort();
}

function directlyOwningProjects(
  path: string,
  projects: readonly LoadedProject[],
  previous: ReadonlyMap<string, ProjectProgramEntry>,
  roots: ReadonlyMap<string, readonly string[]>,
): Set<string> {
  const absolute = resolve(path);
  const direct = new Set<string>();
  for (const project of projects) {
    if (
      resolve(project.configPath) === absolute ||
      project.configInputs.some((file) => resolve(file) === absolute) ||
      project.parsed.fileNames.some((file) => resolve(file) === absolute) ||
      (roots.get(project.id) ?? []).some((file) => resolve(file) === absolute)
    ) {
      direct.add(project.id);
    }
  }
  for (const entry of previous.values()) {
    if (
      resolve(entry.project.configPath) === absolute ||
      entry.project.configInputs.some((file) => resolve(file) === absolute) ||
      entry.roots.has(absolute) ||
      entry.program
        .getSourceFiles()
        .some((sourceFile) => resolve(sourceFile.fileName) === absolute)
    ) {
      direct.add(entry.project.id);
    }
  }
  if (direct.size > 0) return direct;

  // Config inheritance and package-manager metadata are not necessarily
  // Program source files.  Attribute them to every nested config so a changed
  // base config, manifest, or lockfile refreshes the projects that can consume
  // it without throwing away unrelated workspace siblings.
  const name = basename(absolute);
  if (
    /^tsconfig(?:\.[^.]+)*\.json$/i.test(name) ||
    /^(?:package(?:-lock)?\.json|npm-shrinkwrap\.json|pnpm-lock\.yaml|yarn\.lock|bun\.lockb?)$/.test(
      name,
    )
  ) {
    for (const project of projects) {
      if (isWithin(dirname(absolute), project.configPath)) {
        direct.add(project.id);
      }
    }
    for (const entry of previous.values()) {
      if (isWithin(dirname(absolute), entry.project.configPath)) {
        direct.add(entry.project.id);
      }
    }
    if (direct.size > 0) return direct;
  }

  // New files are absent from the old Program and may still be excluded from a
  // parsed config (for example a private *.jaunt.ts input).  The nearest
  // containing project directory is the narrowest sound ownership boundary.
  const candidates = [
    ...projects,
    ...[...previous.values()].map((item) => item.project),
  ]
    .filter((project) => project.role !== "solution")
    .filter((project) => isWithin(dirname(project.configPath), absolute))
    .sort(
      (left, right) =>
        dirname(right.configPath).length - dirname(left.configPath).length,
    );
  const nearest = candidates[0];
  if (nearest) direct.add(nearest.id);
  return direct;
}

function analysisHost(
  compiler: typeof import("@typescript/typescript6"),
  options: ts.CompilerOptions,
  oldProgram: ts.Program | undefined,
  invalidated: ReadonlySet<string>,
): ts.CompilerHost {
  const host = compiler.createCompilerHost(options, true);
  if (!oldProgram) return host;
  const getSourceFile = host.getSourceFile.bind(host);
  host.getSourceFile = (
    fileName,
    languageVersionOrOptions,
    onError,
    shouldCreateNewSourceFile,
  ) => {
    const absolute = resolve(fileName);
    if (!shouldCreateNewSourceFile && !invalidated.has(absolute)) {
      const previous = reusableSourceFile(
        oldProgram.getSourceFile(fileName) ??
          oldProgram.getSourceFile(absolute),
      );
      if (previous) return previous;
    }
    return getSourceFile(
      fileName,
      languageVersionOrOptions,
      onError,
      shouldCreateNewSourceFile,
    );
  };
  return host;
}

/**
 * Owns the worker's one non-emitting analysis Program per concrete config.
 *
 * Unaffected projects retain the exact Program object.  Affected projects are
 * recreated with `oldProgram` and a host that keeps unchanged SourceFiles, so
 * TypeScript can reuse structure while changed files are read again.  Solution
 * configs remain graph-only nodes and never allocate a Program.
 */
export class AnalysisProgramCache {
  readonly #compiler: typeof import("@typescript/typescript6");
  readonly #root: string;
  #entries = new Map<string, ProjectProgramEntry>();
  #lastReused = new Set<string>();
  #nextGeneration = 1;

  constructor(
    compiler: typeof import("@typescript/typescript6"),
    root: string,
  ) {
    this.#compiler = compiler;
    this.#root = resolve(root);
  }

  prepare(
    projects: readonly LoadedProject[],
    extraRoots: ReadonlyMap<string, readonly string[]>,
    invalidatedPaths: readonly string[] = [],
  ): void {
    const previous = this.#entries;
    const invalidated = new Set(
      invalidatedPaths.map((path) => resolve(this.#root, path)),
    );
    const nextRoots = new Map(
      projects.map((project) => [
        project.id,
        rootNamesFor(project, extraRoots),
      ]),
    );
    const directlyChanged = new Set<string>();
    for (const path of invalidated) {
      for (const id of directlyOwningProjects(
        path,
        projects,
        previous,
        nextRoots,
      )) {
        directlyChanged.add(id);
      }
    }
    const nextById = new Map(projects.map((project) => [project.id, project]));
    for (const [id, entry] of previous) {
      const project = nextById.get(id);
      if (!project) {
        directlyChanged.add(id);
        continue;
      }
      const roots = nextRoots.get(id) ?? [];
      if (
        entry.configKey !== projectConfigKey(this.#root, project) ||
        roots.length !== entry.roots.size ||
        roots.some((path) => !entry.roots.has(path))
      ) {
        directlyChanged.add(id);
      }
    }
    for (const project of projects) {
      if (project.role !== "solution" && !previous.has(project.id)) {
        directlyChanged.add(project.id);
      }
    }

    const affected = new Set([
      ...affectedProjectIds(
        [...previous.values()].map((entry) => entry.project),
        directlyChanged,
      ),
      ...affectedProjectIds(projects, directlyChanged),
      ...directlyChanged,
    ]);
    const entries = new Map<string, ProjectProgramEntry>();
    const reused = new Set<string>();
    for (const project of projects) {
      const roots = nextRoots.get(project.id) ?? [];
      if (project.role === "solution" || roots.length === 0) continue;
      const old = previous.get(project.id);
      const configKey = projectConfigKey(this.#root, project);
      if (
        old &&
        !affected.has(project.id) &&
        old.configKey === configKey &&
        roots.length === old.roots.size &&
        roots.every((path) => old.roots.has(path))
      ) {
        entries.set(project.id, old);
        reused.add(project.id);
        continue;
      }

      const mayReuseOldProgram = old?.configKey === configKey;
      const options: ts.CompilerOptions = {
        ...project.parsed.options,
        noEmit: true,
      };
      const oldProgram = mayReuseOldProgram ? old.program : undefined;
      const program = this.#compiler.createProgram({
        rootNames: roots,
        options,
        ...(project.parsed.projectReferences
          ? { projectReferences: project.parsed.projectReferences }
          : {}),
        ...(oldProgram ? { oldProgram } : {}),
        host: analysisHost(this.#compiler, options, oldProgram, invalidated),
      });
      const previousSourceFiles = new Set(oldProgram?.getSourceFiles() ?? []);
      entries.set(project.id, {
        project,
        program,
        roots: new Set(roots),
        configKey,
        generation: this.#nextGeneration++,
        rebuilds: (old?.rebuilds ?? 0) + 1,
        reusedSourceFiles: program
          .getSourceFiles()
          .filter((sourceFile) => previousSourceFiles.has(sourceFile)).length,
      });
    }
    this.#entries = entries;
    this.#lastReused = reused;
  }

  programFor(projectId: string): ts.Program {
    const entry = this.#entries.get(projectId);
    if (!entry) {
      throw new Error(`No analysis Program exists for ${projectId}`);
    }
    return entry.program;
  }

  reused(projectId: string): boolean {
    return this.#lastReused.has(projectId);
  }

  state(): readonly AnalysisProgramState[] {
    return [...this.#entries.entries()]
      .map(([projectId, entry]) => ({
        projectId,
        generation: entry.generation,
        rebuilds: entry.rebuilds,
        rootCount: entry.roots.size,
        sourceFileCount: entry.program.getSourceFiles().length,
        reusedSourceFiles: entry.reusedSourceFiles,
        reused: this.#lastReused.has(projectId),
      }))
      .sort((left, right) => left.projectId.localeCompare(right.projectId));
  }
}
