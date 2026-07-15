// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:d993da6ae5cc96c03cf70ec87458f53e00fa96f7626129e8a23a587c02dff9fa
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:d18e1385cd1c7e9204ff14ece768846b47144325b3177f549074baf7038a08d8
// jaunt:prompt_fingerprint=sha256:7236f0285e6d7080553fc46d6d883a9ab36c5c712332db9fb815979f46c25fc4
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:094550e450ff7544283ceba97f24f7855193ac9c06a1fda9c9ad6639ca596531
// jaunt:body_digest=sha256:dbb104aeb5edc5c44430a37b2d1e43ee6b14c65357cb07323645ab3c33ff1b61

import { expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

test("slugify converts the documented title to a lowercase ASCII slug", () => {
  expect(slugify("  Project\tReferences!  ")).toBe("project-references");
});

test("normalizeSpacing trims and collapses the documented ASCII whitespace", () => {
  expect(normalizeSpacing("  Jaunt\tTS  ")).toBe("Jaunt TS");
});
