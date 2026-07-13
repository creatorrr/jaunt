import { expect, test } from "vitest";
import { HeldOutLeakError, HeldOutLeakGuard } from "../../src/test/heldout.js";
import { projectTestResults } from "../../src/test/runner.js";

test("held-out guard recursively detects every sensitive error and output surface", () => {
  const cause = new Error("CAUSE-SENTINEL");
  const aggregate = new AggregateError(
    [cause, new Error("AGGREGATE-CHILD-SENTINEL")],
    "MESSAGE-SENTINEL",
    { cause },
  );
  aggregate.stack = "STACK-SENTINEL";
  Object.assign(aggregate, {
    diff: "DIFF-SENTINEL",
    snapshot: "SNAPSHOT-SENTINEL",
    serializedError: { detail: "SERIALIZED-SENTINEL" },
  });
  const guard = new HeldOutLeakGuard();
  guard.observe(aggregate);
  guard.observe({
    stdout: "STDOUT-SENTINEL",
    stderr: "STDERR-SENTINEL",
    warnings: ["WARNING-SENTINEL"],
    setup: "SETUP-SENTINEL",
    teardown: "TEARDOWN-SENTINEL",
    config: "CONFIG-SENTINEL",
  });

  for (const sentinel of [
    "MESSAGE-SENTINEL",
    "STACK-SENTINEL",
    "DIFF-SENTINEL",
    "SNAPSHOT-SENTINEL",
    "CAUSE-SENTINEL",
    "AGGREGATE-CHILD-SENTINEL",
    "SERIALIZED-SENTINEL",
    "STDOUT-SENTINEL",
    "STDERR-SENTINEL",
    "WARNING-SENTINEL",
    "SETUP-SENTINEL",
    "TEARDOWN-SENTINEL",
    "CONFIG-SENTINEL",
  ]) {
    let thrown: unknown;
    try {
      guard.assertSafe({ detail: `prefix:${sentinel}:suffix` });
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(HeldOutLeakError);
    expect(String(thrown)).not.toContain(sentinel);
  }
});

test("held-out guard permits the explicit opaque DTO", () => {
  const guard = new HeldOutLeakGuard();
  guard.observe(new Error("PRIVATE-SENTINEL"));
  const dto = {
    caseId: "opaque-case",
    category: "assertion",
  };
  guard.allow(dto);
  expect(() => guard.assertSafe(dto)).not.toThrow();
});

test("protected derived records project to the exact two-field allowlist", () => {
  const projected = projectTestResults(
    [
      {
        file: "FILENAME-SENTINEL.derived.test.ts",
        tier: "derived",
        status: "failed",
        caseId: "0123456789abcdef",
        category: "assertion",
        durationMs: 86_400_000,
        message: "MESSAGE-SENTINEL",
      },
      {
        file: "PASSING-FILENAME-SENTINEL.derived.test.ts",
        tier: "derived",
        status: "passed",
        durationMs: 1,
      },
    ],
    true,
  );

  expect(projected).toEqual([
    { caseId: "0123456789abcdef", category: "assertion" },
  ]);
  const rendered = JSON.stringify(projected);
  for (const sentinel of [
    "FILENAME-SENTINEL",
    "PASSING-FILENAME-SENTINEL",
    "MESSAGE-SENTINEL",
    "durationMs",
    "tier",
    "status",
    "file",
  ]) {
    expect(rendered).not.toContain(sentinel);
  }
});
