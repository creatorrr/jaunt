// ⚙️ jaunt:generated — conformance check. DO NOT EDIT.
//
// Declared APIs are checker-enforced: each generated implementation must be
// *assignable* to the signature declared in the spec module. Drift is a
// type error surfaced by the `tsc` pass inside jaunt's validation step.
// This replaces the Python port's canonical-signature JSON comparison and
// the whole `@jaunt.sig` sealed tier: conformance is the default, it
// composes with generics and overloads, and Liskov-shaped widening (accept
// more, return narrower) is allowed — exact-text matching is not the TS
// notion of "same signature".
import type * as spec from "../specs.ts";

import * as gen from "./specs.ts";

gen.createToken satisfies typeof spec.createToken;
gen.verifyToken satisfies typeof spec.verifyToken;
gen.rotateToken satisfies typeof spec.rotateToken;

// TokenStore is a designed API (docstring-only spec): its type flows to
// consumers *from* the generated module via the barrel, so there is no
// declared signature to check it against — by construction it cannot drift.
