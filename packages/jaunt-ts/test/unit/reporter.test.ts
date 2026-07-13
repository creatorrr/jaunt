import { createHash } from "node:crypto";
import { resolve } from "node:path";
import { expect, test } from "vitest";
import { classifyTier, reporterModulePath } from "../../src/test/reporter.js";

const GENERATED_TEST_HEADER =
  "// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.";

function managedExample(body: string): string {
  const canonicalBody = `${body.trim()}\n`;
  const digest = createHash("sha256").update(canonicalBody).digest("hex");
  return `${GENERATED_TEST_HEADER}
// jaunt:tier=example
// jaunt:source=tests/example.jaunt-test.ts
// jaunt:body_digest=sha256:${digest}

${canonicalBody}`;
}

test("reporter derives Vitest 3 module paths relative to the Jaunt root", () => {
  const root = resolve("fixture-workspace");

  expect(
    reporterModulePath(root, {
      moduleId: resolve(root, "tests/example.test.ts"),
    }),
  ).toBe("tests/example.test.ts");
});

test("reporter preserves Vitest 4 relative module IDs", () => {
  expect(
    reporterModulePath(resolve("fixture-workspace"), {
      moduleId: "/unused/absolute/module.test.ts",
      relativeModuleId: "custom\\module.test.ts",
    }),
  ).toBe("custom/module.test.ts");
});

test("reporter trusts only an intact managed header for the example tier", () => {
  const file = "/workspace/tests/__generated__/auth.example.test.ts";
  const valid = managedExample('test("authored", () => {});');

  expect(classifyTier(file, valid)).toBe("example");
  expect(
    classifyTier(file, `test("held out", () => {});\n// jaunt:tier=example\n`),
  ).toBe("derived");
  expect(
    classifyTier(
      file,
      valid.replace(
        "// jaunt:source=tests/example.jaunt-test.ts",
        "// jaunt:tier=example",
      ),
    ),
  ).toBe("derived");
  expect(classifyTier(file, `${valid}test("tampered", () => {});\n`)).toBe(
    "derived",
  );
  expect(
    classifyTier(
      file,
      valid.replace("\n\n", "\n// arbitrary comment before the separator\n\n"),
    ),
  ).toBe("derived");
});

test("example provenance does not upgrade a derived filename", () => {
  expect(
    classifyTier(
      "/workspace/tests/__generated__/auth.derived.test.ts",
      managedExample('test("held out", () => {});'),
    ),
  ).toBe("derived");
});
