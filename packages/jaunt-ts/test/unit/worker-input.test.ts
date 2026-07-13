import { PassThrough } from "node:stream";
import { expect, test } from "vitest";
import { runWorker } from "../../src/worker/main.js";

test("worker bounds an oversized input line and continues at the next frame", async () => {
  const input = new PassThrough();
  const output = new PassThrough();
  const chunks: Buffer[] = [];
  output.on("data", (chunk: Buffer) => chunks.push(chunk));

  const running = runWorker(input, output, 256);
  input.end(
    `${"x".repeat(1_024)}\n${JSON.stringify({
      protocol: "jaunt-ts/1-draft.2",
      id: "shutdown",
      method: "shutdown",
      params: {},
    })}\n`,
  );
  await running;

  const responses = Buffer.concat(chunks)
    .toString("utf8")
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
  expect(responses).toHaveLength(2);
  expect(responses[0]).toMatchObject({
    id: "unknown",
    ok: false,
    error: { code: "MESSAGE_TOO_LARGE" },
  });
  expect(responses[1]).toMatchObject({
    id: "shutdown",
    ok: true,
    result: { shutdown: true },
  });
});
