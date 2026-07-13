// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt build`.
// jaunt:tool_version=0.0.0-ts-preview
// jaunt:source_module=tokens/spec.jaunt
// jaunt:digest_scheme=ts1
// jaunt:module_digest=sha256:0000000000000000000000000000000000000000000000000000000000000000
//
// (Preview note: hand-written to illustrate the output contract of the
// TypeScript port; a real build stamps true digests above.)
//
// Conformance mechanism: every export is annotated with the authored type
// (`typeof import("../spec.jaunt.ts").<name>`). The annotation both
// *enforces* assignability at typecheck time (drift in a generated
// signature is a compile error) and *pins* the consumer-facing type to the
// authored contract — consumers never see an accidentally-wider generated
// type. The spec import is type-only, so it is fully erased: the spec file
// never loads at runtime.
import { createHmac, timingSafeEqual } from "node:crypto";

import { JwtError, nowSeconds } from "../context.ts";
import type { Claims } from "../spec.jaunt.ts";

const HEADER_B64 = Buffer.from(JSON.stringify({ alg: "HS256", typ: "JWT" })).toString(
  "base64url",
);

const SEGMENT = /^[A-Za-z0-9_-]+$/;

function sign(signingInput: string, secret: string): string {
  return createHmac("sha256", secret).update(signingInput).digest("base64url");
}

// Internal helper: the model is free to invent these — they are not part of
// the declared public type, so conformance does not constrain them (the
// freedom Python called "guidepost", relocated to where types don't reach).
function mint(claims: Claims, secret: string): string {
  const payloadB64 = Buffer.from(JSON.stringify(claims)).toString("base64url");
  const signingInput = `${HEADER_B64}.${payloadB64}`;
  return `${signingInput}.${sign(signingInput, secret)}`;
}

function createTokenImpl(
  userId: string,
  secret: string,
  opts?: { ttlSeconds?: number },
): string {
  if (userId === "") {
    throw new RangeError("userId must be non-empty");
  }
  const iat = nowSeconds();
  const ttl = Math.trunc(opts?.ttlSeconds ?? 3600);
  return mint({ sub: userId, iat, exp: iat + ttl }, secret);
}

function isClaims(value: unknown): value is Claims {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    Object.keys(candidate).length === 3 &&
    typeof candidate.sub === "string" &&
    Number.isInteger(candidate.iat) &&
    Number.isInteger(candidate.exp)
  );
}

function isHs256Header(value: unknown): boolean {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return candidate.alg === "HS256" && candidate.typ === "JWT";
}

function verifyTokenImpl(token: string, secret: string): Claims {
  const parts = token.split(".");
  if (parts.length !== 3 || !parts.every((part) => SEGMENT.test(part))) {
    throw new JwtError("malformed");
  }
  const [headerB64, payloadB64, signatureB64] = parts;
  const expected = Buffer.from(sign(`${headerB64}.${payloadB64}`, secret));
  const got = Buffer.from(signatureB64);
  if (got.length !== expected.length || !timingSafeEqual(got, expected)) {
    throw new JwtError("invalid-signature");
  }
  let header: unknown;
  let payload: unknown;
  try {
    header = JSON.parse(Buffer.from(headerB64, "base64url").toString("utf8"));
    payload = JSON.parse(Buffer.from(payloadB64, "base64url").toString("utf8"));
  } catch {
    throw new JwtError("malformed");
  }
  if (!isHs256Header(header) || !isClaims(payload)) {
    throw new JwtError("malformed");
  }
  if (payload.exp <= nowSeconds()) {
    throw new JwtError("expired");
  }
  return payload;
}

function rotateTokenImpl(
  token: string,
  secret: string,
  opts?: { ttlSeconds?: number },
): string {
  const claims = verifyTokenImpl(token, secret);
  const ttl = Math.trunc(opts?.ttlSeconds ?? 3600);
  // Contract: strictly increasing iat/exp even within the same clock second,
  // and even when the fresh ttl is shorter than the input token's remaining
  // lifetime.
  const iat = Math.max(nowSeconds(), claims.iat + 1);
  const exp = Math.max(iat + ttl, claims.exp + 1);
  return mint({ sub: claims.sub, iat, exp }, secret);
}

class TokenStoreImpl {
  readonly #entries = new Map<string, { token: string; exp: number }>();
  readonly #clock: () => number;

  constructor(clock: () => number = nowSeconds) {
    this.#clock = clock;
  }

  put(subject: string, token: string, exp: number): void {
    this.#entries.set(subject, { token, exp });
  }

  get(subject: string): string | null {
    const entry = this.#entries.get(subject);
    if (entry === undefined) return null;
    if (entry.exp <= this.#clock()) {
      this.#entries.delete(subject);
      return null;
    }
    return entry.token;
  }

  sweep(): number {
    const now = this.#clock();
    let removed = 0;
    for (const [subject, entry] of this.#entries) {
      if (entry.exp <= now) {
        this.#entries.delete(subject);
        removed += 1;
      }
    }
    return removed;
  }

  /** Live entries only — expired entries are invisible to every read. */
  get size(): number {
    this.sweep();
    return this.#entries.size;
  }
}

// --- authored-type-annotated exports (the conformance boundary) ---

export const createToken: typeof import("../spec.jaunt.ts").createToken = createTokenImpl;
export const verifyToken: typeof import("../spec.jaunt.ts").verifyToken = verifyTokenImpl;
export const rotateToken: typeof import("../spec.jaunt.ts").rotateToken = rotateTokenImpl;

export const TokenStore: typeof import("../spec.jaunt.ts").TokenStore = TokenStoreImpl;
// The class *type* (instance shape) is the authored one, re-exported so
// consumers can write `let s: TokenStore`.
export type TokenStore = import("../spec.jaunt.ts").TokenStore;
