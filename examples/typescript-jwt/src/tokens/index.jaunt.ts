/**
 * JWT tokens: a private Jaunt TypeScript spec input.
 *
 * This module is parsed and typechecked by Jaunt but never emitted or executed.
 * Production code imports the deterministic API mirror instead.
 */
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Decoded token payload. */
export interface Claims {
  /** subject (user id) */
  sub: string;
  /** issued-at (unix seconds) */
  iat: number;
  /** expiry (unix seconds) */
  exp: number;
}

export type JwtErrorCode = "malformed" | "invalid-signature" | "expired";

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
export function createToken(
  userId: string,
  secret: string,
  opts?: { ttlSeconds?: number },
): string {
  return jaunt.magic();
}

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
export function verifyToken(token: string, secret: string): Claims {
  return jaunt.magic({ deps: [createToken] });
}

/**
 * Verify an existing token and issue a fresh one for the same subject.
 *
 * Verification errors propagate unchanged. The new token has strictly greater
 * `iat` and `exp` values even when the clock has not advanced or the requested
 * ttl is shorter than the original token's remaining lifetime.
 */
export function rotateToken(
  token: string,
  secret: string,
  opts?: { ttlSeconds?: number },
): string {
  return jaunt.magic({ deps: [createToken, verifyToken] });
}

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
export class TokenStore {
  constructor(clock?: () => number) {
    jaunt.magic();
  }

  /** Record the live token for a subject, replacing any previous one. */
  put(subject: string, token: string, exp: number): void {
    jaunt.magic();
  }

  /** The live token for a subject, or null. This read never removes an expired entry. */
  get(subject: string): string | null {
    return jaunt.magic();
  }

  /** Drop every expired entry and return how many were removed. */
  sweep(): number {
    return jaunt.magic();
  }

  /** Count of live entries without deleting expired entries or requiring a sweep. */
  get size(): number {
    return jaunt.magic();
  }
}
