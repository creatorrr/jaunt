import { mkdirSync, mkdtempSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, resolve } from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const require = createRequire(import.meta.url);
export const packageRoot = resolve(
  dirname(fileURLToPath(import.meta.url)),
  "../..",
);

function write(root: string, path: string, content: string): void {
  const target = resolve(root, path);
  mkdirSync(dirname(target), { recursive: true });
  writeFileSync(target, content);
}

export interface FixtureWorkspace {
  readonly root: string;
  readonly compilerModulePath: string;
}

export function createFixtureWorkspace(
  options: {
    withClass?: boolean;
    withTestSpec?: boolean;
    compilerPackage?: "@typescript/typescript6" | "@typescript/typescript58";
  } = {},
): FixtureWorkspace {
  const root = mkdtempSync(resolve(tmpdir(), "jaunt-ts-"));
  mkdirSync(resolve(root, "node_modules/@usejaunt"), { recursive: true });
  symlinkSync(packageRoot, resolve(root, "node_modules/@usejaunt/ts"), "dir");
  const compilerPackage = resolve(
    dirname(
      require.resolve(
        `${options.compilerPackage ?? "@typescript/typescript6"}/package.json`,
      ),
    ),
  );
  symlinkSync(compilerPackage, resolve(root, "node_modules/typescript"), "dir");
  symlinkSync(
    resolve(dirname(require.resolve("vitest/package.json"))),
    resolve(root, "node_modules/vitest"),
    "dir",
  );
  const compilerModulePath = resolve(
    root,
    "node_modules/typescript/lib/typescript.js",
  );
  write(
    root,
    "package.json",
    JSON.stringify(
      {
        name: "fixture",
        private: true,
        type: "module",
        devDependencies: {
          typescript:
            options.compilerPackage === "@typescript/typescript58"
              ? "5.8.3"
              : "6.0.2",
          vitest: "4.1.10",
        },
      },
      null,
      2,
    ),
  );
  write(
    root,
    "tsconfig.json",
    `${JSON.stringify(
      {
        compilerOptions: {
          target: "ES2022",
          module: "NodeNext",
          moduleResolution: "NodeNext",
          strict: true,
          noEmit: true,
          exactOptionalPropertyTypes: true,
          types: [],
        },
        include: ["src/**/*.ts"],
        exclude: [
          "src/**/*.jaunt.ts",
          "src/**/*.jaunt-test.ts",
          "src/**/__generated__/**",
        ],
      },
      null,
      2,
    )}\n`,
  );
  write(
    root,
    "src/slug/index.jaunt.ts",
    `import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Trim, lowercase, and replace whitespace runs with one dash. */
export function slugify(title: string): string {
  return jaunt.magic();
}
`,
  );
  write(
    root,
    "src/app.ts",
    `import { slugify } from "./slug/index.js";
export const result = slugify("Hello World");
`,
  );
  if (options.withClass) {
    write(
      root,
      "src/store/index.jaunt.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
jaunt.magicModule();
/** A string store. */
export class Store {
  constructor(prefix?: string) { jaunt.magic(); }
  /** Store a value. */
  put(key: string, value: string): void { jaunt.magic(); }
  /** Read a value. */
  get(key: string): string | null { return jaunt.magic(); }
  /** Number of values. */
  get size(): number { return jaunt.magic(); }
}
`,
    );
  }
  if (options.withTestSpec) {
    write(
      root,
      "tsconfig.test.json",
      `${JSON.stringify(
        {
          extends: "./tsconfig.json",
          compilerOptions: { noEmit: true },
          include: ["tests/**/*.ts"],
          exclude: ["tests/**/*.jaunt-test.ts"],
        },
        null,
        2,
      )}\n`,
    );
    write(
      root,
      "tests/slug.jaunt-test.ts",
      `import * as jaunt from "@usejaunt/ts/spec";
import { slugify } from "../src/slug/index.jaunt.js";
jaunt.magicModule();
/** Slugifies a title. */
export function slugifies(): never {
  return jaunt.testSpec({ targets: [slugify] });
}
`,
    );
  }
  return { root, compilerModulePath };
}
