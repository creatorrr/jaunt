import type { DiagnosticRecord } from "../analyzer/types.js";

export type ProtocolErrorCode =
  | "PROTOCOL_MISMATCH"
  | "INVALID_REQUEST"
  | "NOT_INITIALIZED"
  | "ALREADY_INITIALIZED"
  | "MESSAGE_TOO_LARGE"
  | "DEADLINE_EXCEEDED"
  | "CANCELLED"
  | "PATH_OUTSIDE_ROOT"
  | "COMPILER_NOT_FOUND"
  | "COMPILER_UNSUPPORTED"
  | "CONFIG_INVALID"
  | "PROJECT_AMBIGUOUS"
  | "DISCOVERY_FAILED"
  | "INVALID_CONTRACT_SOURCE"
  | "STALE_SESSION"
  | "VALIDATION_FAILED"
  | "INTERNAL_ERROR";

export interface ProtocolErrorPayload {
  readonly code: ProtocolErrorCode;
  readonly message: string;
  readonly retryable: boolean;
  readonly diagnostics: readonly DiagnosticRecord[];
}

export class WorkerError extends Error {
  readonly payload: ProtocolErrorPayload;

  constructor(
    code: ProtocolErrorCode,
    message: string,
    options: {
      retryable?: boolean;
      diagnostics?: readonly DiagnosticRecord[];
    } = {},
  ) {
    super(message);
    this.name = "WorkerError";
    this.payload = {
      code,
      message,
      retryable: options.retryable ?? false,
      diagnostics: options.diagnostics ?? [],
    };
  }
}

export function normalizeError(error: unknown): ProtocolErrorPayload {
  if (error instanceof WorkerError) return error.payload;
  return {
    code: "INTERNAL_ERROR",
    message: error instanceof Error ? error.message : "Unknown worker error",
    retryable: false,
    diagnostics: [],
  };
}
