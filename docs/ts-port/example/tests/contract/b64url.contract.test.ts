// ⚙️ jaunt:contract-battery — derived from src/tokens/b64url.ts.
// DO NOT EDIT; regenerate with `jaunt reconcile`. `jaunt check` verifies
// this battery offline (no model call), same as the Python port.
// jaunt:derived-from=tokens/b64url:encode,tokens/b64url:decode
// jaunt:prose-digest=sha256:0000000000000000000000000000000000000000000000000000000000000000
// jaunt:tool-version=0.0.0-ts-preview
import * as fc from "fast-check";
import { expect, test } from "vitest";

import { decode, encode } from "../../src/tokens/b64url.ts";

// --- cases derived from @example tags ---

test("encode: [104, 105] -> aGk", () => {
  expect(encode(new Uint8Array([104, 105]))).toBe("aGk");
});

test("encode: empty -> empty string", () => {
  expect(encode(new Uint8Array([]))).toBe("");
});

test("decode: aGk -> [104, 105]", () => {
  expect(decode("aGk")).toEqual(new Uint8Array([104, 105]));
});

// --- cases derived from @throws tags ---

test.each(["a=b", "abc=", "not base64url!", "with space ", "A", "AAAAA"])(
  "decode rejects invalid input %j with TypeError",
  (text) => {
    expect(() => decode(text)).toThrowError(TypeError);
  },
);

// --- case derived from the @prop bullet ---
//
// `given bytes: fc.uint8Array() :: decode(encode(bytes)) equals bytes`
// The seed is derived from the case digest at reconcile time, so `jaunt
// check` replays the identical run — fast-check's answer to Hypothesis
// `derandomize=True + database=None`, with no shrink-cache to redirect.
test("property: decode(encode(bytes)) equals bytes", () => {
  fc.assert(
    fc.property(fc.uint8Array(), (bytes) => {
      expect(decode(encode(bytes))).toEqual(bytes);
    }),
    { seed: 0x6a61756e, numRuns: 50 },
  );
});
