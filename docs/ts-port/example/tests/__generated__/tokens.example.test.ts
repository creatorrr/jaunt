// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example (from authored test specs in tests/token-specs.ts)
//
// Tiering is by *filename* (`.example.` vs `.derived.`), replacing Python's
// `@pytest.mark.jaunt_tier` markers — jaunt writes these files, so it can
// route tiers to separate files instead of plumbing per-test marks. Failures
// in this tier reach the implementer's repair loop in full detail.
//
// These tests import the spec module path; the jaunt vitest plugin resolves
// it to the generated implementation, exactly as consumers experience it.
import { expect, test } from "vitest";

import { JwtError, createToken, rotateToken, verifyToken } from "../../src/tokens/specs.ts";

function capture(fn: () => unknown): unknown {
  try {
    fn();
  } catch (error) {
    return error;
  }
  throw new Error("expected the call to throw");
}

test("roundtrip create + verify", () => {
  const token = createToken("user-42", "s3cret");
  const claims = verifyToken(token, "s3cret");
  expect(claims.sub).toBe("user-42");
  expect(claims.exp).toBeGreaterThan(claims.iat);
});

test("wrong secret rejects with invalid-signature", () => {
  const token = createToken("user-42", "s3cret");
  const err = capture(() => verifyToken(token, "different"));
  expect(err).toBeInstanceOf(JwtError);
  expect((err as JwtError).code).toBe("invalid-signature");
});

test("rotation preserves subject and strictly advances timestamps", () => {
  const t1 = createToken("user-7", "s3cret");
  const t2 = rotateToken(t1, "s3cret");
  const c1 = verifyToken(t1, "s3cret");
  const c2 = verifyToken(t2, "s3cret");
  expect(c2.sub).toBe(c1.sub);
  expect(c2.iat).toBeGreaterThan(c1.iat);
  expect(c2.exp).toBeGreaterThan(c1.exp);
});
