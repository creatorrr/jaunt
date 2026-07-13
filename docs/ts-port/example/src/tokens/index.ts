/**
 * Public surface of the tokens package (the barrel).
 *
 * Two type-flow regimes (DESIGN.md, "guideposts done the TS way"):
 *  - Declared APIs re-export from the spec module: consumers typecheck
 *    against the signatures the author wrote, and at runtime the jaunt
 *    resolver serves the generated implementations through that same path.
 *  - Designed APIs (docstring-only specs, here TokenStore) re-export from
 *    __generated__: both the types and the runtime come from what the model
 *    designed. This replaces Python's `.pyi`-shadows-`.py` trick, which has
 *    no TS equivalent.
 */
export { createToken, verifyToken, rotateToken, JwtError, nowSeconds } from "./specs.ts";
export type { Claims, JwtErrorCode } from "./specs.ts";
export { TokenStore } from "./__generated__/specs.ts";
