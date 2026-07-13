// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived (from the spec module's @throws tags and contract prose)
//
// Held-out tier: the custom vitest reporter (the port's replacement for the
// `jaunt.heldout` pytest plugin) redacts this tier's failure detail — name
// and error class only — before it reaches the implementer's repair loop,
// preserving the implementer/tester barrier.
import { expect, test } from "vitest";

import { JwtError, createToken, verifyToken } from "../../src/tokens/specs.ts";

const SECRET = "s3cret";

function capture(fn: () => unknown): unknown {
  try {
    fn();
  } catch (error) {
    return error;
  }
  throw new Error("expected the call to throw");
}

test.each([
  ["empty string", ""],
  ["one segment", "abc"],
  ["two segments", "abc.def"],
  ["four segments", "a.b.c.d"],
  ["padded segment", "aGVsbG8=.aGVsbG8.aGVsbG8"],
])("malformed token rejects: %s", (_label, token) => {
  const err = capture(() => verifyToken(token, SECRET));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("malformed");
});

test("expired token rejects with expired", () => {
  const token = createToken("user-42", SECRET, { ttlSeconds: -10 });
  const err = capture(() => verifyToken(token, SECRET));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("expired");
});

test("tampered payload rejects with invalid-signature", () => {
  const token = createToken("user-42", SECRET);
  const [header, , signature] = token.split(".");
  const forged = Buffer.from(
    JSON.stringify({ sub: "admin", iat: 1, exp: 9_999_999_999 }),
  ).toString("base64url");
  const err = capture(() => verifyToken(`${header}.${forged}.${signature}`, SECRET));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("invalid-signature");
});

test("empty userId rejects with RangeError", () => {
  expect(() => createToken("", SECRET)).toThrowError(RangeError);
});
