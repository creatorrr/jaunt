import type {
  ArtifactRecord,
  DiagnosticRecord,
  DiscoveredContract,
  DiscoveredSpec,
  DiscoveredTestSpec,
  ModuleRoute,
  OrphanRecord,
  ProjectRecord,
  SessionMetadata,
} from "../analyzer/types.js";
import type { ContractModuleIR } from "../analyzer/ir.js";
import { WorkerError, type ProtocolErrorPayload } from "./errors.js";

export const PROTOCOL_VERSION = "jaunt-ts/1-draft.2" as const;
export const CONTRACT_IR_VERSION = "contract-ir/1-draft.2" as const;

export type WorkerMethod =
  | "initialize"
  | "analyzeWorkspace"
  | "analyzeContracts"
  | "projectContract"
  | "validateOverlay"
  | "findOrphans"
  | "invalidate"
  | "cancel"
  | "shutdown";

export interface InitializeParams {
  readonly root: string;
  readonly projects: readonly string[];
  readonly testProjects: readonly string[];
  readonly sourceRoots: readonly string[];
  readonly testRoots: readonly string[];
  readonly generatedDir: string;
  readonly toolOwner: string;
  readonly compilerModulePath: string;
  readonly clientVersion: string;
  readonly toolVersion: string;
  readonly generationFingerprint?: string;
}

export interface AnalyzeContractsParams {
  readonly moduleIds?: readonly string[];
}

export interface ProjectContractParams {
  readonly source: string;
  readonly symbol: string;
  readonly fileName: string;
}

export interface ProjectContractResult {
  readonly source: string;
  readonly sourceDigest: string;
  readonly symbol: string;
  readonly kind: "function" | "class";
  readonly declarationStart: number;
  readonly declarationEnd: number;
  readonly docsStart?: number;
  readonly docsEnd?: number;
}

export interface ValidateOverlayParams {
  readonly sessionId: string;
  readonly expectedEpoch: number;
  readonly expectedSnapshot: string;
  readonly candidates: Readonly<Record<string, string>>;
  readonly moduleIds?: readonly string[];
  readonly syncModuleIds?: readonly string[];
  readonly restampModuleIds?: readonly string[];
}

export interface FindOrphansParams {
  readonly moduleIds?: readonly string[];
}

export interface InvalidateParams {
  readonly paths: readonly string[];
}

export interface CancelParams {
  readonly requestId: string;
}

export interface WorkerRequest {
  readonly protocol: typeof PROTOCOL_VERSION;
  readonly id: string;
  readonly method: WorkerMethod;
  readonly params: Readonly<Record<string, unknown>>;
  readonly deadlineMs?: number;
}

export interface InitializeResult extends SessionMetadata {
  readonly workerVersion: string;
  readonly protocol: typeof PROTOCOL_VERSION;
  readonly typescriptVersion: string;
  readonly packageManager: string;
  readonly capabilities: readonly string[];
}

export interface AnalyzeWorkspaceResult extends SessionMetadata {
  readonly projects: readonly ProjectRecord[];
  readonly routes: readonly ModuleRoute[];
  readonly specs: readonly DiscoveredSpec[];
  readonly testSpecs: readonly DiscoveredTestSpec[];
  readonly contracts: readonly DiscoveredContract[];
  readonly diagnostics: readonly DiagnosticRecord[];
}

export interface ContractAnalysisRecord extends ContractModuleIR {
  readonly routes: ModuleRoute;
  readonly apiSource: string;
  readonly placeholderSource: string;
  readonly sidecar: string;
  readonly specSource: string;
  readonly contextSource?: string;
}

export interface AnalyzeContractsResult extends SessionMetadata {
  readonly modules: readonly ContractAnalysisRecord[];
}

export interface ValidateOverlayResult extends SessionMetadata {
  readonly valid: boolean;
  readonly artifacts: readonly ArtifactRecord[];
  readonly diagnostics: readonly DiagnosticRecord[];
  readonly affectedProjects: readonly string[];
}

export interface FindOrphansResult extends SessionMetadata {
  readonly artifacts: readonly OrphanRecord[];
}

export interface InvalidateResult extends SessionMetadata {
  readonly invalidated: readonly string[];
}

export interface WorkerSuccess {
  readonly protocol: typeof PROTOCOL_VERSION;
  readonly id: string;
  readonly ok: true;
  readonly result: unknown;
}

export interface WorkerFailure {
  readonly protocol: typeof PROTOCOL_VERSION;
  readonly id: string;
  readonly ok: false;
  readonly error: ProtocolErrorPayload;
}

export type WorkerResponse = WorkerSuccess | WorkerFailure;

const METHODS = new Set<WorkerMethod>([
  "initialize",
  "analyzeWorkspace",
  "analyzeContracts",
  "projectContract",
  "validateOverlay",
  "findOrphans",
  "invalidate",
  "cancel",
  "shutdown",
]);

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function assertOnlyKeys(
  value: Record<string, unknown>,
  allowed: ReadonlySet<string>,
): void {
  const extra = Object.keys(value).filter((key) => !allowed.has(key));
  if (extra.length > 0) {
    throw new WorkerError(
      "INVALID_REQUEST",
      `Unknown request field(s): ${extra.sort().join(", ")}`,
    );
  }
}

export function parseRequest(value: unknown): WorkerRequest {
  if (!isRecord(value))
    throw new WorkerError("INVALID_REQUEST", "Request must be an object");
  assertOnlyKeys(
    value,
    new Set(["protocol", "id", "method", "params", "deadlineMs"]),
  );
  if (value.protocol !== PROTOCOL_VERSION) {
    throw new WorkerError(
      "PROTOCOL_MISMATCH",
      `Expected protocol ${PROTOCOL_VERSION}, received ${String(value.protocol)}`,
    );
  }
  if (
    typeof value.id !== "string" ||
    value.id.length === 0 ||
    value.id.length > 128
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "id must be a non-empty string of at most 128 characters",
    );
  }
  if (
    typeof value.method !== "string" ||
    !METHODS.has(value.method as WorkerMethod)
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      `Unsupported method: ${String(value.method)}`,
    );
  }
  if (!isRecord(value.params))
    throw new WorkerError("INVALID_REQUEST", "params must be an object");
  if (
    value.deadlineMs !== undefined &&
    (typeof value.deadlineMs !== "number" ||
      !Number.isInteger(value.deadlineMs) ||
      value.deadlineMs < 1 ||
      value.deadlineMs > 3_600_000)
  ) {
    throw new WorkerError(
      "INVALID_REQUEST",
      "deadlineMs must be an integer from 1 to 3600000",
    );
  }
  return {
    protocol: PROTOCOL_VERSION,
    id: value.id,
    method: value.method as WorkerMethod,
    params: value.params,
    ...(value.deadlineMs === undefined
      ? {}
      : { deadlineMs: value.deadlineMs as number }),
  };
}

export function success(id: string, result: unknown): WorkerSuccess {
  return { protocol: PROTOCOL_VERSION, id, ok: true, result };
}

export function failure(
  id: string,
  error: ProtocolErrorPayload,
): WorkerFailure {
  return { protocol: PROTOCOL_VERSION, id, ok: false, error };
}
