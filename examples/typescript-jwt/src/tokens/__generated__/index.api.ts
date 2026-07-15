// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:src/tokens/index
// jaunt:structural=sha256:61f9af1b6b55f6962955e91d135b6f7f71db0089a70257ce27a8d6787e991dcd
// jaunt:prose=sha256:616b419f63caf931be25251541e08d964b503d846e7f0c727d8dbc6468bcce3c
// jaunt:api=sha256:752d7162c462105766efe9c6ded06b5a340a3b0edb878500e10c2c48ed5801a9
/**
 * Decoded token payload.
 */
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
export declare function createToken(userId: string, secret: string, opts?: { ttlSeconds?: number }): string;

/**
 * Verify an existing token and issue a fresh one for the same subject.
 *
 * Verification errors propagate unchanged. The new token has strictly greater
 * `iat` and `exp` values even when the clock has not advanced or the requested
 * ttl is shorter than the original token's remaining lifetime.
 */
export declare function rotateToken(token: string, secret: string, opts?: { ttlSeconds?: number }): string;

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
export declare class TokenStore {
  constructor(clock?: () => number);
  /**
   * Record the live token for a subject, replacing any previous one.
   */
  put(subject: string, token: string, exp: number): void;
  /**
   * The live token for a subject, or null. This read never removes an expired entry.
   */
  get(subject: string): null | string;
  /**
   * Drop every expired entry and return how many were removed.
   */
  sweep(): number;
  /**
   * Count of live entries without deleting expired entries or requiring a sweep.
   */
  get size(): number;
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
export declare function verifyToken(token: string, secret: string): Claims;
