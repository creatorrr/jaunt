import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { isAbsolute, win32 } from "node:path";
import { npmCliInvocation } from "./npm-cli.mjs";

const packageJson = JSON.parse(
  readFileSync(new URL("../package.json", import.meta.url), "utf8"),
);
const { WORKER_VERSION } = await import("../dist/worker/session.js");
assert.equal(
  WORKER_VERSION,
  packageJson.version,
  "built worker version must match package.json",
);
const npm = npmCliInvocation();
const packed = JSON.parse(
  execFileSync(
    npm.command,
    [...npm.args, "pack", "--json", "--dry-run", "--ignore-scripts"],
    {
      cwd: new URL("..", import.meta.url),
      encoding: "utf8",
      env: { ...process.env, npm_config_loglevel: "silent" },
    },
  ),
)[0];

assert(
  packed && Array.isArray(packed.files),
  "npm pack did not return a file manifest",
);
const files = packed.files.map(({ path }) => path).sort();
const fileSet = new Set(files);
assert.deepEqual(packageJson.files, ["dist", "LICENSE", "README.md"]);
assert.equal(packageJson.license, "MIT");
assert.equal(
  packageJson.repository?.url,
  "git+https://github.com/creatorrr/jaunt.git",
  "trusted publishing requires the exact GitHub repository URL",
);
assert.equal(packageJson.repository?.directory, "packages/jaunt-ts");
assert.equal(packageJson.sideEffects, false);
assert.equal(packageJson.type, "module");
assert.equal(
  packageJson.dependencies,
  undefined,
  "worker must not bundle runtime dependencies",
);
const required = [
  "LICENSE",
  "README.md",
  "package.json",
  "dist/spec.js",
  "dist/spec.cjs",
  "dist/spec.d.cts",
  "dist/spec.d.ts",
  "dist/worker/main.js",
  "dist/worker/main.d.ts",
  "dist/test/runner.js",
  "dist/test/runner.d.ts",
  "dist/test/permission_guard.cjs",
  "dist/schema/protocol-v1.schema.json",
  "dist/schema/contract-ir-v1.schema.json",
];
for (const path of required)
  assert(fileSet.has(path), `npm tarball is missing ${path}`);

function exportedPaths(value) {
  if (typeof value === "string") return [value];
  if (!value || typeof value !== "object") return [];
  return Object.values(value).flatMap(exportedPaths);
}

for (const target of [
  ...exportedPaths(packageJson.exports),
  ...Object.values(packageJson.bin ?? {}),
]) {
  assert(target.startsWith("./"), `package target must be relative: ${target}`);
  assert(
    fileSet.has(target.slice(2)),
    `package target is absent from tarball: ${target}`,
  );
}

for (const path of files) {
  assert(
    path === "README.md" ||
      path === "LICENSE" ||
      path === "LICENSE.md" ||
      path === "package.json" ||
      path.startsWith("dist/"),
    `npm tarball contains non-allowlisted path ${path}`,
  );
  assert(
    !/\.jaunt(?:-test)?\.(?:ts|tsx)$/.test(path),
    `npm tarball contains raw spec ${path}`,
  );
  assert(
    !path.endsWith(".ts") || path.endsWith(".d.ts"),
    `npm tarball contains uncompiled TypeScript source ${path}`,
  );
  assert(
    !path.includes("/fixtures/"),
    `npm tarball contains a test fixture ${path}`,
  );
}

for (const path of files.filter((candidate) => candidate.endsWith(".map"))) {
  const map = JSON.parse(
    readFileSync(new URL(`../${path}`, import.meta.url), "utf8"),
  );
  const sourcePaths = [
    map.sourceRoot,
    ...(Array.isArray(map.sources) ? map.sources : []),
  ].filter((value) => typeof value === "string" && value.length > 0);
  for (const sourcePath of sourcePaths) {
    assert(
      !sourcePath.startsWith("file:") &&
        !isAbsolute(sourcePath) &&
        !win32.isAbsolute(sourcePath),
      `${path} contains absolute source-map metadata: ${sourcePath}`,
    );
  }
}

for (const lifecycle of [
  "preinstall",
  "install",
  "postinstall",
  "prepare",
  "prepublish",
  "prepublishOnly",
]) {
  assert(
    !(lifecycle in (packageJson.scripts ?? {})),
    `package must not define ${lifecycle}`,
  );
}

process.stdout.write(
  `verified npm tarball allowlist (${files.length} files)\n`,
);
