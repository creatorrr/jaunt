/**
 * Authored test intent. The `.jaunt-test.ts` suffix excludes this private
 * analysis input from Vitest collection and production/test emit.
 */
import * as jaunt from "@usejaunt/ts/spec";
import {
  createToken,
  rotateToken,
  verifyToken,
} from "../src/tokens/index.jaunt.js";

jaunt.magicModule();

/**
 * Roundtrip create + verify:
 * - create a token for `user-42` with secret `s3cret`;
 * - verify it with the same secret;
 * - require subject `user-42` and `exp > iat`.
 * Import runtime symbols through the public `../src/tokens/index.js` facade only.
 * Use string methods such as `at()` or `charAt()` instead of computed property access.
 */
export function roundtripCreateAndVerify(): void {
  jaunt.testSpec({ targets: [createToken, verifyToken] });
}

/**
 * A token verified with a different secret raises `invalid-signature`.
 * Import runtime symbols through the public `../src/tokens/index.js` facade only,
 * and avoid computed property access in the generated battery.
 */
export function wrongSecretRejects(): void {
  jaunt.testSpec({ targets: [verifyToken] });
}

/**
 * Rotation preserves the subject and strictly advances `iat` and `exp`.
 * Import runtime symbols through the public `../src/tokens/index.js` facade only,
 * and avoid computed property access in the generated battery.
 */
export function rotationAdvancesTimestamps(): void {
  jaunt.testSpec({ targets: [rotateToken] });
}
