import { describe, expect, test } from "vitest";
import {
  canonicalCompilerOptions,
  compilerOptionsHash,
} from "../../src/analyzer/compiler_options.js";

describe("compiler-options path identity", () => {
  test("normalizes Windows drives, separators, arrays, maps, and custom paths", () => {
    const options = (root: string, externalDrive: string) => ({
      rootDir: `${root}\\src`,
      outDir: `${root}\\dist`,
      rootDirs: [`${root}\\src`, `${root}\\..\\shared`],
      pathsBasePath: root,
      paths: {
        "@app/*": [`${root}\\src\\*`, "..\\shared\\*"],
      },
      plugins: [
        {
          name: "fixture-transformer",
          customPath: `${root}\\tools\\plugin.js`,
          cacheDirectory: `${externalDrive}:\\jaunt-cache\\fixture`,
        },
      ],
      configFilePath: `${root}\\tsconfig.json`,
    });

    const left = options("C:\\work\\repo\\packages\\app", "D");
    const right = options("E:\\work\\repo\\packages\\app", "F");
    expect(
      compilerOptionsHash(
        "C:\\work\\repo",
        "C:\\work\\repo\\packages\\app\\tsconfig.json",
        left,
      ),
    ).toBe(
      compilerOptionsHash(
        "E:\\work\\repo",
        "E:\\work\\repo\\packages\\app\\tsconfig.json",
        right,
      ),
    );

    const canonical = JSON.stringify(
      canonicalCompilerOptions(
        "C:\\work\\repo",
        "C:\\work\\repo\\packages\\app\\tsconfig.json",
        left,
      ),
    );
    expect(canonical).not.toContain("C:\\\\work");
    expect(canonical).not.toContain("D:\\\\jaunt-cache");
    expect(canonical).toContain("<relative>/../shared/*");

    expect(
      compilerOptionsHash(
        "C:\\work\\repo",
        "C:\\work\\repo\\packages\\app\\tsconfig.json",
        left,
      ),
    ).not.toBe(
      compilerOptionsHash(
        "C:\\work\\repo",
        "C:\\work\\repo\\packages\\app\\tsconfig.json",
        {
          ...left,
          rootDir: "C:\\work\\repo\\packages\\app\\source",
        },
      ),
    );

    const originalHash = compilerOptionsHash(
      "C:\\work\\repo",
      "C:\\work\\repo\\packages\\app\\tsconfig.json",
      left,
    );
    for (const changed of [
      {
        ...left,
        rootDirs: [
          "C:\\work\\repo\\packages\\app\\src",
          "C:\\work\\repo\\shared-v2",
        ],
      },
      {
        ...left,
        paths: {
          "@app/*": ["C:\\work\\repo\\packages\\app\\source\\*"],
        },
      },
      {
        ...left,
        plugins: [
          {
            ...left.plugins[0],
            customPath: "C:\\work\\repo\\packages\\app\\tools\\plugin-v2.js",
          },
        ],
      },
    ]) {
      expect(
        compilerOptionsHash(
          "C:\\work\\repo",
          "C:\\work\\repo\\packages\\app\\tsconfig.json",
          changed,
        ),
      ).not.toBe(originalHash);
    }
  });

  test("normalizes POSIX copies but retains meaningful outside-root changes", () => {
    const options = (root: string, shared: string) => ({
      baseUrl: root,
      typeRoots: [`${root}/types`, shared],
      configFilePath: `${root}/tsconfig.json`,
      custom: { schemaFile: `${root}/schemas/options.json` },
    });
    const left = options("/tmp/checkout-a/app", "/tmp/checkout-a/shared/types");
    const right = options(
      "/opt/worktrees/checkout-b/app",
      "/opt/worktrees/checkout-b/shared/types",
    );
    expect(
      compilerOptionsHash(
        "/tmp/checkout-a",
        "/tmp/checkout-a/app/tsconfig.json",
        left,
      ),
    ).toBe(
      compilerOptionsHash(
        "/opt/worktrees/checkout-b",
        "/opt/worktrees/checkout-b/app/tsconfig.json",
        right,
      ),
    );
    expect(
      compilerOptionsHash(
        "/tmp/checkout-a",
        "/tmp/checkout-a/app/tsconfig.json",
        left,
      ),
    ).not.toBe(
      compilerOptionsHash(
        "/tmp/checkout-a",
        "/tmp/checkout-a/app/tsconfig.json",
        options("/tmp/checkout-a/app", "/tmp/checkout-a/shared-v2/types"),
      ),
    );
  });

  test("keeps a fixed external absolute target stable across checkout depth", () => {
    const external = "/opt/shared-types";
    const left = compilerOptionsHash(
      "/tmp/checkout-a",
      "/tmp/checkout-a/app/tsconfig.json",
      { typeRoots: [external] },
    );
    const right = compilerOptionsHash(
      "/opt/worktrees/checkout-b",
      "/opt/worktrees/checkout-b/app/tsconfig.json",
      { typeRoots: [external] },
    );
    expect(right).toBe(left);

    expect(
      compilerOptionsHash(
        "/tmp/checkout-a",
        "/tmp/checkout-a/app/tsconfig.json",
        { typeRoots: ["/opt/other-types"] },
      ),
    ).not.toBe(left);

    expect(
      canonicalCompilerOptions(
        "/tmp/checkout-a",
        "/tmp/checkout-a/app/tsconfig.json",
        {
          rootDir: "/tmp/checkout-a/app/src",
          typeRoots: [external],
        },
      ),
    ).toEqual({
      rootDir: "<workspace>/app/src",
      typeRoots: ["<external:posix>/opt/shared-types"],
    });
  });
});
