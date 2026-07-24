// ⚙️ jaunt:contract-battery — DO NOT EDIT. Regenerate with `jaunt reconcile`.
// jaunt:source=src/tokens/b64url.ts
// jaunt:source_digest=sha256:0438748771e2aee3474d083c87b5aaf8ab3f72d3b41fcc561d3a811df6079a0c
// jaunt:property_scheme=jaunt-ts-property/2
// jaunt:property_digest=sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855
// jaunt:fixture_path=tests/fixtures.ts
// jaunt:fixture_digest=sha256:c32ec5f5c70a85bd7592d29dad10ecc821f73933526c8d247cb5026feba316ac
// jaunt:fixture_topology={"tests/contract/fixtures.ts":"<missing>","tests/contract/fixtures.tsx":"<missing>","tests/contract/src/fixtures.ts":"<missing>","tests/contract/src/fixtures.tsx":"<missing>","tests/contract/src/tokens/fixtures.ts":"<missing>","tests/contract/src/tokens/fixtures.tsx":"<missing>","tests/fixtures.ts":"sha256:c32ec5f5c70a85bd7592d29dad10ecc821f73933526c8d247cb5026feba316ac","tests/fixtures.tsx":"<missing>"}
// jaunt:body_digest=sha256:b2c9e7b392a6a9b1be35c6cbc807d57b043e411cf642802c1f2e16b134aa4e07
// jaunt:strength_scheme=jaunt-ts-mutation/1
// jaunt:strength=5/5
// jaunt:strength_excluded=1
// jaunt:strength_concurrency=1
// jaunt:strength_cases=[{"column":55,"id":"001:constant:33:55","kind":"constant","line":33,"outcome":"killed","reason":"test-failed"},{"column":57,"id":"002:comparison:33:57","kind":"comparison","line":33,"outcome":"killed","reason":"test-failed"},{"column":61,"id":"003:constant:33:61","kind":"constant","line":33,"outcome":"killed","reason":"test-failed"},{"column":5,"id":"004:throw:34:5","kind":"throw","line":34,"outcome":"killed","reason":"test-failed"},{"column":3,"id":"005:return:36:3","kind":"return","line":36,"outcome":"killed","reason":"test-failed"},{"column":43,"id":"006:constant:36:43","kind":"constant","line":36,"outcome":"excluded","reason":"did-not-compile"}]

import { describe, expect, test } from "vitest";

import { decode } from "../../../../src/tokens/b64url.js";

describe("decode", () => {
  test("decodes the documented unpadded base64url example", () => {
    const decoded = decode("aGk");

    expect(decoded).toBeInstanceOf(Uint8Array);
    expect(Array.from(decoded)).toEqual([104, 105]);
  });

  test.each([
    ["padding", "aGk="],
    ["standard base64 plus", "aGk+"],
    ["standard base64 slash", "aGk/"],
    ["punctuation", "aGk!"],
    ["space", "a Gk"],
    ["newline", "aGk\n"],
    ["non-ASCII character", "aGké"],
  ])("throws TypeError for %s outside the base64url alphabet", (_label, text) => {
    expect(() => decode(text)).toThrow(TypeError);
  });

  test.each(["A", "abcde", "ABCDEFGHI"])(
    "throws TypeError for impossible unpadded length: %s",
    (text) => {
      expect(() => decode(text)).toThrow(TypeError);
    },
  );
});
