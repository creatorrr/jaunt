import { createRequire } from "node:module";
import { describe, expect, test } from "vitest";
import * as esm from "../../dist/spec.js";

const require = createRequire(import.meta.url);
const cjs = require("../../dist/spec.cjs") as typeof esm;

for (const [format, api] of [
  ["ESM", esm],
  ["CommonJS", cjs],
] as const) {
  describe(`${format} marker runtime`, () => {
    for (const marker of ["magicModule", "magic", "testSpec"] as const) {
      test(`${marker} rejects execution`, () => {
        expect(() => api[marker]({ targets: [] } as never)).toThrowError(
          api.JauntNotBuiltError,
        );
        try {
          api[marker]({ targets: [] } as never);
        } catch (error) {
          expect(error).toMatchObject({ code: "JAUNT_NOT_BUILT" });
          expect((error as Error).message).toContain("jaunt build");
        }
      });
    }
  });
}
