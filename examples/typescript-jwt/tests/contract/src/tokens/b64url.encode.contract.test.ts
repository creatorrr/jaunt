// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with `jaunt reconcile`.
// jaunt:source=src/tokens/b64url.ts
// jaunt:source_digest=sha256:0438748771e2aee3474d083c87b5aaf8ab3f72d3b41fcc561d3a811df6079a0c
// jaunt:property_scheme=jaunt-ts-property/2
// jaunt:property_digest=sha256:490814509e5610d6152e6bbb4e049571a58ecc96379e4bace8f6dd36ef7cbea3
// jaunt:body_digest=sha256:83871aa231561f7b61be87a682fa323dbbdf0b00671bb50c58d2e8db2c85bede
// jaunt:strength_scheme=jaunt-ts-mutation/1
// jaunt:strength=1/1
// jaunt:strength_excluded=1
// jaunt:strength_concurrency=1
// jaunt:strength_cases=[{"column":3,"id":"001:return:20:3","kind":"return","line":20,"outcome":"killed","reason":"test-failed"},{"column":38,"id":"002:constant:20:38","kind":"constant","line":20,"outcome":"excluded","reason":"did-not-compile"}]

import * as fc from "fast-check";
import { expect as __jauntPropertyExpect } from "vitest";
import { test as __jauntPropertyTest } from "vitest";
import * as __jauntPropertyTarget0 from "../../../../src/tokens/b64url.js";

const __jauntPropertyArbitrary_123222871502808b: fc.Arbitrary<Uint8Array> = fc.uint8Array();
__jauntPropertyTest("@prop prop-123222871502808b: decode(encode(bytes)) equals bytes", () => {
  fc.assert(
    fc.property(
      __jauntPropertyArbitrary_123222871502808b,
      (bytes) => {
        __jauntPropertyExpect(__jauntPropertyTarget0.decode(__jauntPropertyTarget0.encode(bytes))).toEqual(bytes);
      },
    ),
    { seed: 575601522, numRuns: 50 },
  );
});

import { describe, expect, test } from "vitest";

import { encode } from "../../../../src/tokens/b64url.js";

describe("encode", () => {
  test("encodes bytes as unpadded base64url", () => {
    expect(encode(new Uint8Array([104, 105]))).toBe("aGk");
  });

  test("encodes an empty byte array as an empty string", () => {
    expect(encode(new Uint8Array([]))).toBe("");
  });
});
