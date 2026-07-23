import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterEach, describe, expect, test, vi } from "vitest";
import { digestCanonical } from "../../src/analyzer/canonical.js";
import {
  groupSemanticEnvironmentRecords,
  stablePathId,
} from "../../src/analyzer/type_environment.js";

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

  test("groups declaration files by package and deduplicates unresolved imports", () => {
    expect(
      groupSemanticEnvironmentRecords([
        { id: "package:@types/node/assert.d.ts", digest: "sha256:assert" },
        { id: "package:@types/node/fs.d.ts", digest: "sha256:fs" },
        { id: "package:vite/client.d.ts", digest: "sha256:vite" },
        { id: "unresolved-module:node:fs", digest: "sha256:missing" },
        { id: "unresolved-module:node:fs", digest: "sha256:missing" },
        { id: "workspace:src/types.ts", digest: "sha256:workspace" },
      ]).map((record) => record.id),
    ).toEqual([
      "package:@types/node",
      "package:vite",
      "unresolved-modules",
      "workspace:src/types.ts",
    ]);
  });

  test("groups Unicode record IDs by code units rather than locale", () => {
    const localeCompare = vi
      .spyOn(String.prototype, "localeCompare")
      .mockImplementation(function (this: string, other: string): number {
        const left = String(this);
        const right = String(other);
        return left < right ? 1 : left > right ? -1 : 0;
      });
    const grouped = (() => {
      try {
        return groupSemanticEnvironmentRecords([
          { id: "workspace:src/ä.ts", digest: "sha256:workspace-umlaut" },
          { id: "workspace:src/z.ts", digest: "sha256:workspace-z" },
          { id: "package:demo/ä.d.ts", digest: "sha256:package-umlaut" },
          { id: "package:demo/z.d.ts", digest: "sha256:package-z" },
        ]);
      } finally {
        localeCompare.mockRestore();
      }
    })();

    expect(grouped.map((record) => record.id)).toEqual([
      "package:demo",
      "workspace:src/z.ts",
      "workspace:src/ä.ts",
    ]);
    expect(grouped[0]?.digest).toBe(
      digestCanonical([
        { id: "package:demo/z.d.ts", digest: "sha256:package-z" },
        {
          id: "package:demo/ä.d.ts",
          digest: "sha256:package-umlaut",
        },
      ]),
    );
  });
});
