// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:f746f2a48488bf063d69d5c7cd7c60559d3f14a725c0b34e744f6c614b97917f
// jaunt:fixture_fingerprint=sha256:184e0133ce415140efdb1a2a1515cb0e9494a882aaeb027b20e21182dbb785b7
// jaunt:vitest_fingerprint=sha256:bcf02994ff7e31dfd0b3a6a40ccd69cd082e7540cecf5e7951ea1e2a8fccb834
// jaunt:fast_check_fingerprint=sha256:f1c128dc85d13bc09d48112c75bb3a18159bfb2d301fe28fe9140058bd5801ac
// jaunt:runner_fingerprint=sha256:52602d0c1edc81cb7e6da304e5fbf595cf43146144afa8a8aaf7acf0f1e7880c
// jaunt:prompt_fingerprint=sha256:a274f34bea91b04218014d8c915efe6bb2754c16a073fe67fa11bba30bcf22f5
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:57568e73d86336fb432cbbda26fc1a78ef975fa4f81a4532a7231131805a5754
// jaunt:body_digest=sha256:f18cf401127f82b3a06e14f2962735a4e368136b7e90dfec47d58ee784686cd6

import { expect, vi } from "vitest";

import { createToken, rotateToken, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

const FIXED_TIME_MS = 1_700_000_000_000;

test("roundtrips a token for user-42", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_TIME_MS);

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
  vi.setSystemTime(FIXED_TIME_MS);

  try {
    const token = createToken("user-42", "s3cret");
    let failure: unknown;

    try {
      verifyToken(token, "different-secret");
    } catch (error) {
      failure = error;
    }

    expect(failure).toMatchObject({ code: "invalid-signature" });
  } finally {
    vi.useRealTimers();
  }
});

test("rotation preserves the subject and advances both timestamps", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_TIME_MS);

  try {
    const original = createToken("user-42", "s3cret");
    const originalClaims = verifyToken(original, "s3cret");
    const rotated = rotateToken(original, "s3cret");
    const rotatedClaims = verifyToken(rotated, "s3cret");

    expect(rotatedClaims.sub).toBe(originalClaims.sub);
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
  } finally {
    vi.useRealTimers();
  }
});
