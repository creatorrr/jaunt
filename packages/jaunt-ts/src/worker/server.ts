import { normalizeError, WorkerError } from "../protocol/errors.js";
import {
  failure,
  success,
  type WorkerRequest,
  type WorkerResponse,
} from "../protocol/messages.js";
import {
  AnalyzerSession,
  parseAnalyzeContractsParams,
  parseAnalyzeWorkspaceParams,
  parseCancelParams,
  parseEmptyParams,
  parseFindOrphansParams,
  parseInitializeParams,
  parseInvalidateParams,
  parseProjectContractParams,
  parseValidateOverlayParams,
} from "./session.js";

export class WorkerServer {
  #session?: AnalyzerSession;
  readonly #cancelled = new Set<string>();
  readonly #lastPhase = new Map<string, string>();
  shutdownRequested = false;

  private reportTiming(
    request: WorkerRequest,
    phase: string,
    state: "start" | "finish",
    elapsedMs: number,
  ): void {
    this.#lastPhase.set(request.id, phase);
    if (process.env.JAUNT_TS_PHASE_TELEMETRY !== "1") return;
    process.stderr.write(
      `[jaunt:phase] request=${request.id} method=${request.method} phase=${phase} state=${state} elapsed_ms=${Math.round(elapsedMs)}\n`,
    );
  }

  async dispatch(request: WorkerRequest): Promise<WorkerResponse> {
    try {
      const result = await this.withDeadline(request, () =>
        this.handle(request),
      );
      return success(request.id, result);
    } catch (error) {
      const normalized = normalizeError(error);
      const phase = this.#lastPhase.get(request.id);
      return failure(
        request.id,
        normalized.code === "INTERNAL_ERROR" && phase
          ? {
              ...normalized,
              message: `${request.method} failed during phase=${phase}: ${normalized.message}`,
            }
          : normalized,
      );
    } finally {
      this.#lastPhase.delete(request.id);
    }
  }

  private async withDeadline(
    request: WorkerRequest,
    operation: () => Promise<unknown>,
  ): Promise<unknown> {
    if (request.deadlineMs === undefined) return operation();
    let timer: NodeJS.Timeout | undefined;
    try {
      return await Promise.race([
        operation(),
        new Promise<never>((_resolve, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new WorkerError(
                  "DEADLINE_EXCEEDED",
                  `Request ${request.id} exceeded its deadline`,
                  { retryable: true },
                ),
              ),
            request.deadlineMs,
          );
        }),
      ]);
    } finally {
      if (timer) clearTimeout(timer);
    }
  }

  private async handle(request: WorkerRequest): Promise<unknown> {
    if (this.#cancelled.delete(request.id))
      throw new WorkerError("CANCELLED", `Request ${request.id} was cancelled`);
    if (request.method === "initialize") {
      if (this.#session)
        throw new WorkerError(
          "ALREADY_INITIALIZED",
          "Worker is already initialized",
        );
      const startedAt = performance.now();
      this.reportTiming(request, "initialize", "start", 0);
      try {
        this.#session = await AnalyzerSession.create(
          parseInitializeParams(request.params),
        );
      } finally {
        this.reportTiming(
          request,
          "initialize",
          "finish",
          performance.now() - startedAt,
        );
      }
      return this.#session.initializeResult();
    }
    if (request.method === "cancel") {
      const { requestId } = parseCancelParams(request.params);
      this.#cancelled.add(requestId);
      return { cancelled: requestId };
    }
    if (request.method === "shutdown") {
      parseEmptyParams(request.params, "shutdown");
      this.shutdownRequested = true;
      return { shutdown: true };
    }
    const session = this.#session;
    if (!session)
      throw new WorkerError(
        "NOT_INITIALIZED",
        "initialize must be the first request",
      );
    if (request.method === "analyzeWorkspace") {
      return session.analyzeWorkspace(
        parseAnalyzeWorkspaceParams(request.params),
      );
    }
    if (request.method === "analyzeContracts") {
      return session.analyzeContracts(
        parseAnalyzeContractsParams(request.params),
      );
    }
    if (request.method === "projectContract") {
      return session.projectContract(
        parseProjectContractParams(request.params),
      );
    }
    if (request.method === "validateOverlay") {
      return session.validateOverlay(
        parseValidateOverlayParams(request.params),
        ({ phase, state, elapsedMs }) =>
          this.reportTiming(request, phase, state, elapsedMs),
      );
    }
    if (request.method === "findOrphans") {
      return session.findOrphans(parseFindOrphansParams(request.params));
    }
    if (request.method === "invalidate") {
      return session.invalidate(parseInvalidateParams(request.params));
    }
    throw new WorkerError(
      "INVALID_REQUEST",
      `Unsupported method ${request.method}`,
    );
  }
}
