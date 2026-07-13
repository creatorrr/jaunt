import { expect, test } from "vitest";
import {
  parseAnalyzeContractsParams,
  parseCancelParams,
  parseEmptyParams,
  parseFindOrphansParams,
  parseInitializeParams,
  parseInvalidateParams,
  parseProjectContractParams,
  parseValidateOverlayParams,
} from "../../src/worker/session.js";

function input(): Record<string, unknown> {
  return {
    root: "/workspace",
    projects: ["tsconfig.json"],
    testProjects: [],
    sourceRoots: ["src"],
    testRoots: ["tests"],
    generatedDir: "__generated__",
    toolOwner: ".",
    compilerModulePath: "/workspace/node_modules/typescript/lib/typescript.js",
    clientVersion: "1.0.0",
    toolVersion: "1.0.0",
  };
}

test("initialize rejects workspace path escapes before filesystem access", () => {
  expect(() =>
    parseInitializeParams({ ...input(), sourceRoots: ["../outside"] }),
  ).toThrow(/safe root-relative POSIX path/);
  expect(() =>
    parseInitializeParams({ ...input(), generatedDir: "." }),
  ).toThrow(/safe root-relative POSIX path/);
  expect(() =>
    parseInitializeParams({ ...input(), toolOwner: "C:\\outside" }),
  ).toThrow(/safe root-relative POSIX path/);
});

test("overlay parsing keeps sync and authorized restamp selections distinct", () => {
  expect(
    parseValidateOverlayParams({
      sessionId: "session-1",
      expectedEpoch: 0,
      expectedSnapshot: "sha256:snapshot",
      candidates: {},
      syncModuleIds: ["ts:src/new/index"],
      restampModuleIds: ["ts:src/built/index"],
    }),
  ).toMatchObject({
    syncModuleIds: ["ts:src/new/index"],
    restampModuleIds: ["ts:src/built/index"],
  });
});

test("every method parser rejects fields outside its pinned draft shape", () => {
  const cases: readonly (() => unknown)[] = [
    () => parseInitializeParams({ ...input(), future: true }),
    () => parseEmptyParams({ future: true }, "analyzeWorkspace"),
    () => parseAnalyzeContractsParams({ future: true }),
    () =>
      parseProjectContractParams({
        source: "export function value(): string;",
        symbol: "value",
        fileName: "src/value.ts",
        future: true,
      }),
    () =>
      parseValidateOverlayParams({
        sessionId: "session-1",
        expectedEpoch: 0,
        expectedSnapshot: "snapshot",
        candidates: {},
        future: true,
      }),
    () => parseFindOrphansParams({ future: true }),
    () => parseInvalidateParams({ paths: [], future: true }),
    () => parseCancelParams({ requestId: "1", future: true }),
    () => parseEmptyParams({ future: true }, "shutdown"),
  ];
  for (const invoke of cases) {
    expect(invoke).toThrow(/params contain unknown field/u);
  }
});
