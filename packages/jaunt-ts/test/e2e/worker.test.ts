import { spawn } from "node:child_process";
import { existsSync, rmSync, writeFileSync } from "node:fs";
import { createServer } from "node:http";
import { resolve } from "node:path";
import { afterEach, expect, test } from "vitest";
import { createFixtureWorkspace } from "../helpers/workspace.js";

const roots: string[] = [];
afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

test("symlink-installed JSONL worker handshakes and uses protocol-only stdout", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const child = spawn(
    process.execPath,
    [resolve(workspace.root, "node_modules/@usejaunt/ts/dist/worker/main.js")],
    {
      cwd: workspace.root,
      stdio: ["pipe", "pipe", "pipe"],
    },
  );
  const requests = [
    {
      protocol: "jaunt-ts/1-draft.3",
      id: "1",
      method: "initialize",
      params: {
        root: workspace.root,
        projects: ["tsconfig.json"],
        testProjects: [],
        sourceRoots: ["src"],
        testRoots: ["tests"],
        generatedDir: "__generated__",
        toolOwner: ".",
        compilerModulePath: workspace.compilerModulePath,
        clientVersion: "test",
        toolVersion: "0.1.0-alpha.0",
      },
    },
    {
      protocol: "jaunt-ts/1-draft.3",
      id: "2",
      method: "analyzeWorkspace",
      params: {},
    },
    {
      protocol: "jaunt-ts/1-draft.3",
      id: "3",
      method: "projectContract",
      params: {
        source:
          "/** Public contract. */\n" +
          "export function value(): { ok: true } { " +
          'throw new Error("worker-secret"); }\n',
        symbol: "value",
        fileName: "src/value.ts",
      },
    },
    { protocol: "jaunt-ts/1-draft.3", id: "4", method: "shutdown", params: {} },
  ];
  child.stdin.end(
    `${requests.map((request) => JSON.stringify(request)).join("\n")}\n`,
  );
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));
  const exitCode = await new Promise<number | null>((resolveExit) =>
    child.on("exit", resolveExit),
  );
  expect(exitCode, Buffer.concat(stderr).toString()).toBe(0);
  const responses = Buffer.concat(stdout)
    .toString()
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  expect(responses).toHaveLength(4);
  expect(responses[0]).toMatchObject({ id: "1", ok: true });
  expect(responses[1].result.routes[0].moduleId).toBe("ts:src/slug/index");
  expect(responses[2]).toMatchObject({ id: "3", ok: true });
  expect(responses[2].result.source).toContain(
    "export function value(): { ok: true };",
  );
  expect(responses[2].result.source).not.toContain("worker-secret");
  expect(responses[3]).toMatchObject({
    id: "4",
    ok: true,
    result: { shutdown: true },
  });
});

test("analysis never executes hostile user modules", async () => {
  const workspace = createFixtureWorkspace();
  roots.push(workspace.root);
  const filesystemSentinel = resolve(workspace.root, "filesystem-executed");
  const childSentinel = resolve(workspace.root, "child-executed");
  let networkRequests = 0;
  const server = createServer((_request, response) => {
    networkRequests += 1;
    response.end("executed");
  });
  await new Promise<void>((resolveListen, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", () => resolveListen());
  });
  const address = server.address();
  if (address === null || typeof address === "string")
    throw new Error("hostile-fixture server did not bind a TCP port");

  const childScript = `require("node:fs").writeFileSync(${JSON.stringify(childSentinel)}, "executed")`;
  writeFileSync(
    resolve(workspace.root, "src/slug/index.jaunt.ts"),
    `import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";
import process from "node:process";
import * as jaunt from "@usejaunt/ts/spec";

writeFileSync(${JSON.stringify(filesystemSentinel)}, "executed");
execFileSync(process.execPath, ["-e", ${JSON.stringify(childScript)}]);
await fetch(${JSON.stringify(`http://127.0.0.1:${address.port}/executed`)}, { method: "POST" });
process.exit(73);

jaunt.magicModule();
/** A contract that must be discovered without importing this module. */
export function slugify(title: string): string {
  return jaunt.magic();
}
`,
  );

  const child = spawn(
    process.execPath,
    [resolve(workspace.root, "node_modules/@usejaunt/ts/dist/worker/main.js")],
    { cwd: workspace.root, stdio: ["pipe", "pipe", "pipe"] },
  );
  child.stdin.end(
    `${[
      {
        protocol: "jaunt-ts/1-draft.3",
        id: "1",
        method: "initialize",
        params: {
          root: workspace.root,
          projects: ["tsconfig.json"],
          testProjects: [],
          sourceRoots: ["src"],
          testRoots: ["tests"],
          generatedDir: "__generated__",
          toolOwner: ".",
          compilerModulePath: workspace.compilerModulePath,
          clientVersion: "test",
          toolVersion: "0.1.0-alpha.0",
        },
      },
      {
        protocol: "jaunt-ts/1-draft.3",
        id: "2",
        method: "analyzeContracts",
        params: {},
      },
      {
        protocol: "jaunt-ts/1-draft.3",
        id: "3",
        method: "shutdown",
        params: {},
      },
    ]
      .map((request) => JSON.stringify(request))
      .join("\n")}\n`,
  );
  const stdout: Buffer[] = [];
  const stderr: Buffer[] = [];
  child.stdout.on("data", (chunk: Buffer) => stdout.push(chunk));
  child.stderr.on("data", (chunk: Buffer) => stderr.push(chunk));

  try {
    const exitCode = await new Promise<number | null>((resolveExit) =>
      child.on("exit", resolveExit),
    );
    expect(exitCode, Buffer.concat(stderr).toString()).toBe(0);
    const responses = Buffer.concat(stdout)
      .toString()
      .trim()
      .split("\n")
      .map((line) => JSON.parse(line));
    expect(responses).toHaveLength(3);
    expect(responses[1]).toMatchObject({ id: "2", ok: true });
    expect(responses[1].result.modules).toHaveLength(1);
    expect(existsSync(filesystemSentinel)).toBe(false);
    expect(existsSync(childSentinel)).toBe(false);
    expect(networkRequests).toBe(0);
  } finally {
    await new Promise<void>((resolveClose, reject) =>
      server.close((error) => (error ? reject(error) : resolveClose())),
    );
  }
});
