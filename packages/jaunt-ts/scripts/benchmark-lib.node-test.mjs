import assert from "node:assert/strict";
import test from "node:test";
import {
  LEAK_BUDGETS,
  PERFORMANCE_BUDGETS,
  evaluateLeakBudgets,
  evaluatePerformanceBudgets,
  linearRegressionSlope,
  percentile,
} from "./benchmark-lib.mjs";

test("percentile uses the nearest-rank definition", () => {
  assert.equal(percentile([5, 1, 4, 2, 3], 0.5), 3);
  assert.equal(percentile([5, 1, 4, 2, 3], 0.95), 5);
});

test("linearRegressionSlope reports growth per sample", () => {
  assert.equal(linearRegressionSlope([10, 20, 30, 40]), 10);
  assert.equal(linearRegressionSlope([7]), 0);
});

function passingMetrics() {
  return {
    cycles: LEAK_BUDGETS.cycles,
    peakRssBytes: LEAK_BUDGETS.maxPeakRssBytes,
    rssDeltaBytes: LEAK_BUDGETS.maxRssDeltaBytes,
    rssSlopeBytesPerCycle: LEAK_BUDGETS.maxRssSlopeBytesPerCycle,
    fileDescriptorDelta: LEAK_BUDGETS.maxFileDescriptorDelta,
    fileDescriptorPeakGrowth: LEAK_BUDGETS.maxFileDescriptorPeakGrowth,
    activeResourceDelta: LEAK_BUDGETS.maxActiveResourceDelta,
    activeResourcePeakGrowth: LEAK_BUDGETS.maxActiveResourcePeakGrowth,
    listenerDelta: LEAK_BUDGETS.maxListenerDelta,
    listenerPeakGrowth: LEAK_BUDGETS.maxListenerPeakGrowth,
    childProcessPeak: LEAK_BUDGETS.maxChildProcessPeak,
    survivingProcesses: LEAK_BUDGETS.maxSurvivingProcesses,
    workerRestarts: LEAK_BUDGETS.maxWorkerRestarts,
  };
}

test("leak budgets accept their exact numeric limits", () => {
  const result = evaluateLeakBudgets(passingMetrics());
  assert.equal(result.passed, true);
  assert.equal(result.assertions.length, 12);
});

test("leak budgets reject growth and worker restarts", () => {
  const result = evaluateLeakBudgets({
    ...passingMetrics(),
    rssSlopeBytesPerCycle: LEAK_BUDGETS.maxRssSlopeBytesPerCycle + 1,
    workerRestarts: 1,
  });
  assert.equal(result.passed, false);
  assert.deepEqual(
    result.assertions.filter((item) => !item.passed).map((item) => item.metric),
    ["rssSlopeBytesPerCycle", "workerRestarts"],
  );
});

test("leak budgets require the planned 100-cycle sample", () => {
  assert.throws(
    () => evaluateLeakBudgets({ ...passingMetrics(), cycles: 99 }),
    /exactly 100 cycles/,
  );
});

function passingTimings() {
  return {
    workerHandshake: PERFORMANCE_BUDGETS.maxWorkerHandshakeMs,
    coldDiscovery: PERFORMANCE_BUDGETS.maxColdDiscoveryMs,
    coldContractAnalysis: PERFORMANCE_BUDGETS.maxColdContractAnalysisMs,
    combinedOverlayValidation:
      PERFORMANCE_BUDGETS.maxCombinedOverlayValidationMs,
    oneFileWarmAnalysis: PERFORMANCE_BUDGETS.maxOneFileWarmAnalysisMs,
    incrementalEdits: {
      p50: PERFORMANCE_BUDGETS.maxIncrementalEditP50Ms,
      p95: PERFORMANCE_BUDGETS.maxIncrementalEditP95Ms,
    },
    vitestStartup: PERFORMANCE_BUDGETS.maxVitestStartupMs,
  };
}

test("performance budgets accept their exact 20-percent limits", () => {
  const result = evaluatePerformanceBudgets(passingTimings());
  assert.equal(result.passed, true);
  assert.equal(result.assertions.length, 8);
});

test("performance budgets reject a p95 regression", () => {
  const result = evaluatePerformanceBudgets({
    ...passingTimings(),
    incrementalEdits: {
      ...passingTimings().incrementalEdits,
      p95: PERFORMANCE_BUDGETS.maxIncrementalEditP95Ms + 0.001,
    },
  });
  assert.equal(result.passed, false);
  assert.deepEqual(
    result.assertions.filter((item) => !item.passed).map((item) => item.metric),
    ["incrementalEditP95Ms"],
  );
});
