import assert from "node:assert/strict";
import { createRequire } from "node:module";
import test from "node:test";

import * as esm from "../index.js";

const require = createRequire(import.meta.url);
const cjs = require("../index.cjs");

for (const [format, api] of [
  ["ESM", esm],
  ["CommonJS", cjs],
]) {
  for (const marker of ["magicModule", "magic", "testSpec"]) {
    test(`${format} ${marker} rejects runtime execution`, () => {
      assert.throws(
        () => api[marker](),
        (error) =>
          error instanceof api.JauntNotBuiltError &&
          error.code === "JAUNT_NOT_BUILT" &&
          error.message.includes("jaunt build"),
      );
    });
  }
}
