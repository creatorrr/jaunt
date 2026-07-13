// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived (from the spec module's @throws tags and contract prose)
//
// Held-out tier: the custom vitest reporter (the port's replacement for the
// `jaunt.heldout` pytest plugin) redacts this tier's failure detail — name
// and error class only — before it reaches the implementer's repair loop,
// preserving the implementer/tester barrier.
import { createHmac } from "node:crypto";

import { expect, test } from "vitest";

import { JwtError, createToken, rotateToken, verifyToken } from "../../src/tokens/specs.ts";

const SECRET = "s3cret";

/** Mint a validly-signed token with an arbitrary header/payload. */
function signRaw(header: unknown, payload: unknown, secret: string): string {
  const b64 = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  const input = `${b64(header)}.${b64(payload)}`;
  return `${input}.${createHmac("sha256", secret).update(input).digest("base64url")}`;
}

const HS256 = { alg: "HS256", typ: "JWT" };
const LIVE_CLAIMS = { sub: "user-42", iat: 1, exp: 9_999_999_999 };

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

test("validly-signed non-HS256 header rejects as malformed", () => {
  const token = signRaw({ alg: "none", typ: "JWT" }, LIVE_CLAIMS, SECRET);
  const err = capture(() => verifyToken(token, SECRET));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("malformed");
});

test("validly-signed payload with extra fields rejects as malformed", () => {
  const token = signRaw(HS256, { ...LIVE_CLAIMS, role: "admin" }, SECRET);
  const err = capture(() => verifyToken(token, SECRET));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("malformed");
});

test("fractional ttlSeconds is truncated to whole seconds and verifies", () => {
  const token = createToken("user-42", SECRET, { ttlSeconds: 90.9 });
  const claims = verifyToken(token, SECRET);
  expect(Number.isInteger(claims.iat)).toBe(true);
  expect(Number.isInteger(claims.exp)).toBe(true);
  expect(claims.exp - claims.iat).toBe(90);
});

test("rotation with a shorter ttl still strictly advances exp", () => {
  const t1 = createToken("user-7", SECRET); // default ttl: 3600
  const t2 = rotateToken(t1, SECRET, { ttlSeconds: 60 });
  const c1 = verifyToken(t1, SECRET);
  const c2 = verifyToken(t2, SECRET);
  expect(c2.iat).toBeGreaterThan(c1.iat);
  expect(c2.exp).toBeGreaterThan(c1.exp);
});
