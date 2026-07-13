import { mkdtempSync, mkdirSync, rmSync, symlinkSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, expect, test } from "vitest";
import { assertWithinRoot } from "../../src/analyzer/artifacts.js";

const cleanup: string[] = [];

afterEach(() => {
  for (const path of cleanup.splice(0))
    rmSync(path, { recursive: true, force: true });
});

test("path containment follows existing symlink ancestors", () => {
  const root = mkdtempSync(join(tmpdir(), "jaunt-root-"));
  const outside = mkdtempSync(join(tmpdir(), "jaunt-outside-"));
  cleanup.push(root, outside);
  mkdirSync(join(root, "src"));
  symlinkSync(outside, join(root, "src", "escaped"), "dir");

  expect(() =>
    assertWithinRoot(root, join(root, "src", "escaped", "generated.ts")),
  ).toThrow(/escapes workspace root/);
  expect(
    assertWithinRoot(root, join(root, "src", "nested", "generated.ts")),
  ).toBe(join(root, "src", "nested", "generated.ts"));
});
