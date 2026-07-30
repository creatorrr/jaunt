// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:f746f2a48488bf063d69d5c7cd7c60559d3f14a725c0b34e744f6c614b97917f
// jaunt:fixture_fingerprint=sha256:184e0133ce415140efdb1a2a1515cb0e9494a882aaeb027b20e21182dbb785b7
// jaunt:vitest_fingerprint=sha256:bcf02994ff7e31dfd0b3a6a40ccd69cd082e7540cecf5e7951ea1e2a8fccb834
// jaunt:fast_check_fingerprint=sha256:f1c128dc85d13bc09d48112c75bb3a18159bfb2d301fe28fe9140058bd5801ac
// jaunt:runner_fingerprint=sha256:32a716b44a009f4568cef091e5b73d62290ddca05aa9e57845b96a926c2507d0
// jaunt:prompt_fingerprint=sha256:c01073b453383c0f7394eaaf1cfeebadd1099a87810a688a2e8785b50876635f
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:f196050acd95b5a7728bc51fb5c710fb7fa2b304ea33f9e24ba1abf81f9f0a98
// jaunt:body_digest=sha256:9e8916bd5fed95c273e6b7b0d0eb7910fc0019288dfb528eab6b2c1c7ba5688d

import { createHmac } from "node:crypto";

import { expect, vi } from "vitest";

import {
  createToken,
  rotateToken,
  TokenStore,
  verifyToken,
} from "../../src/tokens/index.js";
import { test } from "../fixtures.js";

function encode(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function sign(header: unknown, payload: unknown, secret: string): string {
  const unsigned = `${encode(header)}.${encode(payload)}`;
  const signature = createHmac("sha256", secret).update(unsigned).digest("base64url");
  return `${unsigned}.${signature}`;
}

function decodeSegment(segment: string): unknown {
  return JSON.parse(Buffer.from(segment, "base64url").toString("utf8"));
}

function errorCode(action: () => unknown): unknown {
  try {
    action();
  } catch (error) {
    if (typeof error === "object" && error !== null && "code" in error) {
      return error.code;
    }
    throw error;
  }
  throw new Error("expected action to throw");
}

test("D001", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = createToken("subject-a", "key-a", { ttlSeconds: 17.9 });
    const segments = token.split(".");

    expect(segments).toHaveLength(3);
    expect(segments.every((segment) => segment.length > 0 && !segment.includes("="))).toBe(true);
    expect(decodeSegment(segments.at(0)!)).toEqual({ alg: "HS256", typ: "JWT" });
    expect(decodeSegment(segments.at(1)!)).toEqual({
      sub: "subject-a",
      iat: 1_700_000_000,
      exp: 1_700_000_017,
    });

    const unsigned = `${segments.at(0)}.${segments.at(1)}`;
    expect(segments.at(2)).toBe(
      createHmac("sha256", "key-a").update(unsigned).digest("base64url"),
    );
  } finally {
    vi.useRealTimers();
  }
});

test("D002", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const claims = decodeSegment(createToken("subject-b", "", {}).split(".").at(1)!);
    expect(claims).toEqual({
      sub: "subject-b",
      iat: 1_700_000_000,
      exp: 1_700_003_600,
    });
  } finally {
    vi.useRealTimers();
  }
});

test("D003", () => {
  expect(() => createToken("", "key-b")).toThrow(RangeError);
});

test("D004", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = sign(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-c", iat: 1_699_999_900, exp: 1_700_000_001 },
      "key-c",
    );
    expect(verifyToken(token, "key-c")).toEqual({
      sub: "subject-c",
      iat: 1_699_999_900,
      exp: 1_700_000_001,
    });
  } finally {
    vi.useRealTimers();
  }
});

test("D005", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = sign(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject-d", iat: 1_699_999_900, exp: 1_700_000_000 },
      "key-d",
    );
    expect(errorCode(() => verifyToken(token, "key-d"))).toBe("expired");
  } finally {
    vi.useRealTimers();
  }
});

test("D006", () => {
  const malformed = ["", "a.b", "a..b", "a.b.c.d", "!.e30.eA"];
  for (const token of malformed) {
    expect(errorCode(() => verifyToken(token, "key-e"))).toBe("malformed");
  }
});

test("D007", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = sign(
      { alg: "HS512", typ: "JWT" },
      { sub: "subject-e", iat: 1_700_000_000, exp: 1_700_000_100 },
      "key-f",
    );
    expect(errorCode(() => verifyToken(token, "key-f"))).toBe("malformed");
  } finally {
    vi.useRealTimers();
  }
});

test("D008", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const invalidPayloads = [
      { sub: 7, iat: 1_700_000_000, exp: 1_700_000_100 },
      { sub: "subject-f", iat: "1700000000", exp: 1_700_000_100 },
      { sub: "subject-f", iat: 1_700_000_000, exp: 1_700_000_100, role: "admin" },
      { sub: "subject-f", iat: 1_700_000_000 },
    ];
    for (const payload of invalidPayloads) {
      const token = sign({ alg: "HS256", typ: "JWT" }, payload, "key-g");
      expect(errorCode(() => verifyToken(token, "key-g"))).toBe("malformed");
    }
  } finally {
    vi.useRealTimers();
  }
});

test("D009", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = createToken("subject-g", "key-h");
    expect(errorCode(() => verifyToken(token, "key-i"))).toBe("invalid-signature");
  } finally {
    vi.useRealTimers();
  }
});

test("D010", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const original = createToken("subject-h", "key-j", { ttlSeconds: 100 });
    const originalClaims = verifyToken(original, "key-j");
    const rotated = rotateToken(original, "key-j", { ttlSeconds: 1 });
    const rotatedClaims = verifyToken(rotated, "key-j");

    expect(rotatedClaims.sub).toBe(originalClaims.sub);
    expect(rotatedClaims.iat).toBeGreaterThan(originalClaims.iat);
    expect(rotatedClaims.exp).toBeGreaterThan(originalClaims.exp);
  } finally {
    vi.useRealTimers();
  }
});

test("D011", () => {
  vi.useFakeTimers();
  try {
    vi.setSystemTime(new Date(1_700_000_000_000));
    const token = createToken("subject-i", "key-k");
    expect(errorCode(() => verifyToken(token, "key-l"))).toBe("invalid-signature");
    expect(errorCode(() => rotateToken(token, "key-l"))).toBe("invalid-signature");
  } finally {
    vi.useRealTimers();
  }
});

test("D012", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("alpha", "token-1", clock.now() + 10);
  store.put("beta", "token-2", clock.now() + 20);
  store.put("alpha", "token-3", clock.now() + 30);

  expect(store.get("alpha")).toBe("token-3");
  expect(store.get("beta")).toBe("token-2");
  expect(store.get("missing")).toBeNull();
  expect(store.size).toBe(2);
});

test("D013", ({ clock }) => {
  const store = new TokenStore(clock.now);
  const boundary = clock.now() + 5;
  store.put("alpha", "token-1", boundary);
  store.put("beta", "token-2", boundary + 1);
  clock.advance(5);

  expect(store.get("alpha")).toBeNull();
  expect(store.get("alpha")).toBeNull();
  expect(store.get("beta")).toBe("token-2");
  expect(store.size).toBe(1);
  expect(store.sweep()).toBe(1);
  expect(store.sweep()).toBe(0);
  expect(store.size).toBe(1);
});
