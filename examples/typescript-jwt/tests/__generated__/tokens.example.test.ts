// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:99a11c364da059dfc6909ca9153694b3fae8b35459c7ec0dcc107c8f17258ca5
// jaunt:vitest_fingerprint=sha256:5ef9c3f603f2a5e5aa0967833c5876ba1374afb318d3777d80f2d86c1fb0a905
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:b67fbe59a52a7c28951c4b2fe342f7db3e0e2caae9d229190451f4f5fda7ce5c
// jaunt:prompt_fingerprint=sha256:a274f34bea91b04218014d8c915efe6bb2754c16a073fe67fa11bba30bcf22f5
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:60b967e264c099a25e62a373d86feba7dc4bfb362c84caaf4f433bbde6521ebc
// jaunt:body_digest=sha256:98945b527685bbef846f1d7e0cf0215d550b2c23760c14d167377003ff7a5a4d

import { expect, vi } from "vitest";

import { createToken, rotateToken, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

const FIXED_NOW_SECONDS = 1_700_000_000;

function errorCode(action: () => unknown): unknown {
  try {
    action();
  } catch (error: unknown) {
    return error !== null && typeof error === "object" && "code" in error
      ? error.code
      : undefined;
  }
  return undefined;
}

test("roundtrips a created token for user-42", () => {
  const dateNow = vi.spyOn(Date, "now").mockReturnValue(FIXED_NOW_SECONDS * 1000);
  try {
    const token = createToken("user-42", "s3cret");
    const claims = verifyToken(token, "s3cret");

    expect(claims.sub).toBe("user-42");
    expect(claims.exp).toBeGreaterThan(claims.iat);
  } finally {
    dateNow.mockRestore();
  }
});

test("rejects verification with a different secret", () => {
  const dateNow = vi.spyOn(Date, "now").mockReturnValue(FIXED_NOW_SECONDS * 1000);
  try {
    const token = createToken("user-42", "s3cret");

    expect(errorCode(() => verifyToken(token, "different-secret"))).toBe(
      "invalid-signature",
    );
  } finally {
    dateNow.mockRestore();
  }
});

test("rotation preserves the subject and advances both timestamps", () => {
  const dateNow = vi.spyOn(Date, "now").mockReturnValue(FIXED_NOW_SECONDS * 1000);
  try {
    const token = createToken("user-42", "s3cret");
    const originalClaims = verifyToken(token, "s3cret");
    const rotatedClaims = verifyToken(rotateToken(token, "s3cret"), "s3cret");

    expect(rotatedClaims.sub).toBe(originalClaims.sub);
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
  } finally {
    dateNow.mockRestore();
  }
});
