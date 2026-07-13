#!/usr/bin/env node
import { realpathSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { resolve } from "node:path";
import { normalizeError } from "../protocol/errors.js";
import { failure, parseRequest } from "../protocol/messages.js";
import { WorkerServer } from "./server.js";

export const MAX_MESSAGE_BYTES = 16 * 1024 * 1024;

interface BoundedLine {
  readonly line?: string;
  readonly tooLarge: boolean;
}

async function* boundedLines(
  input: AsyncIterable<string | Uint8Array>,
  maxMessageBytes: number,
): AsyncGenerator<BoundedLine> {
  let chunks: Buffer[] = [];
  let length = 0;
  let discarding = false;

  function append(chunk: Buffer): void {
    if (discarding) return;
    if (length + chunk.length > maxMessageBytes) {
      chunks = [];
      length = 0;
      discarding = true;
      return;
    }
    if (chunk.length > 0) chunks.push(chunk);
    length += chunk.length;
  }

  function finish(): BoundedLine {
    if (discarding) {
      chunks = [];
      length = 0;
      discarding = false;
      return { tooLarge: true };
    }
    const bytes = Buffer.concat(chunks, length);
    chunks = [];
    length = 0;
    const end = bytes.at(-1) === 0x0d ? bytes.length - 1 : bytes.length;
    return { line: bytes.subarray(0, end).toString("utf8"), tooLarge: false };
  }

  for await (const raw of input) {
    const buffer = Buffer.isBuffer(raw) ? raw : Buffer.from(raw);
    let start = 0;
    while (start < buffer.length) {
      const newline = buffer.indexOf(0x0a, start);
      if (newline < 0) {
        append(buffer.subarray(start));
        break;
      }
      append(buffer.subarray(start, newline));
      yield finish();
      start = newline + 1;
    }
  }
  if (discarding || length > 0) yield finish();
}

export async function runWorker(
  input: AsyncIterable<string | Uint8Array> = process.stdin,
  output: { write(chunk: string): unknown } = process.stdout,
  maxMessageBytes = MAX_MESSAGE_BYTES,
): Promise<void> {
  const server = new WorkerServer();
  for await (const record of boundedLines(input, maxMessageBytes)) {
    let id = "unknown";
    let response;
    try {
      if (record.tooLarge) {
        throw Object.assign(new Error("Protocol message exceeds 16 MiB"), {
          code: "MESSAGE_TOO_LARGE",
        });
      }
      const line = record.line ?? "";
      const decoded = JSON.parse(line) as unknown;
      if (
        decoded &&
        typeof decoded === "object" &&
        "id" in decoded &&
        typeof decoded.id === "string"
      ) {
        id = decoded.id;
      }
      const request = parseRequest(decoded);
      response = await server.dispatch(request);
    } catch (error) {
      const normalized = normalizeError(error);
      response = failure(
        id,
        normalized.code === "INTERNAL_ERROR" &&
          (error as { code?: string }).code === "MESSAGE_TOO_LARGE"
          ? {
              ...normalized,
              code: "MESSAGE_TOO_LARGE",
              message: "Protocol message exceeds 16 MiB",
            }
          : normalized,
      );
    }
    output.write(`${JSON.stringify(response)}\n`);
    if (server.shutdownRequested) break;
  }
}

function comparableEntryPath(path: string): string {
  try {
    return realpathSync(path);
  } catch {
    return resolve(path);
  }
}

const invokedPath = process.argv[1] ? comparableEntryPath(process.argv[1]) : "";
if (invokedPath === comparableEntryPath(fileURLToPath(import.meta.url))) {
  runWorker().catch((error: unknown) => {
    process.stderr.write(
      `${error instanceof Error ? (error.stack ?? error.message) : String(error)}\n`,
    );
    process.exitCode = 1;
  });
}
