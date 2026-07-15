import { readFileSync, rmSync } from "node:fs";
import { resolve } from "node:path";
import ts from "@typescript/typescript6";
import Ajv, { type ValidateFunction } from "ajv";
import { describe, expect, test, vi } from "vitest";
import { serializeType } from "../../src/analyzer/ir.js";
import {
  parseRequest,
  PROTOCOL_VERSION,
  type WorkerMethod,
} from "../../src/protocol/messages.js";
import { WorkerServer } from "../../src/worker/server.js";
import { AnalyzerSession } from "../../src/worker/session.js";
import { createFixtureWorkspace, packageRoot } from "../helpers/workspace.js";

const schemaRoot = resolve(packageRoot, "../../schemas/jaunt-ts");
const schema = JSON.parse(
  readFileSync(resolve(schemaRoot, "protocol-v1.schema.json"), "utf8"),
);
const contractSchema = JSON.parse(
  readFileSync(resolve(schemaRoot, "contract-ir-v1.schema.json"), "utf8"),
);
const ajv = new Ajv({ strict: true, allErrors: true });
ajv.addSchema(contractSchema);
const validate = ajv.compile(schema);
const validateContract = ajv.getSchema(contractSchema.$id) as ValidateFunction;

const methodFixtures = [
  ["initialize", "initialize"],
  ["analyzeWorkspace", "analyze-workspace"],
  ["analyzeContracts", "analyze-contracts"],
  ["projectContract", "project-contract"],
  ["validateOverlay", "validate-overlay"],
  ["findOrphans", "find-orphans"],
  ["invalidate", "invalidate"],
  ["cancel", "cancel"],
  ["shutdown", "shutdown"],
] as const satisfies readonly (readonly [WorkerMethod, string])[];

function fixture(name: string): Record<string, any> {
  return JSON.parse(
    readFileSync(resolve(schemaRoot, "fixtures", name), "utf8"),
  );
}

function validates(ref: string, value: unknown): boolean {
  const validator = ajv.compile({ $ref: `${schema.$id}#/definitions/${ref}` });
  return validator(value);
}

describe("protocol-v1 golden fixtures", () => {
  for (const [method, stem] of methodFixtures) {
    test(`${method} request and success match their pinned schemas`, () => {
      const request = fixture(`${stem}.request.json`);
      const response = fixture(`${stem}.response.json`);

      expect(request.method).toBe(method);
      expect(validate(request), JSON.stringify(validate.errors)).toBe(true);
      expect(validate(response), JSON.stringify(validate.errors)).toBe(true);

      const requestWithUnknownParam = structuredClone(request);
      requestWithUnknownParam.params.__futureDraftField = true;
      expect(validate(requestWithUnknownParam)).toBe(false);

      const responseWithUnknownResult = structuredClone(response);
      responseWithUnknownResult.result.__futureDraftField = true;
      expect(validate(responseWithUnknownResult)).toBe(false);
    });
  }

  test("the typed failure fixture remains an exact protocol alternative", () => {
    const value = fixture("error.response.json");
    expect(validate(value), JSON.stringify(validate.errors)).toBe(true);
    value.error.__futureDraftField = true;
    expect(validate(value)).toBe(false);
  });

  test("unexpected overlay failures report the active worker phase", async () => {
    const workspace = createFixtureWorkspace({ withTestSpec: true });
    const server = new WorkerServer();
    try {
      const initialized = await server.dispatch(
        parseRequest({
          protocol: PROTOCOL_VERSION,
          id: "phase-init",
          method: "initialize",
          params: {
            root: workspace.root,
            projects: ["tsconfig.json"],
            testProjects: ["tsconfig.test.json"],
            sourceRoots: ["src"],
            testRoots: ["tests"],
            generatedDir: "__generated__",
            toolOwner: ".",
            compilerModulePath: workspace.compilerModulePath,
            clientVersion: "test",
            toolVersion: "test",
          },
        }),
      );
      expect(initialized.ok, JSON.stringify(initialized)).toBe(true);
      const metadata = initialized.result as Record<string, unknown>;
      const validation = vi
        .spyOn(AnalyzerSession.prototype, "validateOverlay")
        .mockImplementation((_params, reportPhase) => {
          reportPhase?.({
            phase: "module-overlays",
            state: "start",
            elapsedMs: 1,
          });
          throw new Error("synthetic compiler failure");
        });
      try {
        const response = await server.dispatch(
          parseRequest({
            protocol: PROTOCOL_VERSION,
            id: "phase-overlay",
            method: "validateOverlay",
            params: {
              sessionId: metadata.sessionId,
              expectedEpoch: metadata.epoch,
              expectedSnapshot: metadata.snapshot,
              candidates: {},
            },
          }),
        );
        expect(validate(response), JSON.stringify(validate.errors)).toBe(true);
        expect(response).toMatchObject({
          ok: false,
          error: {
            code: "INTERNAL_ERROR",
            message:
              "validateOverlay failed during phase=module-overlays: synthetic compiler failure",
          },
        });
      } finally {
        validation.mockRestore();
      }
    } finally {
      rmSync(workspace.root, { recursive: true, force: true });
    }
  });

  test("request validation is strict and preserves deadlineMs", () => {
    const value = fixture("initialize.request.json");
    expect(parseRequest(value)).toMatchObject({
      protocol: PROTOCOL_VERSION,
      id: "init-1",
      method: "initialize",
      deadlineMs: 30_000,
    });
    expect(() => parseRequest({ ...value, surprise: true })).toThrow(
      /Unknown request field/,
    );
    expect(() =>
      parseRequest({ ...value, protocol: "jaunt-ts/1-draft.1" }),
    ).toThrow(/Expected protocol jaunt-ts\/1-draft\.2/u);
  });

  test("every worker method's live success matches the shared protocol schema", async () => {
    const workspace = createFixtureWorkspace({
      withClass: true,
      withTestSpec: true,
    });
    try {
      const server = new WorkerServer();
      const dispatch = async (
        id: string,
        method: WorkerMethod,
        params: Record<string, unknown>,
      ): Promise<Record<string, any>> => {
        const response = await server.dispatch(
          parseRequest({ protocol: PROTOCOL_VERSION, id, method, params }),
        );
        expect(validate(response), JSON.stringify(validate.errors)).toBe(true);
        expect(response.ok).toBe(true);
        return response as unknown as Record<string, any>;
      };

      const initialized = await dispatch("live-1", "initialize", {
        root: workspace.root,
        projects: ["tsconfig.json"],
        testProjects: ["tsconfig.test.json"],
        sourceRoots: ["src"],
        testRoots: ["tests"],
        generatedDir: "__generated__",
        toolOwner: ".",
        compilerModulePath: workspace.compilerModulePath,
        clientVersion: "test",
        toolVersion: "0.1.0-alpha.0",
        generationFingerprint: `sha256:${"a".repeat(64)}`,
      });
      await dispatch("live-2", "analyzeWorkspace", {});
      const analyzed = await dispatch("live-3", "analyzeContracts", {});
      await dispatch(
        "live-4",
        "projectContract",
        fixture("project-contract.request.json").params,
      );
      await dispatch("live-5", "validateOverlay", {
        sessionId: initialized.result.sessionId,
        expectedEpoch: initialized.result.epoch,
        expectedSnapshot: initialized.result.snapshot,
        candidates: {},
        moduleIds: ["ts:src/slug/index"],
        syncModuleIds: ["ts:src/slug/index"],
      });
      await dispatch("live-6", "findOrphans", {});
      await dispatch("live-7", "invalidate", {
        paths: ["src/slug/index.jaunt.ts"],
      });
      await dispatch("live-8", "cancel", { requestId: "future-request" });
      await dispatch("live-9", "shutdown", {});

      const coreKeys = new Set([
        "schema",
        "moduleId",
        "specPath",
        "facadePath",
        "apiMirrorPath",
        "implementationPath",
        "contextPath",
        "project",
        "packageOwner",
        "symbols",
        "options",
        "typeDeclarations",
        "typeImports",
        "contextDocs",
        "semanticEnvironmentDigest",
        "dependencies",
        "structuralDigest",
        "proseDigest",
        "apiDigest",
        "fingerprint",
      ]);
      for (const module of analyzed.result.modules) {
        const ir = Object.fromEntries(
          Object.entries(module).filter(([key]) => coreKeys.has(key)),
        );
        expect(
          validateContract(ir),
          JSON.stringify(validateContract.errors),
        ).toBe(true);
      }
    } finally {
      rmSync(workspace.root, { recursive: true, force: true });
    }
  });

  test("contract IR closes every core nested record", () => {
    const module = fixture("analyze-contracts.response.json").result.modules[0];
    const analysisOnly = new Set([
      "routes",
      "apiSource",
      "placeholderSource",
      "sidecar",
      "specSource",
      "contextSource",
    ]);
    const ir = Object.fromEntries(
      Object.entries(module).filter(([key]) => !analysisOnly.has(key)),
    );
    expect(validateContract(ir), JSON.stringify(validateContract.errors)).toBe(
      true,
    );

    const mutations = [
      (value: any) => (value.options.__futureDraftField = true),
      (value: any) =>
        (value.symbols[0].signatures[0].returnType.__futureDraftField = true),
      (value: any) => (value.symbols[1].members[0].__futureDraftField = true),
      (value: any) => (value.typeDeclarations[0].__futureDraftField = true),
      (value: any) => (value.typeImports[0].__futureDraftField = true),
      (value: any) =>
        (value.contextDocs[0].exports[0].__futureDraftField = true),
      (value: any) => (value.fingerprint.__futureDraftField = true),
    ];
    for (const mutate of mutations) {
      const changed = structuredClone(ir);
      mutate(changed);
      expect(validateContract(changed)).toBe(false);
    }
  });

  test("every currently serialized TypeScript type family satisfies typeNode", () => {
    const source = ts.createSourceFile(
      "schema-types.ts",
      `declare const runtimeValue: { value: string };
type Keywords = void | undefined | never | unknown | any | string | number | bigint | boolean | symbol | object;
type Literals = "text" | true | false | 1 | -2 | 3n | -4n | null;
type Reference = Promise<string>;
type ArrayType = readonly string[];
type Tuple = [head: string, tail?: number, ...rest: boolean[]];
type Callable = <T extends string = "x">(value: T, ...rest: number[]) => T[];
type Constructable = new (value: string) => { value: string };
type ObjectType = {
  readonly value?: string;
  method(input: number): void;
  (input: string): number;
  new (): { ok: true };
  [key: string]: unknown;
};
type Indexed = { value: string }["value"];
type Conditional<T> = T extends string ? "yes" : "no";
type Operator = keyof { value: string };
type Query = typeof runtimeValue;
type Imported = import("./types.js").Thing<string>;
type Mapped<T> = { readonly [K in keyof T as \`get\${string & K}\`]?: T[K] };
type Template<T extends string> = \`prefix-\${T}-suffix\`;`,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TS,
    );
    const typeNode = ajv.compile({
      $ref: `${contractSchema.$id}#/definitions/typeNode`,
    });
    const serialized = new Map<string, ReturnType<typeof serializeType>>();
    for (const alias of source.statements.filter(ts.isTypeAliasDeclaration)) {
      const ir = serializeType(ts, alias.type);
      serialized.set(alias.name.text, ir);
      expect(
        typeNode(ir),
        `${alias.name.text}: ${JSON.stringify(typeNode.errors)}`,
      ).toBe(true);
    }
    expect(serialized.get("Imported")).toEqual({
      kind: "import",
      text: '"./types.js"',
      name: "Thing",
      typeArguments: [{ kind: "string" }],
    });
  });

  test("projectContract request and result match the typed shared fixture", async () => {
    const request = fixture("project-contract.request.json");
    const response = fixture("project-contract.response.json");
    expect(validates("projectContractParams", request.params)).toBe(true);
    expect(validates("projectContractResult", response.result)).toBe(true);

    const workspace = createFixtureWorkspace();
    try {
      const session = await AnalyzerSession.create({
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
      });
      expect(session.projectContract(request.params)).toEqual(response.result);
    } finally {
      rmSync(workspace.root, { recursive: true, force: true });
    }
  });
});
