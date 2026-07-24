// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:d192a0f0d5f6705c9ca0854d669f6b4b4db585bd5629167fe8b87fab8c937660
// jaunt:fixture_fingerprint=sha256:184e0133ce415140efdb1a2a1515cb0e9494a882aaeb027b20e21182dbb785b7
// jaunt:vitest_fingerprint=sha256:bcf02994ff7e31dfd0b3a6a40ccd69cd082e7540cecf5e7951ea1e2a8fccb834
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:51b858352ab8df2106aebd8688385d75f2b05bc61fff85ba5d8f074a3463aca1
// jaunt:prompt_fingerprint=sha256:c01073b453383c0f7394eaaf1cfeebadd1099a87810a688a2e8785b50876635f
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:5b5d9830b6b8713caec2531cef1686e07c78a71c6455da5437179a5aeef43fb3
// jaunt:body_digest=sha256:48e8c034b1d9b3808036c0e051d2650aebb41cbc3a3e48d5b19629f1fb6da4da

import { createHmac } from "node:crypto";

import { expect, vi } from "vitest";

import { createToken, rotateToken, TokenStore, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

interface ErrorWithCode extends Error {
  code?: unknown;
}

function encodeJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function signSegments(header: unknown, payload: unknown, secret: string): string {
  const encodedHeader = encodeJson(header);
  const encodedPayload = encodeJson(payload);
  const signingInput = `${encodedHeader}.${encodedPayload}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

function readJsonSegment(token: string, position: number): unknown {
  const segment = token.split(".").at(position);
  if (segment === undefined) {
    throw new Error("missing JWT segment");
  }
  return JSON.parse(Buffer.from(segment, "base64url").toString("utf8")) as unknown;
}

function captureError(operation: () => unknown): ErrorWithCode {
  try {
    operation();
  } catch (error) {
    if (error instanceof Error) {
      return error;
    }
    throw error;
  }
  throw new Error("operation did not throw");
}

test("d-01f4a8", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_123_456));
    const actual = createToken("subject-a", "key-a", { ttlSeconds: 12.9 });
    const expected = signSegments(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-a", iat: 1_700_000_123, exp: 1_700_000_135 },
      "key-a",
    );

    expect(actual).toBe(expected);
    expect(actual.split(".")).toHaveLength(3);
    expect(actual).not.toContain("=");
  } finally {
    vi.useRealTimers();
  }
});

test("d-0be37c", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_010_000_999));
    const claims = readJsonSegment(createToken("subject-b", "", {}), 1);

    expect(claims).toEqual({
      sub: "subject-b",
      iat: 1_700_010_000,
      exp: 1_700_013_600,
    });
  } finally {
    vi.useRealTimers();
  }
});

test("d-13ce92", () => {
  expect(() => createToken("", "key-b")).toThrow(RangeError);
});

test("d-21a6dd", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_020_000_000));
    const token = signSegments(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-c", iat: 1_700_019_900, exp: 1_700_020_100 },
      "signing-key",
    );
    const error = captureError(() => verifyToken(token, "verification-key"));

    expect(error.code).toBe("invalid-signature");
  } finally {
    vi.useRealTimers();
  }
});

test("d-3648b1", () => {
  for (const token of ["", "a.b", "a..c", ".b.c", "a.b.c.d", "a!.b.c"]) {
    const error = captureError(() => verifyToken(token, "key-c"));
    expect(error.code).toBe("malformed");
  }
});

test("d-47df05", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_030_000_000));
    const validClaims = { sub: "subject-d", iat: 1_700_029_900, exp: 1_700_030_100 };
    const candidates = [
      signSegments({ alg: "HS512", typ: "JWT" }, validClaims, "key-d"),
      signSegments(
        { alg: "HS256", typ: "JWT" },
        { ...validClaims, audience: "extra" },
        "key-d",
      ),
      signSegments(
        { alg: "HS256", typ: "JWT" },
        { sub: 7, iat: validClaims.iat, exp: validClaims.exp },
        "key-d",
      ),
      signSegments(
        { alg: "HS256", typ: "JWT" },
        { sub: validClaims.sub, exp: validClaims.exp },
        "key-d",
      ),
    ];

    for (const token of candidates) {
      const error = captureError(() => verifyToken(token, "key-d"));
      expect(error.code).toBe("malformed");
    }
  } finally {
    vi.useRealTimers();
  }
});

test("d-58c20e", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_040_000_000));
    const atBoundary = signSegments(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-e", iat: 1_700_039_900, exp: 1_700_040_000 },
      "key-e",
    );
    const beforeBoundary = signSegments(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-f", iat: 1_700_039_800, exp: 1_700_039_999 },
      "key-e",
    );

    expect(captureError(() => verifyToken(atBoundary, "key-e")).code).toBe("expired");
    expect(captureError(() => verifyToken(beforeBoundary, "key-e")).code).toBe("expired");
  } finally {
    vi.useRealTimers();
  }
});

test("d-69b7f3", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_050_000_000));
    const original = createToken("subject-g", "key-f", { ttlSeconds: 600 });
    const originalClaims = verifyToken(original, "key-f");
    const rotated = rotateToken(original, "key-f", { ttlSeconds: 1 });
    const rotatedClaims = verifyToken(rotated, "key-f");

    expect(rotated).not.toBe(original);
    expect(rotatedClaims.sub).toBe(originalClaims.sub);
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
    expect(Object.keys(rotatedClaims).sort()).toEqual(["exp", "iat", "sub"]);
  } finally {
    vi.useRealTimers();
  }
});

test("d-7a14c9", () => {
  const malformed = captureError(() => rotateToken("not-a-token", "key-g"));
  expect(malformed.code).toBe("malformed");
});

test("d-8d03e6", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("subject-h", "first", clock.now() + 20);
  store.put("subject-i", "other", clock.now() + 30);
  store.put("subject-h", "replacement", clock.now() + 40);

  expect(store.get("subject-h")).toBe("replacement");
  expect(store.get("subject-i")).toBe("other");
  expect(store.get("missing")).toBeNull();
  expect(store.size).toBe(2);
});

test("d-9eb521", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("subject-j", "boundary", clock.now() + 5);
  store.put("subject-k", "later", clock.now() + 6);
  clock.advance(5);

  expect(store.get("subject-j")).toBeNull();
  expect(store.get("subject-j")).toBeNull();
  expect(store.get("subject-k")).toBe("later");
  expect(store.size).toBe(1);
  expect(store.sweep()).toBe(1);
  expect(store.sweep()).toBe(0);
  expect(store.size).toBe(1);
});

test("d-a24f70", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("subject-l", "already-expired", clock.now() - 1);
  store.put("subject-m", "at-boundary", clock.now());
  store.put("subject-n", "live", clock.now() + 1);

  expect(store.size).toBe(1);
  expect(store.sweep()).toBe(2);
  expect(store.get("subject-n")).toBe("live");
  expect(store.size).toBe(1);
});
