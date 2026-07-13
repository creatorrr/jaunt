#!/usr/bin/env node
import { spawn } from "node:child_process";
import { createRequire } from "node:module";
import {
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { createInterface } from "node:readline";
import { fileURLToPath } from "node:url";
import { performance } from "node:perf_hooks";
import {
  LEAK_BUDGETS,
  PERFORMANCE_BASELINES_MS,
  PERFORMANCE_BUDGETS,
  WATCH_CYCLES,
  evaluateLeakBudgets,
  evaluatePerformanceBudgets,
  linearRegressionSlope,
  linuxProcessSnapshot,
  percentile,
  processExists,
} from "./benchmark-lib.mjs";

const require = createRequire(import.meta.url);
const packageRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const DEFAULT_GRAPH_FILES = 1000;
const REQUEST_TIMEOUT_MS = 30_000;
const PROTOCOL = "jaunt-ts/1-draft.2";

function parsePositiveInteger(raw, option) {
  const value = Number.parseInt(raw, 10);
  if (!Number.isInteger(value) || value <= 0) {
    throw new Error(`${option} requires a positive integer`);
  }
  return value;
}

export function parseArguments(argv) {
  const options = {
    assertBudgets: false,
    cycles: WATCH_CYCLES,
    graphFiles: DEFAULT_GRAPH_FILES,
    output: undefined,
  };
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === "--assert") {
      options.assertBudgets = true;
    } else if (argument === "--no-assert") {
      options.assertBudgets = false;
    } else if (argument === "--cycles") {
      options.cycles = parsePositiveInteger(argv[++index], "--cycles");
    } else if (argument === "--graph-files") {
      options.graphFiles = parsePositiveInteger(argv[++index], "--graph-files");
    } else if (argument === "--output") {
      const output = argv[++index];
      if (!output) throw new Error("--output requires a path");
      options.output = resolve(output);
    } else {
      throw new Error(`Unknown benchmark option: ${argument}`);
    }
  }
  if (options.assertBudgets && options.cycles !== WATCH_CYCLES) {
    throw new Error(
      `--assert requires the pinned ${WATCH_CYCLES}-cycle sample`,
    );
  }
  if (options.assertBudgets && options.graphFiles !== DEFAULT_GRAPH_FILES) {
    throw new Error(
      `--assert requires the pinned ${DEFAULT_GRAPH_FILES}-file project graph`,
    );
  }
  if (options.assertBudgets && process.platform !== "linux") {
    throw new Error("--assert requires Linux /proc resource metrics");
  }
  return options;
}

function write(root, path, content) {
  const target = resolve(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

function editSource(edit) {
  return `import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Return the supplied value unchanged. Benchmark edit ${String(edit).padStart(3, "0")}. */
export function benchmarkValue(value: string): string {
  return jaunt.magic();
}
`;
}

function createBenchmarkWorkspace(graphFiles) {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-ts-watch-benchmark-"));
  const compilerRoot = dirname(
    require.resolve("@typescript/typescript6/package.json"),
  );
  const vitestRoot = dirname(require.resolve("vitest/package.json"));
  mkdirSync(resolve(root, "node_modules/@usejaunt"), { recursive: true });
  symlinkSync(packageRoot, resolve(root, "node_modules/@usejaunt/ts"), "dir");
  symlinkSync(compilerRoot, resolve(root, "node_modules/typescript"), "dir");
  symlinkSync(vitestRoot, resolve(root, "node_modules/vitest"), "dir");

  write(
    root,
    "package.json",
    `${JSON.stringify(
      {
        name: "jaunt-ts-watch-benchmark",
        private: true,
        type: "module",
        devDependencies: {
          "@usejaunt/ts": "0.1.0-alpha.0",
          typescript: "6.0.2",
          vitest: "4.1.10",
        },
      },
      null,
      2,
    )}\n`,
  );
  write(
    root,
    "tsconfig.json",
    `${JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          exactOptionalPropertyTypes: true,
          types: [],
        },
        include: ["src/**/*.ts"],
        exclude: ["src/**/*.jaunt.ts", "src/**/__generated__/**"],
      },
      null,
      2,
    )}\n`,
  );
  for (let index = 0; index < graphFiles - 1; index += 1) {
    const id = String(index).padStart(4, "0");
    write(
      root,
      `src/graph/node${id}.ts`,
      `export interface GraphNode${id} { readonly id: ${index}; }\n`,
    );
  }
  write(
    root,
    "src/app.ts",
    'import type { GraphNode0000 } from "./graph/node0000.js";\n' +
      "export const rootNode: GraphNode0000 = { id: 0 };\n",
  );
  write(root, "src/benchmark/index.jaunt.ts", editSource(0));
  write(
    root,
    "benchmark/startup.test.ts",
    'import { expect, test } from "vitest";\n' +
      'test("starts", () => expect(1 + 1).toBe(2));\n',
  );
  write(
    root,
    "vitest.config.mjs",
    `export default {
  test: {
    include: ["benchmark/**/*.test.ts"],
    maxWorkers: 1,
    minWorkers: 1,
  },
};
`,
  );
  return {
    root,
    compilerModulePath: resolve(
      root,
      "node_modules/typescript/lib/typescript.js",
    ),
  };
}

function withTimeout(promise, label) {
  let timer;
  return Promise.race([
    promise,
    new Promise((_, reject) => {
      timer = setTimeout(
        () => reject(new Error(`${label} exceeded ${REQUEST_TIMEOUT_MS} ms`)),
        REQUEST_TIMEOUT_MS,
      );
    }),
  ]).finally(() => clearTimeout(timer));
}

function startWorker(workspace) {
  const workerPath = resolve(packageRoot, "scripts/benchmark-worker.mjs");
  const child = spawn(process.execPath, ["--expose-gc", workerPath], {
    cwd: workspace.root,
    env: { ...process.env, NODE_OPTIONS: "" },
    stdio: ["pipe", "pipe", "pipe", "ipc"],
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    if (stderr.length < 64 * 1024) stderr += chunk;
  });
  const lines = createInterface({
    input: child.stdout,
    crlfDelay: Number.POSITIVE_INFINITY,
  })[Symbol.asyncIterator]();
  const exit = new Promise((accept, reject) => {
    child.once("error", reject);
    child.once("exit", (code, signal) => accept({ code, signal }));
  });
  let nextId = 0;
  let nextGarbageCollectionId = 0;
  const garbageCollections = new Map();
  child.on("message", (message) => {
    if (
      !message ||
      typeof message !== "object" ||
      typeof message.id !== "number"
    ) {
      return;
    }
    const pending = garbageCollections.get(message.id);
    if (!pending) return;
    garbageCollections.delete(message.id);
    if (message.error) pending.reject(new Error(message.error));
    else pending.accept(message);
  });

  async function request(method, params) {
    const id = `benchmark-${++nextId}`;
    const message = JSON.stringify({
      protocol: PROTOCOL,
      id,
      method,
      params,
      deadlineMs: REQUEST_TIMEOUT_MS,
    });
    if (!child.stdin.write(`${message}\n`)) {
      await withTimeout(
        new Promise((accept) => child.stdin.once("drain", accept)),
        `${method} stdin drain`,
      );
    }
    const record = await withTimeout(lines.next(), method);
    if (record.done) {
      const status = await exit;
      throw new Error(
        `worker exited during ${method}: ${JSON.stringify(status)} ${stderr}`,
      );
    }
    let response;
    try {
      response = JSON.parse(record.value);
    } catch {
      throw new Error(`worker emitted non-JSON stdout during ${method}`);
    }
    if (response.id !== id || response.protocol !== PROTOCOL) {
      throw new Error(`worker returned a mismatched response during ${method}`);
    }
    if (!response.ok) {
      throw new Error(`${method} failed: ${JSON.stringify(response.error)}`);
    }
    return response.result;
  }

  async function collectGarbage() {
    const id = ++nextGarbageCollectionId;
    const response = new Promise((accept, reject) => {
      garbageCollections.set(id, { accept, reject });
    });
    child.send({ id, method: "collectGarbage" });
    return withTimeout(response, "worker garbage collection");
  }
  return { child, collectGarbage, exit, request, stderr: () => stderr };
}

async function timed(operation) {
  const started = performance.now();
  const result = await operation();
  return { durationMs: performance.now() - started, result };
}

function summarize(values) {
  return {
    samples: values.length,
    min: Math.min(...values),
    p50: percentile(values, 0.5),
    p95: percentile(values, 0.95),
    max: Math.max(...values),
  };
}

async function runCommand(command, arguments_, options) {
  const started = performance.now();
  const child = spawn(command, arguments_, {
    ...options,
    stdio: ["ignore", "pipe", "pipe"],
  });
  const stdout = [];
  const stderr = [];
  child.stdout.on("data", (chunk) => stdout.push(chunk));
  child.stderr.on("data", (chunk) => stderr.push(chunk));
  const status = await withTimeout(
    new Promise((accept, reject) => {
      child.once("error", reject);
      child.once("exit", (code, signal) => accept({ code, signal }));
    }),
    "Vitest startup",
  );
  if (status.code !== 0) {
    throw new Error(
      `Vitest startup failed: ${JSON.stringify(status)}\n${Buffer.concat(stderr).toString()}\n${Buffer.concat(stdout).toString()}`,
    );
  }
  return performance.now() - started;
}

function rounded(value) {
  return Math.round(value * 1000) / 1000;
}

function roundTimings(value) {
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => [
      key,
      typeof item === "number"
        ? rounded(item)
        : Object.fromEntries(
            Object.entries(item).map(([childKey, childValue]) => [
              childKey,
              typeof childValue === "number" ? rounded(childValue) : childValue,
            ]),
          ),
    ]),
  );
}

export async function runBenchmark(options) {
  const workspace = createBenchmarkWorkspace(options.graphFiles);
  const worker = startWorker(workspace);
  const observedPids = new Set([worker.child.pid]);
  const resourceSamples = [];
  let workerClosed = false;
  try {
    const packageMetadata = JSON.parse(
      readFileSync(resolve(packageRoot, "package.json"), "utf8"),
    );
    const handshake = await timed(() =>
      worker.request("initialize", {
        root: workspace.root,
        projects: ["tsconfig.json"],
        testProjects: [],
        sourceRoots: ["src"],
        testRoots: ["benchmark"],
        generatedDir: "__generated__",
        toolOwner: ".",
        compilerModulePath: workspace.compilerModulePath,
        clientVersion: "benchmark",
        toolVersion: packageMetadata.version,
        generationFingerprint: "watch-benchmark-v1",
      }),
    );
    const discovery = await timed(() => worker.request("analyzeWorkspace", {}));
    if (discovery.result.routes.length !== 1) {
      throw new Error(
        "benchmark fixture must discover exactly one Jaunt module",
      );
    }
    const contracts = await timed(() => worker.request("analyzeContracts", {}));
    const moduleId = contracts.result.modules[0]?.moduleId;
    if (!moduleId)
      throw new Error("benchmark fixture returned no contract module");
    const overlay = await timed(() =>
      worker.request("validateOverlay", {
        sessionId: contracts.result.sessionId,
        expectedEpoch: contracts.result.epoch,
        expectedSnapshot: contracts.result.snapshot,
        candidates: {
          [moduleId]:
            "const __jaunt_impl_benchmarkValue = (value: string): string => value;",
        },
        moduleIds: [moduleId],
      }),
    );
    if (!overlay.result.valid) {
      throw new Error(
        `benchmark overlay did not validate: ${JSON.stringify(overlay.result.diagnostics)}`,
      );
    }

    write(workspace.root, "src/benchmark/index.jaunt.ts", editSource(1));
    const warmAnalysis = await timed(async () => {
      await worker.request("invalidate", {
        paths: ["src/benchmark/index.jaunt.ts"],
      });
      return worker.request("analyzeContracts", { moduleIds: [moduleId] });
    });
    const baselineGc = await worker.collectGarbage();
    const baseline = {
      ...linuxProcessSnapshot(worker.child.pid),
      heapUsedBytes: baselineGc.memoryUsage.heapUsed,
      activeResources: baselineGc.activeResources,
    };
    resourceSamples.push(baseline);
    for (const pid of baseline.descendantPids) observedPids.add(pid);

    const cycleDurations = [];
    for (let cycle = 0; cycle < options.cycles; cycle += 1) {
      const edit = cycle + 2;
      const cycleResult = await timed(async () => {
        write(workspace.root, "src/benchmark/index.jaunt.ts", editSource(edit));
        const invalidated = await worker.request("invalidate", {
          paths: ["src/benchmark/index.jaunt.ts"],
        });
        const analyzed = await worker.request("analyzeContracts", {
          moduleIds: [moduleId],
        });
        if (analyzed.epoch !== invalidated.epoch) {
          throw new Error(`cycle ${cycle + 1} analyzed a stale worker epoch`);
        }
        if (
          !analyzed.modules[0]?.specSource.includes(
            `Benchmark edit ${String(edit).padStart(3, "0")}`,
          )
        ) {
          throw new Error(`cycle ${cycle + 1} reused stale spec source`);
        }
      });
      cycleDurations.push(cycleResult.durationMs);
      const garbageCollection = await worker.collectGarbage();
      const sample = {
        ...linuxProcessSnapshot(worker.child.pid),
        heapUsedBytes: garbageCollection.memoryUsage.heapUsed,
        activeResources: garbageCollection.activeResources,
      };
      resourceSamples.push(sample);
      for (const pid of sample.descendantPids) observedPids.add(pid);
    }

    const finalSample = resourceSamples.at(-1);
    const rssValues = resourceSamples.map((sample) => sample.rssBytes);
    const peakRssValues = resourceSamples.map((sample) => sample.peakRssBytes);
    const heapUsedValues = resourceSamples.map(
      (sample) => sample.heapUsedBytes,
    );
    const fileDescriptorValues = resourceSamples.map(
      (sample) => sample.fileDescriptors,
    );
    const listenerValues = resourceSamples.map((sample) => sample.listeners);
    const childProcessValues = resourceSamples.map(
      (sample) => sample.childProcesses,
    );
    const activeResourceValues = resourceSamples.map(
      (sample) => sample.activeResources.length,
    );
    const rssSlopeWindow = rssValues.slice(-Math.min(80, rssValues.length));

    await worker.request("shutdown", {});
    worker.child.stdin.end();
    const exitStatus = await withTimeout(worker.exit, "worker shutdown");
    workerClosed = true;
    if (exitStatus.code !== 0) {
      throw new Error(
        `worker shutdown failed: ${JSON.stringify(exitStatus)} ${worker.stderr()}`,
      );
    }
    await new Promise((accept) => setTimeout(accept, 25));
    const survivingProcesses = [...observedPids].filter(processExists).length;
    const vitestStartupMs = await runCommand(
      process.execPath,
      [
        resolve(packageRoot, "node_modules/vitest/vitest.mjs"),
        "run",
        "--root",
        workspace.root,
        "--config",
        resolve(workspace.root, "vitest.config.mjs"),
      ],
      { cwd: workspace.root },
    );

    const metrics = {
      cycles: options.cycles,
      peakRssBytes: Math.max(...peakRssValues),
      retainedRssPeakBytes: Math.max(...rssValues),
      rssBaselineBytes: baseline.rssBytes,
      rssFinalBytes: finalSample.rssBytes,
      rssDeltaBytes: finalSample.rssBytes - baseline.rssBytes,
      rssSlopeWindowCycles: rssSlopeWindow.length - 1,
      rssSlopeBytesPerCycle: rounded(linearRegressionSlope(rssSlopeWindow)),
      heapUsedBaselineBytes: baseline.heapUsedBytes,
      heapUsedPeakBytes: Math.max(...heapUsedValues),
      heapUsedFinalBytes: finalSample.heapUsedBytes,
      heapUsedDeltaBytes: finalSample.heapUsedBytes - baseline.heapUsedBytes,
      activeResourceBaseline: baseline.activeResources,
      activeResourceFinal: finalSample.activeResources,
      activeResourceBaselineCount: baseline.activeResources.length,
      activeResourcePeakCount: Math.max(...activeResourceValues),
      activeResourceFinalCount: finalSample.activeResources.length,
      activeResourceDelta:
        finalSample.activeResources.length - baseline.activeResources.length,
      activeResourcePeakGrowth:
        Math.max(...activeResourceValues) - baseline.activeResources.length,
      fileDescriptorBaseline: baseline.fileDescriptors,
      fileDescriptorPeak: Math.max(...fileDescriptorValues),
      fileDescriptorFinal: finalSample.fileDescriptors,
      fileDescriptorDelta:
        finalSample.fileDescriptors - baseline.fileDescriptors,
      fileDescriptorPeakGrowth:
        Math.max(...fileDescriptorValues) - baseline.fileDescriptors,
      listenerBaseline: baseline.listeners,
      listenerPeak: Math.max(...listenerValues),
      listenerFinal: finalSample.listeners,
      listenerDelta: finalSample.listeners - baseline.listeners,
      listenerPeakGrowth: Math.max(...listenerValues) - baseline.listeners,
      childProcessPeak: Math.max(...childProcessValues),
      childProcessFinal: finalSample.childProcesses,
      workerProcessesStarted: 1,
      workerRestarts: 0,
      survivingProcesses,
    };
    const leakEvaluation =
      options.assertBudgets || options.cycles === WATCH_CYCLES
        ? evaluateLeakBudgets(metrics)
        : { assertions: [], passed: true };
    const timingsMs = roundTimings({
      workerHandshake: handshake.durationMs,
      coldDiscovery: discovery.durationMs,
      coldContractAnalysis: contracts.durationMs,
      combinedOverlayValidation: overlay.durationMs,
      oneFileWarmAnalysis: warmAnalysis.durationMs,
      incrementalEdits: summarize(cycleDurations),
      vitestStartup: vitestStartupMs,
    });
    const performanceEvaluation = options.assertBudgets
      ? evaluatePerformanceBudgets(timingsMs)
      : { assertions: [], passed: true };
    const assertions = [
      ...leakEvaluation.assertions,
      ...performanceEvaluation.assertions,
    ];
    return {
      schemaVersion: "jaunt-ts-benchmark/1",
      benchmark: "incremental-watch",
      environment: {
        platform: process.platform,
        arch: process.arch,
        nodeVersion: process.version,
        typescriptVersion: handshake.result.typescriptVersion,
        workerVersion: handshake.result.workerVersion,
        protocol: handshake.result.protocol,
        projectGraphFiles: options.graphFiles,
        cycles: options.cycles,
      },
      timingPolicy:
        "The pinned Node 24 / TypeScript 6 lane blocks timing regressions above 20% from the rounded first-alpha baseline",
      timingsMs,
      resources: metrics,
      baselines: { performanceMs: PERFORMANCE_BASELINES_MS },
      budgets: {
        leak: LEAK_BUDGETS,
        performanceMs: PERFORMANCE_BUDGETS,
      },
      assertions,
      enforced: options.assertBudgets,
      passed: leakEvaluation.passed && performanceEvaluation.passed,
    };
  } finally {
    if (
      !workerClosed &&
      worker.child.exitCode === null &&
      worker.child.signalCode === null
    ) {
      worker.child.kill("SIGKILL");
    }
    rmSync(workspace.root, { recursive: true, force: true });
  }
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  const result = await runBenchmark(options);
  const output = `${JSON.stringify(result, null, 2)}\n`;
  if (options.output) {
    mkdirSync(dirname(options.output), { recursive: true });
    writeFileSync(options.output, output);
  }
  process.stdout.write(output);
  if (result.enforced && !result.passed) {
    const failed = result.assertions
      .filter((item) => !item.passed)
      .map(
        (item) =>
          `${item.metric}: ${item.actual} (${item.comparison} ${item.limit})`,
      )
      .join(", ");
    throw new Error(`TypeScript watch benchmark budget failed: ${failed}`);
  }
}

const invokedPath = process.argv[1] ? resolve(process.argv[1]) : "";
if (invokedPath === fileURLToPath(import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(
      `${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`,
    );
    process.exitCode = 1;
  });
}
