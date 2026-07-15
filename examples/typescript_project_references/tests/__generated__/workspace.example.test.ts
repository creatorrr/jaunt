// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:814a1de2afaeca48098b0012907e3947fd2689727aa921be528e99a5daa679db
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:98e5fd319aba9e863cec966aac91d1003196241837f6b0cc2a716e558bf057ce
// jaunt:prompt_fingerprint=sha256:7236f0285e6d7080553fc46d6d883a9ab36c5c712332db9fb815979f46c25fc4
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:8347b568828c50f747b3c7a80faaff6f637cc111361ab10fe52202ce0a401922
// jaunt:body_digest=sha256:1e3e43a900e58b55c965ac6bd7d1684cefd07bca93597e20de4a0b36d5bc5bb7

import { describe, expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

describe("authored examples", () => {
  test("slugify converts a spaced title to a lowercase ASCII URL slug", () => {
    expect(slugify("  Project\tReferences!  ")).toBe("project-references");
  });

  test("normalizeSpacing trims and collapses ASCII whitespace", () => {
    expect(normalizeSpacing("  Jaunt\tTS  ")).toBe("Jaunt TS");
  });
});
