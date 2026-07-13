// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:src/tokens/index
// jaunt:structural=sha256:c134eff9f071a78e297afcc875062b6a7fb5a9807ed93b22915e5cf815451b56
// jaunt:prose=sha256:616b419f63caf931be25251541e08d964b503d846e7f0c727d8dbc6468bcce3c
// jaunt:api=sha256:fc47b3f9b77ec6f05296ec589683f99f07cb2cea6b88eaed56b908d9d3e97853
import type * as __JauntApi from "./index.api.js";
import { Buffer } from "node:buffer";
import { createHmac, timingSafeEqual } from "node:crypto";

import { JwtError, nowSeconds } from "../index.context.js";

type TokenOptions = { ttlSeconds?: number };

type TokenClaims = {
  sub: string;
  iat: number;
  exp: number;
};

type StoredToken = {
  token: string;
  exp: number;
};

const encodedHeader = encodeJson({ alg: "HS256", typ: "JWT" });
const base64urlPattern = /^[A-Za-z0-9_-]+$/;

function encodeJson(value: object): string {
  return Buffer.from(JSON.stringify(value), "utf8").toString("base64url");
}

function sign(encodedHeaderAndPayload: string, secret: string): string {
  return createHmac("sha256", secret).update(encodedHeaderAndPayload).digest("base64url");
}

function issueToken(subject: string, secret: string, issuedAt: number, expiresAt: number): string {
  const encodedPayload = encodeJson({ sub: subject, iat: issuedAt, exp: expiresAt });
  const unsignedToken = `${encodedHeader}.${encodedPayload}`;
  return `${unsignedToken}.${sign(unsignedToken, secret)}`;
}

function ttlSeconds(opts?: TokenOptions): number {
  return Math.trunc(opts?.ttlSeconds ?? 3600);
}

function isBase64urlSegment(segment: string): boolean {
  return (
    segment.length > 0 &&
    base64urlPattern.test(segment) &&
    Buffer.from(segment, "base64url").toString("base64url") === segment
  );
}

function decodeJson(segment: string): unknown {
  try {
    return JSON.parse(Buffer.from(segment, "base64url").toString("utf8"));
  } catch {
    throw new JwtError("malformed");
  }
}

function hasExactKeys(value: object, expected: readonly string[]): boolean {
  const keys = Object.keys(value);
  return keys.length === expected.length && expected.every((key) => keys.includes(key));
}

function validateHeader(value: unknown): void {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    !hasExactKeys(value, ["alg", "typ"]) ||
    !("alg" in value) ||
    value.alg !== "HS256" ||
    !("typ" in value) ||
    value.typ !== "JWT"
  ) {
    throw new JwtError("malformed");
  }
}

function validateClaims(value: unknown): TokenClaims {
  if (
    typeof value !== "object" ||
    value === null ||
    Array.isArray(value) ||
    !hasExactKeys(value, ["sub", "iat", "exp"]) ||
    !("sub" in value) ||
    typeof value.sub !== "string" ||
    !("iat" in value) ||
    typeof value.iat !== "number" ||
    !Number.isFinite(value.iat) ||
    !("exp" in value) ||
    typeof value.exp !== "number" ||
    !Number.isFinite(value.exp)
  ) {
    throw new JwtError("malformed");
  }

  return { sub: value.sub, iat: value.iat, exp: value.exp };
}

function __jaunt_impl_createToken(
  userId: string,
  secret: string,
  opts?: TokenOptions,
): string {
  if (userId.length === 0) {
    throw new RangeError("userId must not be empty");
  }

  const issuedAt = nowSeconds();
  return issueToken(userId, secret, issuedAt, issuedAt + ttlSeconds(opts));
}

function __jaunt_impl_verifyToken(token: string, secret: string): TokenClaims {
  const segments = token.split(".");
  if (segments.length !== 3 || segments.some((segment) => !isBase64urlSegment(segment))) {
    throw new JwtError("malformed");
  }

  const headerSegment = segments[0];
  const payloadSegment = segments[1];
  const signatureSegment = segments[2];
  if (headerSegment === undefined || payloadSegment === undefined || signatureSegment === undefined) {
    throw new JwtError("malformed");
  }

  const unsignedToken = `${headerSegment}.${payloadSegment}`;
  const expectedSignature = createHmac("sha256", secret).update(unsignedToken).digest();
  const actualSignature = Buffer.from(signatureSegment, "base64url");
  if (
    actualSignature.length !== expectedSignature.length ||
    !timingSafeEqual(actualSignature, expectedSignature)
  ) {
    throw new JwtError("invalid-signature");
  }

  validateHeader(decodeJson(headerSegment));
  const claims = validateClaims(decodeJson(payloadSegment));
  if (claims.exp <= nowSeconds()) {
    throw new JwtError("expired");
  }
  return claims;
}

function __jaunt_impl_rotateToken(
  token: string,
  secret: string,
  opts?: TokenOptions,
): string {
  const claims = __jaunt_impl_verifyToken(token, secret);
  const issuedAt = Math.max(nowSeconds(), Math.floor(claims.iat) + 1);
  const expiresAt = Math.max(issuedAt + ttlSeconds(opts), Math.floor(claims.exp) + 1);
  return issueToken(claims.sub, secret, issuedAt, expiresAt);
}

class __jaunt_impl_TokenStore {
  readonly #clock: () => number;
  readonly #entries = new Map<string, StoredToken>();

  constructor(clock: () => number = nowSeconds) {
    this.#clock = clock;
  }

  put(subject: string, token: string, exp: number): void {
    this.#entries.set(subject, { token, exp });
  }

  get(subject: string): string | null {
    const entry = this.#entries.get(subject);
    if (entry === undefined || entry.exp <= this.#clock()) {
      return null;
    }
    return entry.token;
  }

  sweep(): number {
    const currentTime = this.#clock();
    let removed = 0;
    for (const [subject, entry] of this.#entries) {
      if (entry.exp <= currentTime) {
        this.#entries.delete(subject);
        removed += 1;
      }
    }
    return removed;
  }

  get size(): number {
    const currentTime = this.#clock();
    let liveEntries = 0;
    for (const entry of this.#entries.values()) {
      if (entry.exp > currentTime) {
        liveEntries += 1;
      }
    }
    return liveEntries;
  }
}

/**
 * Create an HS256-signed JWT.
 *
 * Structure: base64url(header) + "." + base64url(payload) + "." +
 * base64url(signature), where base64url omits "=" padding.
 * - Header is exactly `{"alg":"HS256","typ":"JWT"}`.
 * - Payload is `{"sub":userId,"iat":now,"exp":now+ttl}` with integer unix
 *   seconds; default ttl is 3600 seconds, and a non-integer `ttlSeconds` is
 *   truncated to whole seconds so iat/exp stay integers.
 * - Sign with HMAC-SHA256 using `secret` as the key.
 * - Allow any ttl, including negative, so tests can mint expired tokens.
 *
 * @throws RangeError if `userId` is empty.
 */
export const createToken: typeof __JauntApi.createToken = __jaunt_impl_createToken;
/**
 * Verify an existing token and issue a fresh one for the same subject.
 *
 * Verification errors propagate unchanged. The new token has strictly greater
 * `iat` and `exp` values even when the clock has not advanced or the requested
 * ttl is shorter than the original token's remaining lifetime.
 */
export const rotateToken: typeof __JauntApi.rotateToken = __jaunt_impl_rotateToken;
/**
 * In-memory store of issued tokens with TTL eviction.
 *
 * Expired entries are invisible to every read, whether or not `sweep` ran.
 * Reads never delete an expired entry; only `sweep` removes it, so a later
 * sweep still reports every expired entry that it evicts.
 * The clock is injectable in unix seconds; omission selects real time.
 *
 * This reviewed declaration is the output of the proposed `jaunt design` flow.
 */
export const TokenStore: typeof __JauntApi.TokenStore = __jaunt_impl_TokenStore;
export type TokenStore = __JauntApi.TokenStore;
/**
 * Verify an HS256-signed JWT and return its claims.
 *
 * 1. Split on "."; there must be exactly three non-empty base64url segments.
 * 2. Recompute HMAC-SHA256 over `header.payload` and compare it in constant time.
 * 3. Require header `{ alg: "HS256", typ: "JWT" }`.
 * 4. Parse exactly the {@link Claims} fields with their declared types.
 * 5. Require `exp` to be strictly greater than the current time.
 *
 * @throws JwtError with code `malformed` for structural problems.
 * @throws JwtError with code `invalid-signature` when the HMAC differs.
 * @throws JwtError with code `expired` when `exp` has passed.
 */
export const verifyToken: typeof __JauntApi.verifyToken = __jaunt_impl_verifyToken;
