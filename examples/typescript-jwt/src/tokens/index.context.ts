/**
 * Handwritten executable context for the tokens module.
 *
 * Context is a strict runtime leaf: generated code may import it, but it does
 * not value-import the facade, generated implementation, or private spec.
 * Public types come from the deterministic API mirror.
 */
import type { JwtErrorCode } from "./__generated__/index.api.js";

/** Verification failure with a machine-readable code. */
export class JwtError extends Error {
  readonly code: JwtErrorCode;

  constructor(code: JwtErrorCode) {
    super(`jwt ${code}`);
    this.name = "JwtError";
    this.code = code;
  }
}

/** Current unix time in whole seconds. */
export function nowSeconds(): number {
  return Math.floor(Date.now() / 1000);
}
