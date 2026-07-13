"use strict";

const { syncBuiltinESMExports } = require("node:module");
const workerThreads = require("node:worker_threads");

const installed = Symbol.for("@usejaunt/ts/permission-guard-installed");

if (!globalThis[installed]) {
  const OriginalWorker = workerThreads.Worker;
  const permissionFlag =
    process.execArgv.find(
      (value) =>
        value === "--permission" || value === "--experimental-permission",
    ) ??
    (process.allowedNodeEnvironmentFlags.has("--permission")
      ? "--permission"
      : "--experimental-permission");
  const trustedNodeArgs = [
    permissionFlag,
    ...(process.execArgv.includes("--allow-addons") ? ["--allow-addons"] : []),
    ...process.execArgv.filter(
      (value) =>
        value.startsWith("--allow-fs-read=") ||
        value.startsWith("--allow-fs-write="),
    ),
    `--require=${__filename}`,
  ];
  const trustedNodeOptions = trustedNodeArgs
    .map(
      (value) => `"${value.replaceAll("\\", "\\\\").replaceAll('"', '\\"')}"`,
    )
    .join(" ");

  function sanitizedEnvironment(requested) {
    const source =
      requested &&
      typeof requested === "object" &&
      requested !== workerThreads.SHARE_ENV
        ? requested
        : process.env;
    const environment = {};
    for (const [key, value] of Object.entries(source)) {
      if (key.toUpperCase() !== "NODE_OPTIONS") environment[key] = value;
    }
    environment.NODE_OPTIONS = trustedNodeOptions;
    return environment;
  }

  function PermissionPreservingWorker(filename, options = {}) {
    if (!new.target) {
      throw new TypeError(
        "Class constructor Worker cannot be invoked without 'new'",
      );
    }
    const trustedOptions = {
      ...options,
      env: sanitizedEnvironment(options?.env),
      execArgv: [],
    };
    const newTarget =
      new.target === PermissionPreservingWorker ? OriginalWorker : new.target;
    return Reflect.construct(
      OriginalWorker,
      [filename, trustedOptions],
      newTarget,
    );
  }
  Object.defineProperty(PermissionPreservingWorker, "name", {
    value: "Worker",
  });
  Object.setPrototypeOf(
    PermissionPreservingWorker,
    Object.getPrototypeOf(OriginalWorker),
  );
  PermissionPreservingWorker.prototype = OriginalWorker.prototype;
  Object.defineProperty(PermissionPreservingWorker.prototype, "constructor", {
    configurable: false,
    value: PermissionPreservingWorker,
    writable: false,
  });

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
