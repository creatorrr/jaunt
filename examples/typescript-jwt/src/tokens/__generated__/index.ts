// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:src/tokens/index
// jaunt:structural=sha256:d1c386d5904408d9aaeea55dd80fc9e5793a33bb5c0d8d054d4c899a1526091b
// jaunt:prose=sha256:616b419f63caf931be25251541e08d964b503d846e7f0c727d8dbc6468bcce3c
// jaunt:api=sha256:ed58497e8bd0247bf71b42ce39e8f04cecf5466c66a086e157648450b75f7ae8
import type * as __JauntApi from "./index.api.js";
import { createHmac, timingSafeEqual } from "node:crypto";

import { JwtError, nowSeconds } from "../index.context.js";

type TokenOptions = { ttlSeconds?: number };
type VerifiedClaims = { sub: string; iat: number; exp: number };
type StoredToken = { token: string; exp: number };

const HEADER = { alg: "HS256", typ: "JWT" };
const DEFAULT_TTL_SECONDS = 3600;
const BASE64URL_PATTERN = /^[A-Za-z0-9_-]+$/;

function encodeBase64Url(value: string): string {
  return Buffer.from(value, "utf8").toString("base64url");
}

function decodeBase64Url(segment: string): string {
  if (!BASE64URL_PATTERN.test(segment)) {
    throw new JwtError("malformed");
  }

  const decoded = Buffer.from(segment, "base64url");
  if (decoded.toString("base64url") !== segment) {
    throw new JwtError("malformed");
  }
  return decoded.toString("utf8");
}

function parseJson(source: string): unknown {
  return JSON.parse(source);
}

function isRecord(value: unknown): value is object {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function hasExactKeys(value: object, expected: readonly string[]): boolean {
  const keys = Object.keys(value).sort();
  const sortedExpected = [...expected].sort();
  return (
    keys.length === sortedExpected.length &&
    keys.every((key, index) => key === sortedExpected[index])
  );
}

function sign(encodedHeader: string, encodedPayload: string, secret: string): string {
  return createHmac("sha256", secret)
    .update(`${encodedHeader}.${encodedPayload}`)
    .digest("base64url");
}

function mintToken(subject: string, secret: string, issuedAt: number, ttlSeconds: number): string {
  const encodedHeader = encodeBase64Url(JSON.stringify(HEADER));
  const encodedPayload = encodeBase64Url(
    JSON.stringify({ sub: subject, iat: issuedAt, exp: issuedAt + ttlSeconds }),
  );
  const signature = sign(encodedHeader, encodedPayload, secret);
  return `${encodedHeader}.${encodedPayload}.${signature}`;
}

function normalizedTtl(opts?: TokenOptions): number {
  return Math.trunc(opts?.ttlSeconds ?? DEFAULT_TTL_SECONDS);
}

const __jaunt_impl_createToken = (
  userId: string,
  secret: string,
  opts?: TokenOptions,
): string => {
  if (userId.length === 0) {
    throw new RangeError("userId must not be empty");
  }

  return mintToken(userId, secret, nowSeconds(), normalizedTtl(opts));
};

const __jaunt_impl_verifyToken = (token: string, secret: string): VerifiedClaims => {
  const segments = token.split(".");
  if (segments.length !== 3 || segments.some((segment) => segment.length === 0)) {
    throw new JwtError("malformed");
  }

  const encodedHeader = segments[0];
  const encodedPayload = segments[1];
  const encodedSignature = segments[2];
  if (encodedHeader === undefined || encodedPayload === undefined || encodedSignature === undefined) {
    throw new JwtError("malformed");
  }

  const headerSource = decodeBase64Url(encodedHeader);
  const payloadSource = decodeBase64Url(encodedPayload);
  decodeBase64Url(encodedSignature);

  const expectedSignature = Buffer.from(
    sign(encodedHeader, encodedPayload, secret),
    "base64url",
  );
  const actualSignature = Buffer.from(encodedSignature, "base64url");
  const comparableSignature = Buffer.alloc(expectedSignature.length);
  actualSignature.copy(comparableSignature, 0, 0, expectedSignature.length);
  const signatureContentsMatch = timingSafeEqual(comparableSignature, expectedSignature);
  const signaturesMatch =
    actualSignature.length === expectedSignature.length && signatureContentsMatch;
  if (!signaturesMatch) {
    throw new JwtError("invalid-signature");
  }

  let header: unknown;
  let payload: unknown;
  try {
    header = parseJson(headerSource);
    payload = parseJson(payloadSource);
  } catch {
    throw new JwtError("malformed");
  }

  if (
    !isRecord(header) ||
    !hasExactKeys(header, ["alg", "typ"]) ||
    Reflect.get(header, "alg") !== "HS256" ||
    Reflect.get(header, "typ") !== "JWT"
  ) {
    throw new JwtError("malformed");
  }

  if (!isRecord(payload) || !hasExactKeys(payload, ["sub", "iat", "exp"])) {
    throw new JwtError("malformed");
  }

  const sub: unknown = Reflect.get(payload, "sub");
  const iat: unknown = Reflect.get(payload, "iat");
  const exp: unknown = Reflect.get(payload, "exp");
  if (typeof sub !== "string" || typeof iat !== "number" || typeof exp !== "number") {
    throw new JwtError("malformed");
  }
  if (exp <= nowSeconds()) {
    throw new JwtError("expired");
  }

  return { sub, iat, exp };
};

const __jaunt_impl_rotateToken = (
  token: string,
  secret: string,
  opts?: TokenOptions,
): string => {
  const claims = __jaunt_impl_verifyToken(token, secret);
  const ttlSeconds = normalizedTtl(opts);
  const issuedAt = Math.max(nowSeconds(), claims.iat + 1, claims.exp - ttlSeconds + 1);
  return mintToken(claims.sub, secret, issuedAt, ttlSeconds);
};

class __jaunt_impl_TokenStore {
  readonly #clock: () => number;
  readonly #entries = new Map<string, StoredToken>();

  constructor(clock?: () => number) {
    this.#clock = clock ?? nowSeconds;
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
    const now = this.#clock();
    let removed = 0;
    for (const [subject, entry] of this.#entries) {
      if (entry.exp <= now) {
        this.#entries.delete(subject);
        removed += 1;
      }
    }
    return removed;
  }

  get size(): number {
    const now = this.#clock();
    let liveEntries = 0;
    for (const entry of this.#entries.values()) {
      if (entry.exp > now) {
        liveEntries += 1;
      }
    }
    return liveEntries;
  }
}

Object.defineProperty(__jaunt_impl_createToken, "name", { value: "createToken", configurable: true });
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
Object.defineProperty(__jaunt_impl_rotateToken, "name", { value: "rotateToken", configurable: true });
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
Object.defineProperty(__jaunt_impl_verifyToken, "name", { value: "verifyToken", configurable: true });
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
