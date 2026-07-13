import { expect, test } from "vitest";
import {
  propertySeed,
  renderTypedProperty,
} from "../../src/test/properties.js";

test("fast-check rendering is typed and deterministically seeded", () => {
  expect(propertySeed("case-a")).toBe(propertySeed("case-a"));
  expect(propertySeed("case-a")).not.toBe(propertySeed("case-b"));
  expect(
    renderTypedProperty({
      name: "value",
      expectedType: "string",
      arbitrary: "fc.string()",
      predicate: "(value) => value.length >= 0",
      caseDigest: "case-a",
      numRuns: 50,
    }),
  ).toContain("const valueArbitrary: fc.Arbitrary<string> = fc.string();");
});
