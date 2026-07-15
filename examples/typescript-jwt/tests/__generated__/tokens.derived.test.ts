// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/tokens.jaunt-test.ts
// jaunt:test_spec_digest=sha256:af5a7a64a6ba14f47956cdca7e8990398929d529817d11c369f8f6ca36b53797
// jaunt:target_api_digest=sha256:cd4f1440dd3bcea70185bca822d9e5f3d2cee38d157f5dbb79041b5bc88177de
// jaunt:vitest_fingerprint=sha256:5ef9c3f603f2a5e5aa0967833c5876ba1374afb318d3777d80f2d86c1fb0a905
// jaunt:fast_check_fingerprint=sha256:97f62ee354ca9285052845e71b421a6e36baf29fd9d99056da8a9e34f27b47d8
// jaunt:runner_fingerprint=sha256:3114b139d6bde5d82beb36dae934badd781923fca5fbb0c72eae457211f3d898
// jaunt:prompt_fingerprint=sha256:c01073b453383c0f7394eaaf1cfeebadd1099a87810a688a2e8785b50876635f
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:eee32ba040e7e87e71c6db81816d4ee01c10383b2f1cfb032df6272a75b6692c
// jaunt:body_digest=sha256:fd4826e42afa7e3d396af8de02baa1a1dfe8714c46749a4c4f27354127dcf442

import { createHmac } from "node:crypto";

import fc from "fast-check";
import { expect } from "vitest";

import { test } from "../fixtures.js";
import {
  createToken,
  rotateToken,
  TokenStore,
  verifyToken,
} from "../../src/tokens/index.js";

const FIXED_MILLISECONDS = 1_700_000_000_123;
const FIXED_SECONDS = Math.floor(FIXED_MILLISECONDS / 1_000);

function encodeJson(value: unknown): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function signSegments(header: unknown, payload: unknown, secret: string): string {
  const signingInput = `${encodeJson(header)}.${encodeJson(payload)}`;
  const signature = createHmac("sha256", secret).update(signingInput).digest("base64url");
  return `${signingInput}.${signature}`;
}

function errorCode(action: () => unknown): unknown {
  try {
    action();
  } catch (error: unknown) {
    if (typeof error === "object" && error !== null && "code" in error) {
      return error.code;
    }
    throw error;
  }
  throw new Error("expected action to throw");
}

test("d-01", () => {
  fc.assert(
    fc.property(
      fc.string({ minLength: 1, maxLength: 32 }),
      fc.string({ maxLength: 32 }),
      fc.integer({ min: -86_400, max: 86_400 }),
      (subject, secret, ttlSeconds) => {
        const originalNow = Date.now;
        Date.now = () => FIXED_MILLISECONDS;
        try {
          const token = createToken(subject, secret, { ttlSeconds });
          const segments = token.split(".");

          expect(segments).toHaveLength(3);
          expect(segments.every((segment) => segment.length > 0 && !segment.includes("="))).toBe(
            true,
          );
          expect(JSON.parse(Buffer.from(segments[0]!, "base64url").toString("utf8"))).toEqual({
            alg: "HS256",
            typ: "JWT",
          });

          const expectedClaims = {
            sub: subject,
            iat: FIXED_SECONDS,
            exp: FIXED_SECONDS + ttlSeconds,
          };
          expect(JSON.parse(Buffer.from(segments[1]!, "base64url").toString("utf8"))).toEqual(
            expectedClaims,
          );
          expect(segments[2]).toBe(
            createHmac("sha256", secret)
              .update(`${segments[0]}.${segments[1]}`)
              .digest("base64url"),
          );

          if (ttlSeconds > 0) {
            expect(verifyToken(token, secret)).toEqual(expectedClaims);
          } else {
            expect(errorCode(() => verifyToken(token, secret))).toBe("expired");
          }
        } finally {
          Date.now = originalNow;
        }
      },
    ),
    { seed: 130501811, numRuns: 50 },
  );
});

test("d-02", () => {
  const originalNow = Date.now;
  Date.now = () => FIXED_MILLISECONDS;
  try {
    const token = createToken("subject", "secret", { ttlSeconds: 7.9 });
    expect(verifyToken(token, "secret")).toEqual({
      sub: "subject",
      iat: FIXED_SECONDS,
      exp: FIXED_SECONDS + 7,
    });
    expect(() => createToken("", "secret")).toThrow(RangeError);
  } finally {
    Date.now = originalNow;
  }
});

test("d-03", () => {
  const originalNow = Date.now;
  Date.now = () => FIXED_MILLISECONDS;
  try {
    fc.assert(
      fc.property(
        fc.integer({ min: 1, max: 7_200 }),
        fc.integer({ min: -300, max: 300 }),
        (originalTtl, requestedTtl) => {
          const original = verifyToken(
            createToken("rotating-subject", "rotation-secret", {
              ttlSeconds: originalTtl,
            }),
            "rotation-secret",
          );
          const rotated = verifyToken(
            rotateToken(
              createToken("rotating-subject", "rotation-secret", {
                ttlSeconds: originalTtl,
              }),
              "rotation-secret",
              { ttlSeconds: requestedTtl },
            ),
            "rotation-secret",
          );

          expect(rotated.sub).toBe(original.sub);
          expect(rotated.iat).toBeGreaterThan(original.iat);
          expect(rotated.exp).toBeGreaterThan(original.exp);
        },
      ),
      { seed: 130501811, numRuns: 50 },
    );
  } finally {
    Date.now = originalNow;
  }
});

test("d-04", () => {
  const originalNow = Date.now;
  Date.now = () => FIXED_MILLISECONDS;
  try {
    const extraClaim = signSegments(
      { alg: "HS256", typ: "JWT" },
      { sub: "subject", iat: FIXED_SECONDS, exp: FIXED_SECONDS + 60, role: "admin" },
      "secret",
    );
    const wrongHeader = signSegments(
      { alg: "HS512", typ: "JWT" },
      { sub: "subject", iat: FIXED_SECONDS, exp: FIXED_SECONDS + 60 },
      "secret",
    );

    expect(errorCode(() => verifyToken(extraClaim, "secret"))).toBe("malformed");
    expect(errorCode(() => verifyToken(wrongHeader, "secret"))).toBe("malformed");
    expect(errorCode(() => verifyToken("one..three", "secret"))).toBe("malformed");
  } finally {
    Date.now = originalNow;
  }
});

test("d-05", ({ clock }) => {
  fc.assert(
    fc.property(
      fc.array(
        fc.record({
          subject: fc.string({ minLength: 1, maxLength: 12 }),
          token: fc.string({ maxLength: 16 }),
          offset: fc.integer({ min: -30, max: 30 }),
        }),
        { maxLength: 40 },
      ),
      (entries) => {
        const store = new TokenStore(clock.now);
        const latest = new Map<string, { token: string; exp: number }>();

        for (const entry of entries) {
          const exp = clock.now() + entry.offset;
          store.put(entry.subject, entry.token, exp);
          latest.set(entry.subject, { token: entry.token, exp });
        }

        const live = [...latest.values()].filter(({ exp }) => exp > clock.now()).length;
        const expired = latest.size - live;
        expect(store.size).toBe(live);
        for (const [subject, entry] of latest) {
          expect(store.get(subject)).toBe(entry.exp > clock.now() ? entry.token : null);
        }

        expect(store.sweep()).toBe(expired);
        expect(store.sweep()).toBe(0);
        expect(store.size).toBe(live);
      },
    ),
    { seed: 130501811, numRuns: 50 },
  );
});

test("d-06", ({ clock }) => {
  const store = new TokenStore(clock.now);
  store.put("boundary", "old", clock.now());

  expect(store.get("boundary")).toBeNull();
  expect(store.get("boundary")).toBeNull();
  expect(store.size).toBe(0);
  expect(store.sweep()).toBe(1);

  store.put("moving", "first", clock.now() + 2);
  store.put("moving", "replacement", clock.now() + 4);
  clock.advance(3);
  expect(store.get("moving")).toBe("replacement");
  expect(store.size).toBe(1);
  clock.advance(1);
  expect(store.get("moving")).toBeNull();
  expect(store.sweep()).toBe(1);
});
