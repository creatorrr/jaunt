// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with `jaunt reconcile`.
// jaunt:source=src/tokens/b64url.ts
// jaunt:source_digest=sha256:0438748771e2aee3474d083c87b5aaf8ab3f72d3b41fcc561d3a811df6079a0c
// jaunt:property_scheme=jaunt-ts-property/2
// jaunt:property_digest=sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
// jaunt:body_digest=sha256:19a959dcea5d4d4936622e287be6f070650e49d1bd1fa9c6a8fe5cc96eecab01
// jaunt:strength_scheme=jaunt-ts-mutation/1
// jaunt:strength=5/5
// jaunt:strength_excluded=1
// jaunt:strength_concurrency=1
// jaunt:strength_cases=[{"column":55,"id":"001:constant:33:55","kind":"constant","line":33,"outcome":"killed","reason":"test-failed"},{"column":57,"id":"002:comparison:33:57","kind":"comparison","line":33,"outcome":"killed","reason":"test-failed"},{"column":61,"id":"003:constant:33:61","kind":"constant","line":33,"outcome":"killed","reason":"test-failed"},{"column":5,"id":"004:throw:34:5","kind":"throw","line":34,"outcome":"killed","reason":"test-failed"},{"column":3,"id":"005:return:36:3","kind":"return","line":36,"outcome":"killed","reason":"test-failed"},{"column":43,"id":"006:constant:36:43","kind":"constant","line":36,"outcome":"excluded","reason":"did-not-compile"}]

import { describe, expect, test } from "vitest";

import { decode } from "../../../../src/tokens/b64url.js";

describe("decode", () => {
  test("decodes the documented unpadded base64url example", () => {
    expect(decode("aGk")).toEqual(new Uint8Array([104, 105]));
  });

  test.each([
    ["", []],
    ["Zg", [102]],
    ["Zm9v", [102, 111, 111]],
    ["-_8", [251, 255]],
  ] as const)("decodes valid unpadded base64url text %j", (text, bytes) => {
    expect(decode(text)).toEqual(new Uint8Array(bytes));
  });

  test.each(["=", "aGk=", "a+b", "a/b", "a b", "a.b", "\u00e9"])(
    "throws TypeError for characters outside the base64url alphabet in %j",
    (text) => {
      expect(() => decode(text)).toThrow(TypeError);
    },
  );

  test.each(["A", "AAAAA", "AAAAAAAAA"])(
    "throws TypeError for impossible unpadded length in %j",
    (text) => {
      expect(() => decode(text)).toThrow(TypeError);
    },
  );
});
