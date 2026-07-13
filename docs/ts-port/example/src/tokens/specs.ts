/**
 * JWT tokens — a Jaunt-for-TypeScript spec module (the TS twin of
 * examples/jwt_auth/src/jwt_demo/specs.py).
 *
 * `jaunt.magicModule()` below governs every top-level export in this file.
 * Classification is by body shape, exactly like Python module-magic:
 *   - a body that is exactly `jaunt.magic(...)` is a spec stub;
 *   - a real body is handwritten context the model reads but never touches;
 *   - interfaces and type aliases are always handwritten context (they are
 *     contracts by nature — there is nothing to generate);
 *   - a docstring-only class (empty body + TSDoc) asks the model to design
 *     the API.
 * The TSDoc block preceding each declaration is the behavioral contract:
 * its prose feeds the prose digest (semantic-gate judged), while the
 * declaration itself feeds the structural digest.
 */
import * as jaunt from "../jaunt/index.ts";

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
 * Verification failure. Handwritten context (real body): the model uses it,
 * never regenerates it. Note `@throws JwtError` in the contracts below is
 * checker-resolvable — richer than Python's prose-only error contracts.
 */
export class JwtError extends Error {
  readonly code: JwtErrorCode;

  constructor(code: JwtErrorCode) {
    super(`jwt ${code}`);
    this.name = "JwtError";
    this.code = code;
  }
}

/** Current unix time in whole seconds. Handwritten context. */
export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
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
 * 1. Split on "." — must be exactly 3 non-empty base64url segments.
 * 2. Recompute HMAC-SHA256 over "header.payload"; compare to the signature
 *    segment in constant time.
 * 3. Decode the header — it must be a JSON object with alg "HS256" and typ
 *    "JWT" (anything else, including "none", is malformed).
 * 4. Parse the payload strictly into {@link Claims}: exactly the declared
 *    keys with the declared types — extra keys are malformed.
 * 5. Require `exp` strictly greater than the current time.
 *
 * @throws JwtError code "malformed" for structural problems (wrong segment
 *   count, non-base64url characters, bad JSON, wrong header, wrong or
 *   extra payload fields).
 * @throws JwtError code "invalid-signature" when the HMAC does not match.
 * @throws JwtError code "expired" when `exp` has passed.
 */
export function verifyToken(token: string, secret: string): Claims {
  return jaunt.magic({ deps: [createToken] });
}

/**
 * Verify an existing token and issue a fresh one for the same subject.
 *
 * - Propagate verification errors unchanged.
 * - The rotated token MUST have strictly increasing iat/exp compared to the
 *   input token: if the clock has not advanced (both calls in the same
 *   second), bump iat forward so iat2 > iat1; if the fresh ttl would land
 *   exp at or before the input token's exp (a shorter ttl than the
 *   original), bump exp to the input's exp + 1.
 */
export function rotateToken(
  token: string,
  secret: string,
  opts?: { ttlSeconds?: number },
): string {
  return jaunt.magic({ deps: [createToken, verifyToken] });
}

/**
 * In-memory store of issued tokens with TTL eviction — a *designed* API
 * (docstring-only spec): jaunt designs the members from this contract.
 *
 * Callers need to: record the live token issued to a subject (replacing any
 * previous one), look up a subject's live token, report how many live
 * tokens are held, and sweep expired entries in bulk. Expired entries must
 * be invisible to *every* read — lookups and counts alike — regardless of
 * whether a sweep has run. Take the clock as an injectable `() => number`
 * (unix seconds) defaulting to real time, so tests can control expiry
 * deterministically.
 *
 * Consumers import this through the package barrel (./index.ts), where its
 * types flow from the generated module — see DESIGN.md on designed APIs.
 */
export class TokenStore {}
