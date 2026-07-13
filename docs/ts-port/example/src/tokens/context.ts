/**
 * Handwritten executable context for the tokens module — an ordinary
 * module, deliberately separate from ./spec.jaunt.ts.
 *
 * Executable handwritten code lives here (not in the spec file) so it can
 * never capture a stub's lexical binding: anything in this file that needs
 * a governed function imports the public facade like every other consumer.
 * The generated implementation imports this module for real dependencies,
 * and the facade re-exports it verbatim.
 */
import type { JwtErrorCode } from "./spec.jaunt.ts";

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
