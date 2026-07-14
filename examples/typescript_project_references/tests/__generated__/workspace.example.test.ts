// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:5e82905133ea007e2411af78b1c42783d341d40a7d4d553e110974d40e0d7e09
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:784f8b7a06ae4b2e79b4d5f349b1e530713d3168aaaeba4a714fd68d44b772fa
// jaunt:prompt_fingerprint=sha256:7236f0285e6d7080553fc46d6d883a9ab36c5c712332db9fb815979f46c25fc4
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:86dabfffcbb483bb12d6def78c7781da38a423b0429fa4e46b5e486e5a021fb2
// jaunt:body_digest=sha256:8ce8dcb90c9ca79e413c8ab8eddb277824d5e9e972efad9a8d078be57a5542e3

import { expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

test("slugify converts a spaced title to a lowercase ASCII URL slug", () => {
  expect(slugify("  Project\tReferences!  ")).toBe("project-references");
});

test("normalizeSpacing trims and collapses ASCII whitespace", () => {
  expect(normalizeSpacing("  Jaunt\tTS  ")).toBe("Jaunt TS");
});
