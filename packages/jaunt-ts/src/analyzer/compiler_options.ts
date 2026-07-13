import { resolve } from "node:path";
import type ts from "@typescript/typescript6";
import { digestCanonical } from "./canonical.js";

interface PortableAbsolutePath {
  readonly flavor: "drive" | "posix" | "unc";
  readonly volume: string;
  readonly segments: readonly string[];
  readonly caseInsensitive: boolean;
}

const PATH_OPTION_NAMES = new Set([
  "baseurl",
  "configfilepath",
  "declarationdir",
  "generatecpuprofile",
  "generatetrace",
  "maproot",
  "outdir",
  "outfile",
  "path",
  "paths",
  "pathsbasepath",
  "project",
  "rootdir",
  "rootdirs",
  "sourceroot",
  "tsbuildinfofile",
  "typeroots",
]);

function normalizeSegments(
  parts: readonly string[],
  absolute: boolean,
): string[] {
  const output: string[] = [];
  for (const part of parts) {
    if (part === "" || part === ".") continue;
    if (part === "..") {
      if (output.length > 0 && output.at(-1) !== "..") {
        output.pop();
      } else if (!absolute) {
        output.push(part);
      }
      continue;
    }
    output.push(part);
  }
  return output;
}

function parsePortableAbsolutePath(
  value: string,
): PortableAbsolutePath | undefined {
  const normalized = value.replaceAll("\\", "/");
  const drive = /^([A-Za-z]):(?:\/(.*))?$/.exec(normalized);
  if (drive) {
    return {
      flavor: "drive",
      volume: drive[1]!.toLowerCase(),
      segments: normalizeSegments((drive[2] ?? "").split("/"), true),
      caseInsensitive: true,
    };
  }
  const unc = /^\/\/([^/]+)\/([^/]+)(?:\/(.*))?$/.exec(normalized);
  if (unc) {
    return {
      flavor: "unc",
      volume: `${unc[1]!.toLowerCase()}/${unc[2]!.toLowerCase()}`,
      segments: normalizeSegments((unc[3] ?? "").split("/"), true),
      caseInsensitive: true,
    };
  }
  if (normalized.startsWith("/")) {
    return {
      flavor: "posix",
      volume: "/",
      segments: normalizeSegments(normalized.split("/"), true),
      caseInsensitive: false,
    };
  }
  return undefined;
}

function fileUrlPath(value: string): string | undefined {
  if (!value.toLowerCase().startsWith("file://")) return undefined;
  try {
    const url = new URL(value);
    const pathname = decodeURIComponent(url.pathname);
    if (url.hostname) return `//${url.hostname}${pathname}`;
    return /^\/[A-Za-z]:\//.test(pathname) ? pathname.slice(1) : pathname;
  } catch {
    return undefined;
  }
}

function resolvePortablePath(
  root: PortableAbsolutePath,
  value: string,
): PortableAbsolutePath {
  const absolute = parsePortableAbsolutePath(value);
  if (absolute) return absolute;
  return {
    ...root,
    segments: normalizeSegments(
      [...root.segments, ...value.replaceAll("\\", "/").split("/")],
      true,
    ),
  };
}

function sameVolume(
  left: PortableAbsolutePath,
  right: PortableAbsolutePath,
): boolean {
  return left.flavor === right.flavor && left.volume === right.volume;
}

function relativePortablePath(
  from: PortableAbsolutePath,
  to: PortableAbsolutePath,
): string {
  const left = from.caseInsensitive
    ? from.segments.map((part) => part.toLowerCase())
    : from.segments;
  const right = to.caseInsensitive
    ? to.segments.map((part) => part.toLowerCase())
    : to.segments;
  let common = 0;
  while (
    common < left.length &&
    common < right.length &&
    left[common] === right[common]
  ) {
    common += 1;
  }
  const parts = [
    ...Array.from({ length: left.length - common }, () => ".."),
    ...right.slice(common),
  ];
  return parts.join("/") || ".";
}

function isWithinPortablePath(
  root: PortableAbsolutePath,
  value: PortableAbsolutePath,
): boolean {
  if (!sameVolume(root, value)) return false;
  const left = root.caseInsensitive
    ? root.segments.map((part) => part.toLowerCase())
    : root.segments;
  const right = value.caseInsensitive
    ? value.segments.map((part) => part.toLowerCase())
    : value.segments;
  return left.every((part, index) => right[index] === part);
}

function canonicalAbsolutePath(
  workspaceRoot: PortableAbsolutePath,
  value: PortableAbsolutePath,
): string {
  if (isWithinPortablePath(workspaceRoot, value)) {
    return `<workspace>/${relativePortablePath(workspaceRoot, value)}`;
  }
  // Volume names and checkout depth are installation-specific. An absolute
  // target outside the workspace keeps its path beneath the volume so changing
  // the target remains visible, but moving an otherwise identical checkout does
  // not change the hash.
  const segments = value.caseInsensitive
    ? value.segments.map((part) => part.toLowerCase())
    : value.segments;
  return `<external:${value.flavor}>/${segments.join("/") || "."}`;
}

function normalizeRelativePath(value: string): string {
  if (/^[A-Za-z][A-Za-z0-9+.-]*:/.test(value)) return value;
  const normalized = normalizeSegments(
    value.replaceAll("\\", "/").split("/"),
    false,
  ).join("/");
  return `<relative>/${normalized || "."}`;
}

function looksPathValued(key: string): boolean {
  const normalized = key.toLowerCase();
  return (
    PATH_OPTION_NAMES.has(normalized) ||
    /(?:file|path|paths|dir|directory|root|roots)$/.test(normalized)
  );
}

function canonicalizeValue(
  value: unknown,
  workspaceRoot: PortableAbsolutePath,
  pathValued: boolean,
): unknown {
  if (typeof value === "string") {
    const filePath = fileUrlPath(value);
    const absolute = parsePortableAbsolutePath(filePath ?? value);
    if (absolute) return canonicalAbsolutePath(workspaceRoot, absolute);
    return pathValued ? normalizeRelativePath(value) : value;
  }
  if (Array.isArray(value)) {
    return value.map((item) =>
      canonicalizeValue(item, workspaceRoot, pathValued),
    );
  }
  if (value !== null && typeof value === "object") {
    const object = value as Record<string, unknown>;
    return Object.fromEntries(
      Object.entries(object).map(([key, item]) => [
        key,
        canonicalizeValue(
          item,
          workspaceRoot,
          pathValued || looksPathValued(key),
        ),
      ]),
    );
  }
  return value;
}

/**
 * Remove checkout-specific absolute paths from TypeScript's parsed options.
 *
 * TypeScript resolves path-valued tsconfig entries before returning them and
 * also injects `configFilePath` and `pathsBasePath`. Those absolute values must
 * remain semantically significant without making an identical copied checkout
 * produce a different freshness fingerprint.
 */
export function canonicalCompilerOptions(
  workspaceRoot: string,
  configPath: string,
  options: ts.CompilerOptions,
): unknown {
  const root =
    parsePortableAbsolutePath(workspaceRoot) ??
    parsePortableAbsolutePath(resolve(workspaceRoot));
  if (!root) {
    throw new Error(`Workspace root must be absolute: ${workspaceRoot}`);
  }
  const config = resolvePortablePath(root, configPath);
  if (!isWithinPortablePath(root, config)) {
    throw new Error(
      `TypeScript config must be inside the workspace: ${configPath}`,
    );
  }
  return canonicalizeValue(options, root, false);
}

export function compilerOptionsHash(
  workspaceRoot: string,
  configPath: string,
  options: ts.CompilerOptions,
): string {
  return digestCanonical(
    canonicalCompilerOptions(workspaceRoot, configPath, options),
  );
}
