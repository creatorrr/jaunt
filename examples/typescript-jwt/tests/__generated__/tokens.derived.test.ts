// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:a4b0e2960a1a841e6ba2a76b84ce8cbb5290bac199155056a06edb73328f418b
// jaunt:vitest_fingerprint=sha256:5ef9c3f603f2a5e5aa0967833c5876ba1374afb318d3777d80f2d86c1fb0a905
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:47950fca6a142d21742750cb08527520a2f26f4d881985fdb6a15e8e597ba26e
// jaunt:prompt_fingerprint=sha256:c01073b453383c0f7394eaaf1cfeebadd1099a87810a688a2e8785b50876635f
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:e64e3e9ef9b9895612649ed3eba354e3d880ba7b4c3095aee35fef030c6873a5
// jaunt:body_digest=sha256:90bc013e3a301a61c64b76e3ffb352ccb1d0fdf14d385b08bcba40c593946854

import { createHmac } from "node:crypto";

import { expect, vi } from "vitest";

import { TokenStore, createToken, rotateToken, verifyToken } from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

const FIXED_MILLISECONDS = 1_700_000_000_000;
const FIXED_SECONDS = FIXED_MILLISECONDS / 1_000;

function encodeJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function signSegments(header: string, payload: string, secret: string): string {
  const signature = createHmac("sha256", secret)
    .update(`${header}.${payload}`)
    .digest("base64url");
  return `${header}.${payload}.${signature}`;
}

function signedToken(header: unknown, payload: unknown, secret: string): string {
  return signSegments(encodeJson(header), encodeJson(payload), secret);
}

function decodeSegment(segment: string): unknown {
  return JSON.parse(Buffer.from(segment, "base64url").toString("utf8"));
}

function captureError(action: () => unknown): unknown {
  try {
    action();
  } catch (error) {
    return error;
  }
  throw new Error("expected action to throw");
}

test("d001", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const token = createToken("user-17", "secret-17", { ttlSeconds: 19.9 });
    const segments = token.split(".");

    expect(segments).toHaveLength(3);
    expect(segments.every((segment) => segment.length > 0 && !segment.includes("="))).toBe(true);
    expect(decodeSegment(segments[0]!)).toEqual({ alg: "HS256", typ: "JWT" });
    expect(decodeSegment(segments[1]!)).toEqual({
      sub: "user-17",
      iat: FIXED_SECONDS,
      exp: FIXED_SECONDS + 19,
    });
    expect(token).toBe(signSegments(segments[0]!, segments[1]!, "secret-17"));
  } finally {
    vi.useRealTimers();
  }
});

test("d002", () => {
  expect(() => createToken("", "secret")).toThrow(RangeError);
});

test("d003", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const claims = verifyToken(createToken("subject", "key"), "key");
    expect(claims).toEqual({
      sub: "subject",
      iat: FIXED_SECONDS,
      exp: FIXED_SECONDS + 3_600,
    });
    expect(Object.keys(claims).sort()).toEqual(["exp", "iat", "sub"]);
  } finally {
    vi.useRealTimers();
  }
});

test("d004", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const error = captureError(() => verifyToken(createToken("subject", "right-key"), "wrong-key"));
    expect(error).toMatchObject({ code: "invalid-signature" });
  } finally {
    vi.useRealTimers();
  }
});

test("d005", () => {
  for (const token of ["", "a.b", "a..c", "a.b.c.d", "*.b.c"]) {
    const error = captureError(() => verifyToken(token, "key"));
    expect(error).toMatchObject({ code: "malformed" });
  }
});

test("d006", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const wrongHeader = signedToken(
      { alg: "HS512", typ: "JWT" },
      { sub: "subject", iat: FIXED_SECONDS, exp: FIXED_SECONDS + 10 },
      "key",
    );
    const extraClaim = signedToken(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject", iat: FIXED_SECONDS, exp: FIXED_SECONDS + 10, role: "admin" },
      "key",
    );
    const wrongType = signedToken(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject", iat: String(FIXED_SECONDS), exp: FIXED_SECONDS + 10 },
      "key",
    );

    for (const token of [wrongHeader, extraClaim, wrongType]) {
      expect(captureError(() => verifyToken(token, "key"))).toMatchObject({ code: "malformed" });
    }
  } finally {
    vi.useRealTimers();
  }
});

test("d007", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const token = signedToken(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject", iat: FIXED_SECONDS - 10, exp: FIXED_SECONDS },
      "key",
    );
    expect(captureError(() => verifyToken(token, "key"))).toMatchObject({ code: "expired" });
  } finally {
    vi.useRealTimers();
  }
});

test("d008", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const original = createToken("subject", "key", { ttlSeconds: 600 });
    const rotated = rotateToken(original, "key", { ttlSeconds: 1 });
    const originalPayload = decodeSegment(original.split(".")[1]!) as {
      sub: string;
      iat: number;
      exp: number;
    };
    const rotatedPayload = decodeSegment(rotated.split(".")[1]!) as {
      sub: string;
      iat: number;
      exp: number;
    };

    expect(rotatedPayload.sub).toBe(originalPayload.sub);
    expect(rotatedPayload.iat).toBeGreaterThan(originalPayload.iat);
    expect(rotatedPayload.exp).toBeGreaterThan(originalPayload.exp);
    expect(verifyToken(rotated, "key")).toEqual(rotatedPayload);
  } finally {
    vi.useRealTimers();
  }
});

test("d009", () => {
  vi.useFakeTimers();
  vi.setSystemTime(FIXED_MILLISECONDS);
  try {
    const original = createToken("subject", "right-key");
    const error = captureError(() => rotateToken(original, "wrong-key"));
    expect(error).toMatchObject({ code: "invalid-signature" });
  } finally {
    vi.useRealTimers();
  }
});

test("d010", ({ clock }) => {
  const store = new TokenStore(clock.now);
  const now = clock.now();
  store.put("live", "token-live", now + 5);
  store.put("boundary", "token-boundary", now);
  store.put("past", "token-past", now - 1);

  expect(store.get("live")).toBe("token-live");
  expect(store.get("boundary")).toBeNull();
  expect(store.get("past")).toBeNull();
  expect(store.size).toBe(1);
  expect(store.sweep()).toBe(2);
  expect(store.sweep()).toBe(0);
});

test("d011", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("subject", "first", clock.now() + 100);
  store.put("subject", "second", clock.now() + 2);

  expect(store.size).toBe(1);
  expect(store.get("subject")).toBe("second");
  clock.advance(2);
  expect(store.get("subject")).toBeNull();
  expect(store.size).toBe(0);
  expect(store.sweep()).toBe(1);
});
