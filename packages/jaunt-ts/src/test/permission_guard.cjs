"use strict";

const { syncBuiltinESMExports } = require("node:module");
const workerThreads = require("node:worker_threads");

const installed = Symbol.for("@usejaunt/ts/permission-guard-installed");

if (!globalThis[installed]) {
  const OriginalWorker = workerThreads.Worker;

  class PermissionPreservingWorker extends OriginalWorker {
    constructor(filename, options = {}) {
      const trustedOptions = { ...options };
      delete trustedOptions.execArgv;
      // Omitting execArgv uses Node's native inheritance path for the outer
      // permission flags and this preload. Supplying them explicitly is not
      // portable across worker implementations; accepting the caller's value
      // would let generated code remove the permission model.
      super(filename, trustedOptions);
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
