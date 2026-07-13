// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:a4b0e2960a1a841e6ba2a76b84ce8cbb5290bac199155056a06edb73328f418b
// jaunt:vitest_fingerprint=sha256:5ef9c3f603f2a5e5aa0967833c5876ba1374afb318d3777d80f2d86c1fb0a905
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:784f8b7a06ae4b2e79b4d5f349b1e530713d3168aaaeba4a714fd68d44b772fa
// jaunt:prompt_fingerprint=sha256:a274f34bea91b04218014d8c915efe6bb2754c16a073fe67fa11bba30bcf22f5
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:1df72a0c92ebd6dd5adfc9d144a5c5153dded4b1f87a84b8e4fe680c26e162a0
// jaunt:body_digest=sha256:f0c4d9a9873128b6bc4e705bd0b07a6c9f607f1ac6ebd9d8ac52d7d390f5602b

import { createHmac } from "node:crypto";

import { expect, vi } from "vitest";

import { createToken, rotateToken, TokenStore, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

function base64url(value: string): string {
  return Buffer.from(value, "utf8").toString("base64url");
}

function signedToken(
  header: unknown,
  payload: unknown,
  secret: string,
): string {
  const signingInput = `${base64url(JSON.stringify(header))}.${base64url(JSON.stringify(payload))}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

function expectJwtError(action: () => unknown, code: string): void {
  let thrown: unknown;
  try {
    action();
  } catch (error) {
    thrown = error;
  }
  expect(thrown).toMatchObject({ code });
}

test("createToken emits the specified HS256 JWT with a default one-hour lifetime", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));

    const token = createToken("user-123", "example-secret");
    const expected = signedToken(
      { alg: "HS256", typ: "JWT" },
      { sub: "user-123", iat: 1_700_000_000, exp: 1_700_003_600 },
      "example-secret",
    );

    expect(token).toBe(expected);
    expect(token.split(".")).toHaveLength(3);
    expect(token).not.toContain("=");
  } finally {
    vi.useRealTimers();
  }
});

test("createToken truncates a fractional TTL to whole seconds", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));

    const token = createToken("fractional", "secret", { ttlSeconds: 12.9 });

    expect(token).toBe(
      signedToken(
        { alg: "HS256", typ: "JWT" },
        { sub: "fractional", iat: 1_700_000_000, exp: 1_700_000_012 },
        "secret",
      ),
    );
  } finally {
    vi.useRealTimers();
  }
});

test("createToken rejects an empty subject", () => {
  expect(() => createToken("", "secret")).toThrow(RangeError);
});

test("verifyToken returns the three declared claims for a valid token", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const token = createToken("verified-user", "correct-secret", { ttlSeconds: 90 });

    expect(verifyToken(token, "correct-secret")).toEqual({
      sub: "verified-user",
      iat: 1_700_000_000,
      exp: 1_700_000_090,
    });
  } finally {
    vi.useRealTimers();
  }
});

test("verifyToken reports malformed tokens", () => {
  expectJwtError(() => verifyToken("only.two", "secret"), "malformed");
});

test("verifyToken reports a signature made with a different secret", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const token = createToken("user", "first-secret");

    expectJwtError(() => verifyToken(token, "second-secret"), "invalid-signature");
  } finally {
    vi.useRealTimers();
  }
});

test("verifyToken rejects a signed token with a non-HS256 header", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const token = signedToken(
      { alg: "HS512", typ: "JWT" },
      { sub: "user", iat: 1_700_000_000, exp: 1_700_000_060 },
      "secret",
    );

    expectJwtError(() => verifyToken(token, "secret"), "malformed");
  } finally {
    vi.useRealTimers();
  }
});

test("verifyToken requires exactly the declared claim fields", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const token = signedToken(
      { alg: "HS256", typ: "JWT" },
      { sub: "user", iat: 1_700_000_000, exp: 1_700_000_060, role: "admin" },
      "secret",
    );

    expectJwtError(() => verifyToken(token, "secret"), "malformed");
  } finally {
    vi.useRealTimers();
  }
});

test("a negative TTL can mint a token that verification reports as expired", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const token = createToken("expired-user", "secret", { ttlSeconds: -1 });

    expectJwtError(() => verifyToken(token, "secret"), "expired");
  } finally {
    vi.useRealTimers();
  }
});

test("rotateToken preserves the subject and strictly increases both timestamps", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date("2023-11-14T22:13:20.000Z"));
    const original = createToken("rotating-user", "secret");
    const originalClaims = verifyToken(original, "secret");
    const rotated = rotateToken(original, "secret", { ttlSeconds: 1 });
    const rotatedClaims = verifyToken(rotated, "secret");

    expect(rotatedClaims.sub).toBe("rotating-user");
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
  } finally {
    vi.useRealTimers();
  }
});

test("TokenStore replaces the token recorded for a subject", ({ clock }) => {
  const store = new TokenStore(clock.now);

  store.put("subject", "first", clock.now() + 60);
  store.put("subject", "second", clock.now() + 120);

  expect(store.get("subject")).toBe("second");
  expect(store.size).toBe(1);
});

test("TokenStore reads hide expired entries without deleting them before sweep", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("expired", "old-token", clock.now() + 10);
  store.put("live", "live-token", clock.now() + 20);

  clock.advance(10);

  expect(store.get("expired")).toBeNull();
  expect(store.get("expired")).toBeNull();
  expect(store.get("live")).toBe("live-token");
  expect(store.size).toBe(1);
  expect(store.sweep()).toBe(1);
  expect(store.sweep()).toBe(0);
  expect(store.size).toBe(1);
});
