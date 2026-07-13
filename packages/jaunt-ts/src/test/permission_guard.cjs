"use strict";

const { syncBuiltinESMExports } = require("node:module");
const workerThreads = require("node:worker_threads");

const installed = Symbol.for("@usejaunt/ts/permission-guard-installed");

if (!globalThis[installed]) {
  const OriginalWorker = workerThreads.Worker;
  const trustedExecArgv = Object.freeze([...process.execArgv]);

  class PermissionPreservingWorker extends OriginalWorker {
    constructor(filename, options = {}) {
      super(filename, {
        ...options,
        execArgv: [...trustedExecArgv],
      });
    }
  }

  Object.defineProperty(workerThreads, "Worker", {
    configurable: false,
    enumerable: true,
    value: PermissionPreservingWorker,
    writable: false,
  });
  Object.defineProperty(globalThis, installed, {
    configurable: false,
    value: true,
  });
  syncBuiltinESMExports();
}
