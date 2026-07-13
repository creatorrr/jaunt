import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, relative, resolve, sep } from "node:path";
import type { UserConsoleLog } from "vitest";
import type { Reporter, SerializedError, TestModule } from "vitest/node";
import { sha256Bytes } from "../analyzer/canonical.js";
import { HeldOutLeakGuard } from "./heldout.js";

export type TestTier = "example" | "derived";
export type FailureCategory =
  "assertion" | "timeout" | "type" | "runtime" | "collection" | "runner";

const GENERATED_TEST_HEADER =
  "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.";

export interface TestResultRecord {
  readonly file: string;
  readonly tier: TestTier;
  readonly status: "passed" | "failed" | "skipped";
  readonly caseId?: string;
  readonly category?: FailureCategory;
  readonly durationMs: number;
  readonly message?: string;
}

interface ReporterModuleIdentity {
  readonly moduleId: string;
  /** Added by Vitest 4; Vitest 3 only exposes the absolute `moduleId`. */
  readonly relativeModuleId?: string;
}

export function reporterModulePath(
  root: string,
  module: ReporterModuleIdentity,
): string {
  if (module.relativeModuleId) {
    return module.relativeModuleId.replaceAll("\\", "/");
  }
  const absolute = isAbsolute(module.moduleId)
    ? module.moduleId
    : resolve(root, module.moduleId);
  const candidate = relative(root, absolute);
  const within =
    candidate !== ".." &&
    !candidate.startsWith(`..${sep}`) &&
    !isAbsolute(candidate);
  return (within ? candidate : module.moduleId).replaceAll("\\", "/");
}

export function classifyTier(file: string, source: string): TestTier {
  if (/\.example\.test\.(?:ts|tsx)$/.test(file) && isManagedExample(source)) {
    return "example";
  }
  return "derived";
}

function canonicalManagedBody(source: string): string {
  const normalized = source
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n")
    .trim();
  return `${normalized}\n`;
}

function isManagedExample(source: string): boolean {
  const normalized = source
    .replace(/^\uFEFF+/, "")
    .replaceAll("\r\n", "\n")
    .replaceAll("\r", "\n");
  const lines = normalized.split("\n");
  if (lines[0] !== GENERATED_TEST_HEADER) return false;

  const fields = new Map<string, string>();
  let cursor = 1;
  let separated = false;
  while (cursor < lines.length) {
    const line = lines[cursor] ?? "";
    if (line.trim().length === 0) {
      cursor += 1;
      separated = true;
      break;
    }
    if (!line.startsWith("// jaunt:")) return false;
    const payload = line.slice("// jaunt:".length);
    const equals = payload.indexOf("=");
    if (equals < 1) return false;
    const key = payload.slice(0, equals);
    if (!/^[a-z0-9_]+$/.test(key) || fields.has(key)) return false;
    fields.set(key, payload.slice(equals + 1));
    cursor += 1;
  }
  if (!separated) return false;
  while (cursor < lines.length && (lines[cursor] ?? "").trim().length === 0)
    cursor += 1;

  const body = canonicalManagedBody(lines.slice(cursor).join("\n"));
  const bodyDigest = fields.get("body_digest");
  return (
    fields.get("tier") === "example" &&
    bodyDigest !== undefined &&
    /^sha256:[0-9a-f]{64}$/.test(bodyDigest) &&
    bodyDigest === sha256Bytes(body)
  );
}

export function normalizeFailureCategory(error: unknown): FailureCategory {
  const record =
    error && typeof error === "object"
      ? (error as Record<string, unknown>)
      : {};
  const name = typeof record.name === "string" ? record.name.toLowerCase() : "";
  const message =
    typeof record.message === "string" ? record.message.toLowerCase() : "";
  if (name.includes("assert") || message.includes("expected"))
    return "assertion";
  if (
    name.includes("timeout") ||
    message.includes("timed out") ||
    message.includes("timeout")
  )
    return "timeout";
  if (name.includes("typeerror")) return "type";
  return "runtime";
}

export class JauntReporter implements Reporter {
  readonly results: TestResultRecord[] = [];
  readonly captured = { stdout: "", stderr: "" };
  readonly heldOut = new HeldOutLeakGuard();
  outputTruncated = false;
  readonly #root: string;
  readonly #overlays: Readonly<Record<string, string>>;
  readonly #redactDerived: boolean;

  constructor(
    root: string,
    overlays: Readonly<Record<string, string>>,
    redactDerived: boolean,
  ) {
    this.#root = root;
    this.#overlays = overlays;
    this.#redactDerived = redactDerived;
  }

  private sourceFor(file: string): string {
    const absolute = resolve(file);
    const candidate = relative(this.#root, absolute);
    const within =
      candidate !== ".." &&
      !candidate.startsWith(`..${sep}`) &&
      !isAbsolute(candidate);
    const relativePath = within ? candidate.replaceAll("\\", "/") : file;
    if (this.#overlays[relativePath] !== undefined)
      return this.#overlays[relativePath];
    return existsSync(absolute) ? readFileSync(absolute, "utf8") : "";
  }

  private allowPublicRecord(record: TestResultRecord): void {
    if (record.tier === "example") {
      this.heldOut.allow(record);
      return;
    }
    this.heldOut.allow({
      ...(record.caseId === undefined ? {} : { caseId: record.caseId }),
      ...(record.category === undefined ? {} : { category: record.category }),
    });
  }

  onUserConsoleLog(log: UserConsoleLog): void {
    if (this.#redactDerived) {
      this.heldOut.observe(log.content);
      return;
    }
    const limit = 64 * 1024;
    const key = log.type;
    const current = Buffer.byteLength(this.captured[key], "utf8");
    const remaining = Math.max(0, limit - current);
    const buffer = Buffer.from(log.content);
    if (remaining > 0) {
      this.captured[key] += buffer.subarray(0, remaining).toString("utf8");
    }
    this.outputTruncated ||= buffer.byteLength > remaining;
  }

  onTestRunEnd(
    modules: readonly TestModule[],
    unhandledErrors: readonly SerializedError[],
  ): void {
    for (const module of modules) {
      const modulePath = reporterModulePath(this.#root, module);
      const tier = classifyTier(
        module.moduleId,
        this.sourceFor(module.moduleId),
      );
      for (const test of module.children.allTests()) {
        const result = test.result();
        const status = result.state === "pending" ? "failed" : result.state;
        const error = result.errors?.[0];
        const caseId = sha256Bytes(`${modulePath}\0${test.fullName}`).slice(
          7,
          23,
        );
        if (tier === "derived" && error !== undefined) {
          this.heldOut.observe(error);
        }
        const record: TestResultRecord = {
          file: modulePath,
          tier,
          status,
          durationMs: test.diagnostic()?.duration ?? 0,
          ...(status === "failed"
            ? {
                caseId,
                category:
                  result.state === "pending"
                    ? ("runner" as const)
                    : normalizeFailureCategory(error),
                ...((tier === "example" || !this.#redactDerived) &&
                error?.message
                  ? { message: String(error.message) }
                  : {}),
              }
            : {}),
        };
        this.results.push(record);
        this.allowPublicRecord(record);
      }
      if (!module.ok() && module.children.size === 0) {
        const record: TestResultRecord = {
          file: modulePath,
          tier: "derived",
          status: "failed",
          caseId: sha256Bytes(modulePath).slice(7, 23),
          category: "collection",
          durationMs: module.diagnostic().duration,
        };
        this.results.push(record);
        this.allowPublicRecord(record);
      }
    }
    for (const [index, error] of unhandledErrors.entries()) {
      this.heldOut.observe(error);
      const record: TestResultRecord = {
        file: "<runner>",
        tier: "derived",
        status: "failed",
        caseId: sha256Bytes(`runner:${index}`).slice(7, 23),
        category: "runner",
        durationMs: 0,
        ...(!this.#redactDerived && error.message
          ? { message: String(error.message) }
          : {}),
      };
      this.results.push(record);
      this.allowPublicRecord(record);
    }
    this.results.sort((left, right) =>
      `${left.file}\0${left.caseId ?? ""}`.localeCompare(
        `${right.file}\0${right.caseId ?? ""}`,
      ),
    );
  }

  assertNoHeldOutLeak(value: unknown): void {
    this.heldOut.assertSafe(value);
  }
}
