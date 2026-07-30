// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:1e7e9badf79f44e887c028dd441e98dfc492f99adc4d71e800263c5706a57553
// jaunt:fixture_fingerprint=sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b
// jaunt:vitest_fingerprint=sha256:4962cb1ba597e743b68e1e47c54890379c7e8a12cf4e208aa7aa6108cdbfb423
// jaunt:fast_check_fingerprint=sha256:ae31ee465001db568a27080b0fee3f043554271a0168c98366d235b45516a1f3
// jaunt:runner_fingerprint=sha256:afec5dc0900acd7cb717e221063d6e336518eb9d08b544165343d95bd56522d2
// jaunt:prompt_fingerprint=sha256:7236f0285e6d7080553fc46d6d883a9ab36c5c712332db9fb815979f46c25fc4
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:6815da6328e2c00615ca8c0a0cbd2aac4974448558a6ee807a5cc4c56eb80565
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
