import assert from "node:assert/strict";
import { execFileSync, spawn } from "node:child_process";
import {
  access,
  mkdtemp,
  mkdir,
  readFile,
  rename,
  rm,
  symlink,
  writeFile,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, isAbsolute, join, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import { npmCliInvocation } from "./npm-cli.mjs";

const npm = npmCliInvocation();
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const compilerRoot = resolve(packageRoot, "node_modules/@typescript/old");
const compilerPackage = JSON.parse(
  await readFile(resolve(compilerRoot, "package.json"), "utf8"),
);
assert.equal(
  compilerPackage.name,
  "typescript",
  "clean-consumer compiler fixture must use the ordinary typescript package",
);
assert.match(compilerPackage.version, /^6\./);
const sandbox = await mkdtemp(join(tmpdir(), "jaunt-ts-pack-"));
let worker;

function absoluteFileDependency(path) {
  const specifier = pathToFileURL(resolve(path)).href;
  assert.equal(new URL(specifier).protocol, "file:");
  assert.equal(
    isAbsolute(fileURLToPath(specifier)),
    true,
    `dependency must be an absolute file URL: ${specifier}`,
  );
  return specifier;
}

function run(command, args, cwd, options = {}) {
  return execFileSync(command, args, {
    cwd,
    encoding: "utf8",
    ...(options.input === undefined ? {} : { input: options.input }),
    ...(options.timeout === undefined ? {} : { timeout: options.timeout }),
    env: {
      ...process.env,
      npm_config_audit: "false",
      npm_config_fund: "false",
      npm_config_loglevel: "error",
    },
  });
}

async function linkLocalPeer(project, name) {
  const source = resolve(packageRoot, "node_modules", name);
  const target = resolve(project, "node_modules", name);
  await access(source);
  try {
    await access(target);
  } catch {
    await symlink(
      source,
      target,
      process.platform === "win32" ? "junction" : "dir",
    );
  }
}

async function request(child, lines, id, method, params = {}) {
  child.stdin.write(
    `${JSON.stringify({
      protocol: "jaunt-ts/1-draft.3",
      id,
      method,
      params,
    })}\n`,
  );
  const next = await lines.next();
  assert.equal(
    next.done,
    false,
    `worker exited before responding to ${method}`,
  );
  const response = JSON.parse(next.value);
  assert.equal(response.id, id);
  assert.equal(response.ok, true, JSON.stringify(response.error));
  return response.result;
}

async function commitArtifacts(root, artifacts) {
  for (const artifact of artifacts) {
    const path = resolve(root, artifact.path);
    await mkdir(dirname(path), { recursive: true });
    await writeFile(path, artifact.content);
  }
}

try {
  const suppliedTarball = process.argv[2];
  let tarball;
  if (suppliedTarball) {
    tarball = resolve(suppliedTarball);
  } else {
    const packed = JSON.parse(
      run(
        npm.command,
        [
          ...npm.args,
          "pack",
          "--json",
          "--ignore-scripts",
          "--pack-destination",
          sandbox,
        ],
        packageRoot,
      ),
    )[0];
    assert.equal(typeof packed?.filename, "string");
    tarball = resolve(sandbox, packed.filename);
  }
  await access(tarball);
  const project = resolve(sandbox, "consumer");
  const tarballDependency = absoluteFileDependency(tarball);
  const compilerDependency = absoluteFileDependency(compilerRoot);
  await mkdir(resolve(project, "src"), { recursive: true });
  await mkdir(resolve(project, "tests"), { recursive: true });
  await writeFile(
    resolve(project, "package.json"),
    `${JSON.stringify(
      {
        private: true,
        type: "module",
        devDependencies: {
          "@usejaunt/ts": tarballDependency,
          typescript: compilerDependency,
        },
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    resolve(project, "tsconfig.json"),
    `${JSON.stringify(
      {
        compilerOptions: {
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          target: "ES2022",
          rootDir: "src",
          outDir: "dist",
          declaration: true,
          types: [],
        },
        include: ["src/**/*.ts"],
        exclude: ["src/**/*.jaunt.ts", "src/**/*.jaunt-test.ts"],
      },
      null,
      2,
    )}\n`,
  );
  await writeFile(
    resolve(project, "src/index.ts"),
    `import { slugify } from "./slug/index.js";
if (slugify(" Hello Packed Consumer ") !== "hello-packed-consumer") {
  throw new Error("installed generated implementation returned the wrong value");
}
`,
  );
  await writeFile(
    resolve(project, "worker-types.ts"),
    `import type { runWorker } from "@usejaunt/ts/worker";
type WorkerEntryPoint = typeof runWorker;
declare const workerEntryPoint: WorkerEntryPoint;
void workerEntryPoint;
`,
  );
  await writeFile(
    resolve(project, "tsconfig.worker-types.json"),
    `${JSON.stringify(
      {
        compilerOptions: {
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          target: "ES2022",
          types: [],
          noEmit: true,
        },
        files: ["worker-types.ts"],
      },
      null,
      2,
    )}\n`,
  );
  await mkdir(resolve(project, "src/slug"), { recursive: true });
  await writeFile(
    resolve(project, "src/slug/index.jaunt.ts"),
    `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** Trim, lowercase, and replace whitespace runs with one dash. */
export function slugify(value: string): string { return jaunt.magic(); }
`,
  );
  run(
    npm.command,
    [...npm.args, "install", "--ignore-scripts", "--legacy-peer-deps"],
    project,
  );
  // The mutation coordinator imports Vitest through its optional peer. Link the
  // already-installed development peer so this clean-consumer smoke stays
  // deterministic and never reaches the registry for test-only dependencies.
  await linkLocalPeer(project, "vitest");

  const installedPackage = JSON.parse(
    await readFile(
      resolve(project, "node_modules/@usejaunt/ts/package.json"),
      "utf8",
    ),
  );
  const installedCompiler = JSON.parse(
    await readFile(
      resolve(project, "node_modules/typescript/package.json"),
      "utf8",
    ),
  );
  assert.deepEqual(
    {
      name: installedCompiler.name,
      version: installedCompiler.version,
    },
    {
      name: "typescript",
      version: compilerPackage.version,
    },
    "clean consumer must install TypeScript 6 at node_modules/typescript",
  );

  run(
    process.execPath,
    [
      "--input-type=module",
      "--eval",
      'const m = await import("@usejaunt/ts/spec"); if (typeof m.magic !== "function") process.exit(1);',
    ],
    project,
  );
  run(
    process.execPath,
    [
      resolve(project, "node_modules/typescript/lib/tsc.js"),
      "-p",
      "tsconfig.worker-types.json",
    ],
    project,
  );
  run(
    process.execPath,
    [
      "--eval",
      'const m = require("@usejaunt/ts/spec"); if (typeof m.magic !== "function") process.exit(1);',
    ],
    project,
  );

  const workerPath = resolve(
    project,
    "node_modules/@usejaunt/ts/dist/worker/main.js",
  );
  const child = spawn(process.execPath, [workerPath], {
    cwd: project,
    stdio: ["pipe", "pipe", "pipe"],
  });
  worker = child;
  const exited = new Promise((accept, reject) => {
    child.once("error", reject);
    child.once("exit", accept);
  });
  const lines = createInterface({
    input: child.stdout,
    crlfDelay: Number.POSITIVE_INFINITY,
  })[Symbol.asyncIterator]();
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });

  const initialized = await request(child, lines, "1", "initialize", {
    root: project,
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: resolve(
      project,
      "node_modules/typescript/lib/typescript.js",
    ),
    clientVersion: "pack-smoke",
    toolVersion: "0.1.0-alpha.0",
  });
  assert.equal(initialized.protocol, "jaunt-ts/1-draft.3");
  assert.equal(
    initialized.workerVersion,
    installedPackage.version,
    "installed worker version must match its package metadata",
  );
  assert.match(initialized.typescriptVersion, /^(?:5\.[89]|6\.)/);
  const workspace = await request(child, lines, "2", "analyzeWorkspace");
  assert.equal(workspace.specs.length, 1);
  assert.equal(workspace.routes.length, 1);
  assert.equal(workspace.routes[0].moduleId, "ts:src/slug/index");
  const contracts = await request(child, lines, "3", "analyzeContracts");
  assert.equal(contracts.modules.length, 1);
  const moduleId = contracts.modules[0].moduleId;
  const synchronized = await request(child, lines, "4", "validateOverlay", {
    sessionId: initialized.sessionId,
    expectedEpoch: initialized.epoch,
    expectedSnapshot: initialized.snapshot,
    candidates: {},
    syncModuleIds: [moduleId],
  });
  assert.equal(
    synchronized.valid,
    true,
    JSON.stringify(synchronized.diagnostics),
  );
  await commitArtifacts(project, synchronized.artifacts);
  const invalidated = await request(child, lines, "5", "invalidate", {
    paths: synchronized.artifacts.map(({ path }) => path),
  });
  const generated = await request(child, lines, "6", "validateOverlay", {
    sessionId: invalidated.sessionId,
    expectedEpoch: invalidated.epoch,
    expectedSnapshot: invalidated.snapshot,
    candidates: {
      [moduleId]:
        'const __jaunt_impl_slugify = (value: string): string => value.trim().toLowerCase().replace(/\\s+/g, "-");',
    },
  });
  assert.equal(generated.valid, true, JSON.stringify(generated.diagnostics));
  await commitArtifacts(project, generated.artifacts);
  await request(child, lines, "7", "shutdown");
  child.stdin.end();
  const exitCode = await exited;
  assert.equal(exitCode, 0, stderr);
  assert.equal(stderr, "", "installed worker wrote unexpected stderr");

  run(
    process.execPath,
    [
      resolve(project, "node_modules/typescript/lib/tsc.js"),
      "-p",
      "tsconfig.json",
    ],
    project,
  );
  const emitted = [
    "dist/index.js",
    "dist/slug/index.js",
    "dist/slug/index.d.ts",
    "dist/slug/__generated__/index.js",
    "dist/slug/__generated__/index.d.ts",
    "dist/slug/__generated__/index.api.d.ts",
  ];
  for (const path of emitted) {
    const source = await readFile(resolve(project, path), "utf8");
    assert.doesNotMatch(source, /@usejaunt\/ts/);
    assert.doesNotMatch(source, /index\.jaunt/);
  }
  await access(resolve(project, "dist/slug/index.d.ts"));
  await assert.rejects(access(resolve(project, "dist/slug/index.jaunt.js")));
  await assert.rejects(access(resolve(project, "dist/slug/index.jaunt.d.ts")));

  const installedPackagePath = resolve(project, "node_modules/@usejaunt/ts");
  const mutationPath = resolve(installedPackagePath, "dist/test/mutation.js");
  const mutationReport = JSON.parse(
    run(process.execPath, [mutationPath], project, {
      input: JSON.stringify({
        root: project,
        sourcePath: "src/index.ts",
        symbol: "__jaunt_pack_smoke_missing_symbol__",
        batteryFiles: [],
        overlays: {},
        tsconfigPath: "tsconfig.json",
        compilerModulePath: resolve(
          project,
          "node_modules/typescript/lib/typescript.js",
        ),
        timeoutMs: 1_000,
        globalTimeoutMs: 3_000,
        maxMutants: 1,
      }),
      timeout: 10_000,
    }),
  );
  assert.deepEqual(
    {
      protocol: mutationReport.protocol,
      sourcePath: mutationReport.sourcePath,
      symbol: mutationReport.symbol,
      concurrency: mutationReport.concurrency,
      complete: mutationReport.complete,
      killed: mutationReport.killed,
      survived: mutationReport.survived,
      excluded: mutationReport.excluded?.map(({ outcome, reason }) => ({
        outcome,
        reason,
      })),
      score: mutationReport.score,
    },
    {
      protocol: "jaunt-ts-mutation/1",
      sourcePath: "src/index.ts",
      symbol: "__jaunt_pack_smoke_missing_symbol__",
      concurrency: 1,
      complete: true,
      killed: [],
      survived: [],
      excluded: [{ outcome: "excluded", reason: "no-mutable-site" }],
      score: {
        killed: 0,
        applicable: 0,
        survived: 0,
        excluded: 1,
        ratio: null,
      },
    },
    "installed mutation coordinator must return its deterministic protocol",
  );

  const permissionFlag = process.allowedNodeEnvironmentFlags.has("--permission")
    ? "--permission"
    : process.allowedNodeEnvironmentFlags.has("--experimental-permission")
      ? "--experimental-permission"
      : null;
  assert.notEqual(
    permissionFlag,
    null,
    "supported Node versions must expose the permission model",
  );
  const permissionGuardPath = resolve(
    installedPackagePath,
    "dist/test/permission_guard.cjs",
  );
  const workerProbe = `
const { parentPort } = require("node:worker_threads");
parentPort.postMessage({
  installed:
    globalThis[Symbol.for("@usejaunt/ts/permission-guard-installed")] === true,
  nodeOptions: process.env.NODE_OPTIONS ?? "",
});
`;
  const permissionProbe = `
const { Worker } = require("node:worker_threads");
if (!globalThis[Symbol.for("@usejaunt/ts/permission-guard-installed")]) {
  throw new Error("permission guard was not loaded in the coordinator");
}
const worker = new Worker(${JSON.stringify(workerProbe)}, {
  eval: true,
  env: { ...process.env, NODE_OPTIONS: "--inspect=127.0.0.1:0" },
});
worker.once("message", (message) => {
  if (!message.installed || message.nodeOptions.includes("--inspect")) {
    throw new Error(\`permission guard did not sanitize the worker: \${JSON.stringify(message)}\`);
  }
  process.stdout.write("permission-guard-ok\\n");
});
worker.once("error", (error) => {
  throw error;
});
`;
  const permissionOutput = run(
    process.execPath,
    [
      permissionFlag,
      "--allow-worker",
      `--allow-fs-read=${installedPackagePath}`,
      `--require=${permissionGuardPath}`,
      "--eval",
      permissionProbe,
    ],
    project,
    { timeout: 10_000 },
  );
  assert.equal(permissionOutput, "permission-guard-ok\n");

  await rename(installedPackagePath, `${installedPackagePath}.disabled`);
  run(process.execPath, [resolve(project, "dist/index.js")], project);

  process.stdout.write(
    "verified clean npm-tarball install, generated consumer, internal runners, and runtime isolation\n",
  );
} finally {
  worker?.stdin.end();
  worker?.kill();
  await rm(sandbox, { recursive: true, force: true });
}
