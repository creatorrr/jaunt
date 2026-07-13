// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:d107708114b6616c3f8fb36c796b6d6f5a133fc4e710cc6e78cfede8f9e1fb4c
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:886498db38c2728b4b792da111cfb2780c9000391fc5a4b2a01eb590952858d5
// jaunt:prompt_fingerprint=sha256:21a8eb1f5a71ae46d9e9bb608836d3a58713a033efe4c6d99cd0f9c42d98c607
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:9af2899e5330a01ad17ed877c5b556ba5199527acc61165ffe38ec12888570c3
// jaunt:body_digest=sha256:5cf5d6fe29d7d7cd4d4b4d40bfae8610d5463bb8546965dca88f9153b9a07aaf

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

test("converts the authored title example to a lowercase URL slug", () => {
  expect(slugify(" Hello, Jaunt TS! ")).toBe("hello-jaunt-ts");
});
