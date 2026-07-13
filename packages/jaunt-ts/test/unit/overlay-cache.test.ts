import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import ts from "@typescript/typescript6";
import { afterEach, expect, test } from "vitest";
import { OverlayProgramCache } from "../../src/analyzer/overlay.js";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0)) {
    rmSync(root, { recursive: true, force: true });
  }
});

test("removing a virtual candidate reads the committed file instead of reusing it", () => {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-overlay-cache-"));
  roots.push(root);
  const path = resolve(root, "candidate.ts");
  const committed = "export const value = 'committed';\n";
  const candidate = "export const value = 'candidate';\n";
  writeFileSync(path, committed);
  const cache = new OverlayProgramCache();
  const options: ts.CompilerOptions = {
    target: ts.ScriptTarget.ES2022,
    module: ts.ModuleKind.ESNext,
    noEmit: true,
  };

  const proposed = cache.create(
    "project:native",
    ts,
    [path],
    options,
    new Map([[path, candidate]]),
  );
  expect(proposed.getSourceFile(path)?.text).toBe(candidate);

  const restored = cache.create(
    "project:native",
    ts,
    [path],
    options,
    new Map(),
  );
  expect(restored.getSourceFile(path)?.text).toBe(committed);
});
