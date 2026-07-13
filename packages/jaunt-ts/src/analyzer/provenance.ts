import { existsSync, readFileSync } from "node:fs";
import { builtinModules } from "node:module";
import { dirname, isAbsolute, join, relative, resolve, sep } from "node:path";
import type { DiagnosticRecord } from "./types.js";

const BUILTINS = new Set(
  builtinModules.flatMap((name) => [name, `node:${name}`]),
);

/**
 * Provenance facts produced by module resolution.
 *
 * `resolvedWorkspaceFile` is deliberately limited to a real source file in
 * the configured workspace.  Callers must not treat a physical npm/pnpm store
 * path as package authorization: the logical package name comes from the
 * authored specifier (including its package.json `imports` indirection).
 */
export interface PackageImportResolution {
  readonly resolvedWorkspaceFile?: string;
}

export function packageNameForSpecifier(specifier: string): string | undefined {
  if (
    specifier.startsWith(".") ||
    specifier.startsWith("/") ||
    specifier.startsWith("#")
  )
    return undefined;
  if (BUILTINS.has(specifier)) return undefined;
  const parts = specifier.split("/");
  return specifier.startsWith("@") ? parts.slice(0, 2).join("/") : parts[0];
}

function packageManifest(
  root: string,
  file: string,
): { path: string; value: Record<string, unknown> } | undefined {
  let current = dirname(file);
  const boundary = resolve(root);
  while (true) {
    const containment = relative(boundary, current);
    if (
      containment === ".." ||
      containment.startsWith(`..${sep}`) ||
      isAbsolute(containment)
    ) {
      break;
    }
    const path = join(current, "package.json");
    if (existsSync(path)) {
      try {
        return {
          path,
          value: JSON.parse(readFileSync(path, "utf8")) as Record<
            string,
            unknown
          >,
        };
      } catch {
        return undefined;
      }
    }
    if (current === boundary) break;
    current = dirname(current);
  }
  return undefined;
}

interface ImportsAliasResolution {
  readonly matched: boolean;
  readonly packageNames: readonly string[];
  readonly invalid?: string;
}

function isWithin(parent: string, child: string): boolean {
  const containment = relative(resolve(parent), resolve(child));
  return (
    containment !== ".." &&
    !containment.startsWith(`..${sep}`) &&
    !isAbsolute(containment)
  );
}

function unsafePackageRelativeTarget(target: string): boolean {
  let decoded: string;
  try {
    decoded = decodeURIComponent(target.slice(2));
  } catch {
    return true;
  }
  return decoded.split(/[\\/]/).some((segment) => {
    const normalized = segment.toLowerCase();
    return (
      normalized === "." || normalized === ".." || normalized === "node_modules"
    );
  });
}

function importsMatch(
  imports: Readonly<Record<string, unknown>>,
  specifier: string,
):
  | {
      readonly target: unknown;
      readonly wildcard?: string;
      readonly appendSubpath?: boolean;
    }
  | undefined {
  if (Object.hasOwn(imports, specifier)) {
    return { target: imports[specifier] };
  }
  const patterns = Object.keys(imports)
    .filter((key) => {
      const star = key.indexOf("*");
      if (star < 0) return key.endsWith("/") && specifier.startsWith(key);
      if (star !== key.lastIndexOf("*")) return false;
      const prefix = key.slice(0, star);
      const suffix = key.slice(star + 1);
      return (
        specifier.length >= prefix.length + suffix.length &&
        specifier.startsWith(prefix) &&
        specifier.endsWith(suffix)
      );
    })
    // Match Node/TypeScript's package-pattern precedence, including the legacy
    // trailing-slash prefix form that both resolvers still accept.
    .sort((left, right) => {
      const leftStar = left.indexOf("*");
      const rightStar = right.indexOf("*");
      const leftBase = leftStar < 0 ? left.length : leftStar + 1;
      const rightBase = rightStar < 0 ? right.length : rightStar + 1;
      if (leftBase !== rightBase) return rightBase - leftBase;
      if (leftStar < 0) return 1;
      if (rightStar < 0) return -1;
      return right.length - left.length || left.localeCompare(right);
    });
  const key = patterns[0];
  if (!key) return undefined;
  const star = key.indexOf("*");
  if (star < 0) {
    return {
      target: imports[key],
      wildcard: specifier.slice(key.length),
      appendSubpath: true,
    };
  }
  return {
    target: imports[key],
    wildcard: specifier.slice(star, specifier.length - (key.length - star - 1)),
  };
}

function packageImports(
  manifest: Record<string, unknown>,
): Readonly<Record<string, unknown>> | undefined {
  const imports = manifest.imports;
  return imports && typeof imports === "object" && !Array.isArray(imports)
    ? (imports as Readonly<Record<string, unknown>>)
    : undefined;
}

function resolveImportsAlias(
  manifest: { path: string; value: Record<string, unknown> },
  specifier: string,
  seen: ReadonlySet<string> = new Set(),
): ImportsAliasResolution {
  const imports = packageImports(manifest.value);
  const match = imports ? importsMatch(imports, specifier) : undefined;
  if (!match) return { matched: false, packageNames: [] };
  if (seen.has(specifier)) {
    return {
      matched: true,
      packageNames: [],
      invalid: `package.json imports alias ${JSON.stringify(specifier)} is cyclic`,
    };
  }
  const nextSeen = new Set(seen).add(specifier);
  const packageNames = new Set<string>();
  let invalid: string | undefined;

  const visit = (value: unknown): void => {
    if (invalid || value === null) return;
    if (typeof value === "string") {
      if (match.appendSubpath && !value.endsWith("/")) {
        invalid = `package.json imports alias ${JSON.stringify(specifier)} has a non-directory target for a trailing-slash mapping`;
        return;
      }
      const target =
        match.wildcard === undefined
          ? value
          : match.appendSubpath
            ? `${value}${match.wildcard}`
            : value.replaceAll("*", match.wildcard);
      if (target.startsWith("#")) {
        const nested = resolveImportsAlias(manifest, target, nextSeen);
        if (!nested.matched) {
          invalid = `package.json imports alias ${JSON.stringify(specifier)} targets missing alias ${JSON.stringify(target)}`;
          return;
        }
        if (nested.invalid) {
          invalid = nested.invalid;
          return;
        }
        for (const name of nested.packageNames) packageNames.add(name);
        return;
      }
      if (target.startsWith("./")) {
        const packageRoot = dirname(manifest.path);
        const resolvedTarget = resolve(packageRoot, target);
        if (
          !isWithin(packageRoot, resolvedTarget) ||
          unsafePackageRelativeTarget(target)
        ) {
          invalid = `package.json imports alias ${JSON.stringify(specifier)} has an unsafe package-relative target`;
        }
        return;
      }
      const packageName = packageNameForSpecifier(target);
      if (packageName) {
        packageNames.add(packageName);
        return;
      }
      if (!BUILTINS.has(target)) {
        invalid = `package.json imports alias ${JSON.stringify(specifier)} has invalid target ${JSON.stringify(target)}`;
      }
      return;
    }
    if (Array.isArray(value)) {
      for (const entry of value) visit(entry);
      return;
    }
    if (value && typeof value === "object") {
      for (const entry of Object.values(value)) visit(entry);
      return;
    }
    invalid = `package.json imports alias ${JSON.stringify(specifier)} has a non-string target`;
  };

  visit(match.target);
  return {
    matched: true,
    packageNames: [...packageNames].sort(),
    ...(invalid ? { invalid } : {}),
  };
}

function dependencySet(
  value: Record<string, unknown>,
  key: string,
): Set<string> {
  const dependencies = value[key];
  return dependencies &&
    typeof dependencies === "object" &&
    !Array.isArray(dependencies)
    ? new Set(Object.keys(dependencies))
    : new Set();
}

export function auditPackageImport(
  root: string,
  file: string,
  specifier: string,
  testOnly: boolean,
  resolution?: string | PackageImportResolution,
  allowJauntTooling = true,
): DiagnosticRecord | undefined {
  const resolvedWorkspaceFile =
    typeof resolution === "string"
      ? resolution
      : resolution?.resolvedWorkspaceFile;
  const manifest = packageManifest(root, file);
  const alias =
    specifier.startsWith("#") && manifest
      ? resolveImportsAlias(manifest, specifier)
      : undefined;
  if (alias?.invalid) {
    return {
      code: "JAUNT_TS_PACKAGE_IMPORTS_INVALID",
      severity: "error",
      message: alias.invalid,
    };
  }
  const packageNames = new Set<string>();
  const directPackageName = packageNameForSpecifier(specifier);
  if (directPackageName) packageNames.add(directPackageName);
  for (const name of alias?.packageNames ?? []) packageNames.add(name);
  const hasLogicalAliasTarget = (alias?.packageNames.length ?? 0) > 0;
  if (packageNames.has("@usejaunt/ts")) {
    if (!allowJauntTooling) {
      return {
        code: "JAUNT_TS_TOOLING_RUNTIME_IMPORT",
        severity: "error",
        message:
          "Generated artifacts may not import @usejaunt/ts; Jaunt tooling belongs only in private authored spec inputs",
      };
    }
    packageNames.delete("@usejaunt/ts");
  }
  const resolvedManifest = resolvedWorkspaceFile
    ? packageManifest(root, resolvedWorkspaceFile)
    : undefined;
  if (packageNames.size === 0 && !resolvedManifest) return undefined;
  if (!manifest) {
    return {
      code: "JAUNT_TS_PACKAGE_OWNER_MISSING",
      severity: "error",
      message: `No package.json owns import ${JSON.stringify(specifier)}`,
    };
  }
  // A package-import alias is authorized by its logical target, not merely by
  // wherever a tsconfig `paths` mapping happens to resolve it.  Same-package
  // physical resolution therefore proves only a relative/internal alias; it
  // must not erase an external target declared in `package.json#imports`.
  if (resolvedManifest?.path === manifest.path && !hasLogicalAliasTarget)
    return undefined;
  const resolvedPackageName = resolvedManifest?.value.name;
  if (
    typeof resolvedPackageName === "string" &&
    resolvedPackageName.trim() !== ""
  ) {
    // A source path alias may use an ergonomic spelling that is not the
    // sibling package's published name.  Preserve the established rule: once
    // resolution proves a cross-package workspace owner, that owner's name is
    // the dependency that must be declared.
    // Plain path aliases use their resolved workspace owner's published name.
    // For `#imports` aliases preserve every logical package target as well:
    // physical resolution is provenance evidence, never replacement
    // authorization for the package.json mapping.
    if (!hasLogicalAliasTarget) packageNames.clear();
    packageNames.add(resolvedPackageName);
  }
  if (packageNames.size === 0) {
    return {
      code: "JAUNT_TS_PACKAGE_OWNER_MISSING",
      severity: "error",
      message: `Resolved workspace import ${JSON.stringify(specifier)} crosses package boundaries, but the target package.json has no name`,
    };
  }
  const production = new Set([
    ...dependencySet(manifest.value, "dependencies"),
    ...dependencySet(manifest.value, "peerDependencies"),
    ...dependencySet(manifest.value, "optionalDependencies"),
  ]);
  const allowed = testOnly
    ? new Set([
        ...production,
        ...dependencySet(manifest.value, "devDependencies"),
      ])
    : production;
  const ownPackageName = manifest.value.name;
  const undeclared = [...packageNames]
    .filter((packageName) => packageName !== ownPackageName)
    .filter((packageName) => !allowed.has(packageName))
    .sort();
  if (undeclared.length === 0) return undefined;
  return {
    code: "JAUNT_TS_UNDECLARED_PACKAGE",
    severity: "error",
    message: `${JSON.stringify(undeclared[0])} is not declared by ${manifest.path}; successful hoisted, workspace, package-import, or path-alias resolution is not authorization`,
  };
}
