/**
 * Authored test specs for the tokens module — the TS twin of
 * examples/jwt_auth/tests/specs.py. `jaunt test` generates
 * tests/__generated__/tokens.example.test.ts from these stubs; the TSDoc is
 * the test's behavioral contract, and `targets` are real identifiers the
 * checker resolves.
 *
 * The filename deliberately does not match `*.test.ts`, so vitest never
 * collects the stubs themselves (the analog of Python's `__test__ = False`).
 */
import * as jaunt from "../src/jaunt/index.ts";
import { createToken, rotateToken, verifyToken } from "../src/tokens/specs.ts";

jaunt.magicModule();

/**
 * Roundtrip create + verify:
 * - token = createToken("user-42", "s3cret")
 * - claims = verifyToken(token, "s3cret")
 * - claims.sub === "user-42" and claims.exp > claims.iat
 */
export function roundtripCreateAndVerify(): void {
  jaunt.testSpec({ targets: [createToken, verifyToken] });
}

/**
 * Wrong secret rejects with JwtError code "invalid-signature".
 */
export function wrongSecretRejects(): void {
  jaunt.testSpec({ targets: [verifyToken] });
}

/**
 * Rotation preserves the subject and strictly advances iat/exp, even when
 * both calls land in the same clock second.
 */
export function rotationAdvancesTimestamps(): void {
  jaunt.testSpec({ targets: [rotateToken] });
}
