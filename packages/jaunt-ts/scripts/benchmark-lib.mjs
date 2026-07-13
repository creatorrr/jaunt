import { existsSync, readFileSync, readdirSync, readlinkSync } from "node:fs";

export const WATCH_CYCLES = 100;

export const LEAK_BUDGETS = Object.freeze({
  cycles: WATCH_CYCLES,
  maxPeakRssBytes: 768 * 1024 * 1024,
  maxRssDeltaBytes: 64 * 1024 * 1024,
  maxRssSlopeBytesPerCycle: 256 * 1024,
  maxFileDescriptorDelta: 2,
  maxFileDescriptorPeakGrowth: 4,
  maxActiveResourceDelta: 0,
  maxActiveResourcePeakGrowth: 0,
  maxListenerDelta: 0,
  maxListenerPeakGrowth: 0,
  maxChildProcessPeak: 0,
  maxSurvivingProcesses: 0,
  maxWorkerRestarts: 0,
});

// Rounded first-alpha measurements from the pinned Node 24 / TypeScript 6,
// 1,000-file, 100-edit lane. The maxima are exactly 20% above those baselines;
// the tiny cold phases use rounded noise floors so scheduler jitter does not
// turn sub-millisecond differences into meaningless percentage failures.
export const PERFORMANCE_BASELINES_MS = Object.freeze({
  workerHandshake: 900,
  coldDiscovery: 5,
  coldContractAnalysis: 5,
  combinedOverlayValidation: 2100,
  oneFileWarmAnalysis: 210,
  incrementalEditP50: 230,
  incrementalEditP95: 260,
  vitestStartup: 420,
});

export const PERFORMANCE_BUDGETS = Object.freeze({
  regressionPercent: 20,
  maxWorkerHandshakeMs: 1080,
  maxColdDiscoveryMs: 6,
  maxColdContractAnalysisMs: 6,
  maxCombinedOverlayValidationMs: 2520,
  maxOneFileWarmAnalysisMs: 252,
  maxIncrementalEditP50Ms: 276,
  maxIncrementalEditP95Ms: 312,
  maxVitestStartupMs: 504,
});

export function percentile(values, quantile) {
  if (values.length === 0) throw new Error("percentile requires a sample");
  if (quantile < 0 || quantile > 1) {
    throw new Error("quantile must be between 0 and 1");
  }
  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.max(0, Math.ceil(quantile * sorted.length) - 1);
  return sorted[index];
}

export function linearRegressionSlope(values) {
  if (values.length < 2) return 0;
  const xMean = (values.length - 1) / 2;
  const yMean =
    values.reduce((total, value) => total + value, 0) / values.length;
  let numerator = 0;
  let denominator = 0;
  for (let index = 0; index < values.length; index += 1) {
    const xDelta = index - xMean;
    numerator += xDelta * (values[index] - yMean);
    denominator += xDelta * xDelta;
  }
  return denominator === 0 ? 0 : numerator / denominator;
}

function readText(path) {
  try {
    return readFileSync(path, "utf8");
  } catch {
    return "";
  }
}

function socketInodes(pid) {
  const inodes = new Set();
  let descriptors = [];
  try {
    descriptors = readdirSync(`/proc/${pid}/fd`);
  } catch {
    return inodes;
  }
  for (const descriptor of descriptors) {
    try {
      const target = readlinkSync(`/proc/${pid}/fd/${descriptor}`);
      const match = /^socket:\[(\d+)]$/.exec(target);
      if (match) inodes.add(match[1]);
    } catch {
      // A descriptor can close between readdir and readlink.
    }
  }
  return inodes;
}

function listeningSocketCount(pid) {
  const owned = socketInodes(pid);
  if (owned.size === 0) return 0;
  const listening = new Set();
  for (const table of ["tcp", "tcp6"]) {
    const lines = readText(`/proc/${pid}/net/${table}`)
      .trim()
      .split("\n")
      .slice(1);
    for (const line of lines) {
      const fields = line.trim().split(/\s+/);
      if (fields[3] === "0A" && owned.has(fields[9])) listening.add(fields[9]);
    }
  }
  const unixLines = readText(`/proc/${pid}/net/unix`)
    .trim()
    .split("\n")
    .slice(1);
  for (const line of unixLines) {
    const fields = line.trim().split(/\s+/);
    const inode = fields[6];
    const flags = fields[3];
    if (inode && flags === "00010000" && owned.has(inode)) listening.add(inode);
  }
  return listening.size;
}

export function descendantPids(rootPid) {
  const found = new Set();
  const queue = [rootPid];
  while (queue.length > 0) {
    const pid = queue.shift();
    const children = readText(`/proc/${pid}/task/${pid}/children`)
      .trim()
      .split(/\s+/)
      .filter(Boolean)
      .map(Number)
      .filter(Number.isInteger);
    for (const child of children) {
      if (found.has(child)) continue;
      found.add(child);
      queue.push(child);
    }
  }
  return found;
}

export function linuxProcessSnapshot(pid) {
  const status = readText(`/proc/${pid}/status`);
  if (status === "") throw new Error(`worker process ${pid} is unavailable`);
  const rssMatch = /^VmRSS:\s+(\d+)\s+kB$/m.exec(status);
  if (!rssMatch) throw new Error(`worker process ${pid} has no VmRSS metric`);
  const peakRssMatch = /^VmHWM:\s+(\d+)\s+kB$/m.exec(status);
  if (!peakRssMatch) {
    throw new Error(`worker process ${pid} has no VmHWM metric`);
  }
  const fileDescriptors = readdirSync(`/proc/${pid}/fd`).length;
  const descendants = descendantPids(pid);
  return {
    rssBytes: Number(rssMatch[1]) * 1024,
    peakRssBytes: Number(peakRssMatch[1]) * 1024,
    fileDescriptors,
    listeners: listeningSocketCount(pid),
    childProcesses: descendants.size,
    descendantPids: [...descendants].sort((left, right) => left - right),
  };
}

export function processExists(pid) {
  return existsSync(`/proc/${pid}`);
}

function assertion(metric, actual, limit, comparison = "max") {
  const passed = comparison === "exact" ? actual === limit : actual <= limit;
  return { metric, actual, limit, comparison, passed };
}

export function evaluateLeakBudgets(metrics, budgets = LEAK_BUDGETS) {
  if (metrics.cycles !== budgets.cycles) {
    throw new Error(
      `Leak budgets require exactly ${budgets.cycles} cycles; received ${metrics.cycles}`,
    );
  }
  const assertions = [
    assertion("peakRssBytes", metrics.peakRssBytes, budgets.maxPeakRssBytes),
    assertion("rssDeltaBytes", metrics.rssDeltaBytes, budgets.maxRssDeltaBytes),
    assertion(
      "rssSlopeBytesPerCycle",
      metrics.rssSlopeBytesPerCycle,
      budgets.maxRssSlopeBytesPerCycle,
    ),
    assertion(
      "fileDescriptorDelta",
      metrics.fileDescriptorDelta,
      budgets.maxFileDescriptorDelta,
    ),
    assertion(
      "fileDescriptorPeakGrowth",
      metrics.fileDescriptorPeakGrowth,
      budgets.maxFileDescriptorPeakGrowth,
    ),
    assertion(
      "activeResourceDelta",
      metrics.activeResourceDelta,
      budgets.maxActiveResourceDelta,
    ),
    assertion(
      "activeResourcePeakGrowth",
      metrics.activeResourcePeakGrowth,
      budgets.maxActiveResourcePeakGrowth,
    ),
    assertion("listenerDelta", metrics.listenerDelta, budgets.maxListenerDelta),
    assertion(
      "listenerPeakGrowth",
      metrics.listenerPeakGrowth,
      budgets.maxListenerPeakGrowth,
    ),
    assertion(
      "childProcessPeak",
      metrics.childProcessPeak,
      budgets.maxChildProcessPeak,
    ),
    assertion(
      "survivingProcesses",
      metrics.survivingProcesses,
      budgets.maxSurvivingProcesses,
    ),
    assertion(
      "workerRestarts",
      metrics.workerRestarts,
      budgets.maxWorkerRestarts,
      "exact",
    ),
  ];
  return { assertions, passed: assertions.every((item) => item.passed) };
}

export function evaluatePerformanceBudgets(
  timings,
  budgets = PERFORMANCE_BUDGETS,
) {
  const assertions = [
    assertion(
      "workerHandshakeMs",
      timings.workerHandshake,
      budgets.maxWorkerHandshakeMs,
    ),
    assertion(
      "coldDiscoveryMs",
      timings.coldDiscovery,
      budgets.maxColdDiscoveryMs,
    ),
    assertion(
      "coldContractAnalysisMs",
      timings.coldContractAnalysis,
      budgets.maxColdContractAnalysisMs,
    ),
    assertion(
      "combinedOverlayValidationMs",
      timings.combinedOverlayValidation,
      budgets.maxCombinedOverlayValidationMs,
    ),
    assertion(
      "oneFileWarmAnalysisMs",
      timings.oneFileWarmAnalysis,
      budgets.maxOneFileWarmAnalysisMs,
    ),
    assertion(
      "incrementalEditP50Ms",
      timings.incrementalEdits.p50,
      budgets.maxIncrementalEditP50Ms,
    ),
    assertion(
      "incrementalEditP95Ms",
      timings.incrementalEdits.p95,
      budgets.maxIncrementalEditP95Ms,
    ),
    assertion(
      "vitestStartupMs",
      timings.vitestStartup,
      budgets.maxVitestStartupMs,
    ),
  ];
  return { assertions, passed: assertions.every((item) => item.passed) };
}
