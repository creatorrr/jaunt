import { existsSync, readFileSync, realpathSync } from "node:fs";
import {
  basename,
  dirname,
  extname,
  isAbsolute,
  join,
  relative,
  resolve,
  sep,
} from "node:path";
import { WorkerError } from "../protocol/errors.js";
import type { ModuleRoute, RoutePaths } from "./types.js";

const SPEC_PATTERN = /\.jaunt\.(ts|tsx)$/;

export function toPosix(path: string): string {
  return path.split(sep).join("/");
}

export function assertWithinRoot(root: string, candidate: string): string {
  const absoluteRoot = resolve(root);
  const absolute = resolve(candidate);
  assertLexicallyWithinRoot(absoluteRoot, absolute);
  const physicalRoot = realpathSync(absoluteRoot);
  let existing = absolute;
  const missing: string[] = [];
  while (!existsSync(existing)) {
    const parent = dirname(existing);
    if (parent === existing) break;
    missing.push(basename(existing));
    existing = parent;
  }
  const physical = resolve(realpathSync(existing), ...missing.reverse());
  const rel = relative(physicalRoot, physical);
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new WorkerError(
      "PATH_OUTSIDE_ROOT",
      `Path escapes workspace root: ${candidate}`,
    );
  }
  return absolute;
}

/**
 * Constrain a path by its workspace-visible name without rejecting package-manager
 * symlinks. Use this only for executable tooling resolved below node_modules; user
 * source, generated artifacts, and deletion targets must use assertWithinRoot so
 * their physical destination is checked as well.
 */
export function assertLexicallyWithinRoot(
  root: string,
  candidate: string,
): string {
  const absoluteRoot = resolve(root);
  const absolute = resolve(candidate);
  const rel = relative(absoluteRoot, absolute);
  if (rel === ".." || rel.startsWith(`..${sep}`) || isAbsolute(rel)) {
    throw new WorkerError(
      "PATH_OUTSIDE_ROOT",
      `Path escapes workspace root: ${candidate}`,
    );
  }
  return absolute;
}

export function deriveRoutePaths(
  specPath: string,
  generatedDir: string,
): RoutePaths {
  const match = SPEC_PATTERN.exec(specPath);
  if (!match)
    throw new WorkerError(
      "DISCOVERY_FAILED",
      `Not a TypeScript spec path: ${specPath}`,
    );
  const extension = match[1] === "tsx" ? ".tsx" : ".ts";
  const stem = specPath.slice(0, -`.jaunt${extension}`.length);
  const directory = dirname(stem);
  const base = stem.slice(directory.length + (directory === "." ? 0 : 1));
  const generated = join(directory, generatedDir);
  const contextTsx = `${stem}.context.tsx`;
  const contextTs = `${stem}.context.ts`;
  return {
    specPath,
    facadePath: `${stem}.ts`,
    apiMirrorPath: join(generated, `${base}.api.ts`),
    implementationPath: join(generated, `${base}${extension}`),
    sidecarPath: join(generated, `${base}.jaunt.json`),
    ...(existsSync(contextTsx)
      ? { contextPath: contextTsx }
      : existsSync(contextTs)
        ? { contextPath: contextTs }
        : {}),
  };
}

export function moduleIdFor(root: string, specPath: string): string {
  const relativePath = toPosix(relative(root, specPath)).replace(
    /\.jaunt\.(?:ts|tsx)$/,
    "",
  );
  return `ts:${relativePath}`;
}

export function nearestPackageOwner(root: string, path: string): string {
  let current = dirname(path);
  const absoluteRoot = resolve(root);
  while (true) {
    const containment = relative(absoluteRoot, current);
    if (
      containment === ".." ||
      containment.startsWith(`..${sep}`) ||
      isAbsolute(containment)
    ) {
      break;
    }
    if (existsSync(join(current, "package.json")))
      return toPosix(relative(absoluteRoot, current)) || ".";
    if (current === absoluteRoot) break;
    current = dirname(current);
  }
  return ".";
}

export function makeModuleRoute(
  root: string,
  specPath: string,
  generatedDir: string,
  project: string,
): ModuleRoute {
  const safeSpecPath = assertWithinRoot(root, specPath);
  const paths = deriveRoutePaths(safeSpecPath, generatedDir);
  for (const path of [
    paths.facadePath,
    paths.apiMirrorPath,
    paths.implementationPath,
    paths.sidecarPath,
    ...(paths.contextPath ? [paths.contextPath] : []),
  ]) {
    assertWithinRoot(root, path);
  }
  return {
    moduleId: moduleIdFor(root, safeSpecPath),
    project,
    packageOwner: nearestPackageOwner(root, safeSpecPath),
    specPath: toPosix(relative(root, paths.specPath)),
    facadePath: toPosix(relative(root, paths.facadePath)),
    apiMirrorPath: toPosix(relative(root, paths.apiMirrorPath)),
    implementationPath: toPosix(relative(root, paths.implementationPath)),
    sidecarPath: toPosix(relative(root, paths.sidecarPath)),
    ...(paths.contextPath === undefined
      ? {}
      : { contextPath: toPosix(relative(root, paths.contextPath)) }),
  };
}

export function readOptional(path: string): string | undefined {
  return existsSync(path) ? readFileSync(path, "utf8") : undefined;
}

export function scriptExtension(path: string): ".ts" | ".tsx" {
  return extname(path) === ".tsx" ? ".tsx" : ".ts";
}
