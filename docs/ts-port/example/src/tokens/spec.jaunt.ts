/**
 * JWT tokens — a Jaunt-for-TypeScript spec module (the TS twin of
 * examples/jwt_auth/src/jwt_demo/specs.py).
 *
 * The `.jaunt.ts` suffix marks this file as a spec *input*: it is never
 * imported at runtime. Types flow out of it only through erased positions
 * (`typeof import("./spec.jaunt.ts")` annotations in the generated
 * implementation, `export type` in the facade), so consumers carry no jaunt
 * runtime dependency and there is no raw-spec evaluation.
 *
 * `jaunt.magicModule()` below governs every top-level export in this file.
 * Classification is by body shape, exactly like Python module-magic:
 *   - a body that is exactly `jaunt.magic(...)` is a spec stub;
 *   - interfaces and type aliases are handwritten *type* context (they are
 *     contracts by nature — there is nothing to generate);
 *   - executable handwritten context does NOT live here — it goes in
 *     ./context.ts, an ordinary module. That rule (spec files hold no
 *     runtime code beyond stubs) is what eliminates the lexical-binding
 *     trap: nothing in this file can accidentally call a stub.
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
 * @throws JwtError (see ./context.ts) code "malformed" for structural
 *   problems (wrong segment count, non-base64url characters, bad JSON,
 *   wrong header, wrong or extra payload fields).
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
 * In-memory store of issued tokens with TTL eviction.
 *
 * Expired entries must be invisible to *every* read — lookups and counts
 * alike — regardless of whether a sweep has run. The clock is injectable
 * (unix seconds; omitted means real time) so tests control expiry
 * deterministically.
 *
 * This declaration started as a docstring-only spec; `jaunt design`
 * proposed the member signatures below as a patch to this file, which was
 * accepted in review (see DESIGN.md). From that point on it is an ordinary
 * declared API: conformance-checked, types never lie.
 */
export class TokenStore {
  constructor(clock?: () => number) {
    jaunt.magic();
  }

  /** Record the live token for a subject, replacing any previous one. */
  put(subject: string, token: string, exp: number): void {
    jaunt.magic();
  }

  /** The live token for a subject, or null. Expired entries are invisible. */
  get(subject: string): string | null {
    return jaunt.magic();
  }

  /** Drop every expired entry; return how many were removed. */
  sweep(): number {
    return jaunt.magic();
  }

  /** Count of live entries — expiry is honored even without a sweep. */
  get size(): number {
    return jaunt.magic();
  }
}
