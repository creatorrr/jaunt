// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:d192a0f0d5f6705c9ca0854d669f6b4b4db585bd5629167fe8b87fab8c937660
// jaunt:fixture_fingerprint=sha256:184e0133ce415140efdb1a2a1515cb0e9494a882aaeb027b20e21182dbb785b7
// jaunt:vitest_fingerprint=sha256:bcf02994ff7e31dfd0b3a6a40ccd69cd082e7540cecf5e7951ea1e2a8fccb834
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:50d27e7718852bf96ddfee00be5ccf80c718da6d98af5326aabc1a952dfaf8db
// jaunt:prompt_fingerprint=sha256:a274f34bea91b04218014d8c915efe6bb2754c16a073fe67fa11bba30bcf22f5
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:4219c4f8656c9b573f521938df64149073e44c82fad0b90475b034aa0c5a5c95
// jaunt:body_digest=sha256:b3bb17af3f3819fb70443e06fbcf4d426d962da7d7467d7631687b9ac61a5d2c

import { expect, vi } from "vitest";

import { createToken, rotateToken, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

test("roundtrips a token for user-42", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2024-01-01T00:00:00.000Z"));

  try {
    const token = createToken("user-42", "s3cret");
    const claims = verifyToken(token, "s3cret");

    expect(claims.sub).toBe("user-42");
    expect(claims.exp).toBeGreaterThan(claims.iat);
  } finally {
    vi.useRealTimers();
  }
});

test("rejects a token verified with a different secret", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2024-01-01T00:00:00.000Z"));

  try {
    const token = createToken("user-42", "s3cret");
    let thrown: unknown;

    try {
      verifyToken(token, "different-secret");
    } catch (error: unknown) {
      thrown = error;
    }

    expect(thrown).toMatchObject({ code: "invalid-signature" });
  } finally {
    vi.useRealTimers();
  }
});

test("rotation preserves the subject and advances both timestamps", () => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date("2024-01-01T00:00:00.000Z"));

  try {
    const token = createToken("user-42", "s3cret");
    const originalClaims = verifyToken(token, "s3cret");
    const rotatedToken = rotateToken(token, "s3cret");
    const rotatedClaims = verifyToken(rotatedToken, "s3cret");

    expect(rotatedClaims.sub).toBe(originalClaims.sub);
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
  } finally {
    vi.useRealTimers();
  }
});
