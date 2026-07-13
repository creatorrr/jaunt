import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test } from "vitest";
import { stablePathId } from "../../src/analyzer/type_environment.js";

const roots: string[] = [];

afterEach(() => {
  for (const root of roots.splice(0))
    rmSync(root, { recursive: true, force: true });
});

describe("type-environment path identity", () => {
  test("canonicalizes npm and pnpm physical layouts to the same package path", () => {
    const root = mkdtempSync(join(tmpdir(), "jaunt-type-environment-"));
    roots.push(root);
    const npmPath = join(root, "node_modules", "undici-types", "client.d.ts");
    const pnpmPath = join(
      root,
      "node_modules",
      ".pnpm",
      "undici-types@8.3.0",
      "node_modules",
      "undici-types",
      "client.d.ts",
    );

    expect(stablePathId(root, npmPath)).toBe(
      "package:undici-types/client.d.ts",
    );
    expect(stablePathId(root, pnpmPath)).toBe(
      "package:undici-types/client.d.ts",
    );
    expect(stablePathId(root, join(root, "packages", "core", "index.ts"))).toBe(
      "workspace:packages/core/index.ts",
    );
  });
});
