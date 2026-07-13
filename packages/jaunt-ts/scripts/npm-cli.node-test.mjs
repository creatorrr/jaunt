import assert from "node:assert/strict";
import test from "node:test";
import { npmCliInvocation } from "./npm-cli.mjs";

test("npm CLI resolution uses npm_execpath without a Windows shell", () => {
  assert.deepEqual(
    npmCliInvocation({
      platform: "win32",
      environment: {
        npm_execpath: "C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js",
      },
      nodePath: "C:\\nodejs\\node.exe",
      fileExists: () => false,
    }),
    {
      command: "C:\\nodejs\\node.exe",
      args: ["C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js"],
    },
  );
});

test("npm CLI resolution finds the standard Windows Node installation", () => {
  const npmCli = "C:\\nodejs\\node_modules\\npm\\bin\\npm-cli.js";
  assert.deepEqual(
    npmCliInvocation({
      platform: "win32",
      environment: {},
      nodePath: "C:\\nodejs\\node.exe",
      fileExists: (candidate) => candidate === npmCli,
    }),
    { command: "C:\\nodejs\\node.exe", args: [npmCli] },
  );
});

test("npm CLI resolution keeps the POSIX executable fallback", () => {
  assert.deepEqual(
    npmCliInvocation({
      platform: "linux",
      environment: {},
      nodePath: "/usr/bin/node",
      fileExists: () => false,
    }),
    { command: "npm", args: [] },
  );
});

test("npm CLI resolution fails closed when Windows has no npm JavaScript entry", () => {
  assert.throws(
    () =>
      npmCliInvocation({
        platform: "win32",
        environment: {},
        nodePath: "C:\\custom\\node.exe",
        fileExists: () => false,
      }),
    /Could not locate npm-cli\.js/,
  );
});
