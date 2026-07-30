// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:1c42085a73b0023d49e1ef6d934edcf1e99f76f497337766696e81d5278d438f
// jaunt:fixture_fingerprint=sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b
// jaunt:vitest_fingerprint=sha256:4962cb1ba597e743b68e1e47c54890379c7e8a12cf4e208aa7aa6108cdbfb423
// jaunt:fast_check_fingerprint=sha256:ae31ee465001db568a27080b0fee3f043554271a0168c98366d235b45516a1f3
// jaunt:runner_fingerprint=sha256:32a716b44a009f4568cef091e5b73d62290ddca05aa9e57845b96a926c2507d0
// jaunt:prompt_fingerprint=sha256:264ea8cccd2cb754b5ab7f46bf018f7245195b9851026a0edb87ab956359d82d
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:dcc05c7a56105d2b6d06d27605c3f392ef6016f59105d06beeb3c2a4adcf4097
// jaunt:body_digest=sha256:fe856ac831c68b36351f413a23102ce40168fc0c846141d453d4e0b065ef5ed6

import { expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

const normalizationCases: ReadonlyArray<
  readonly [caseId: string, input: string, expected: string]
> = [
  ["d-001", "", ""],
  ["d-002", " \t\n\v\f\r ", ""],
  ["d-003", "\tAlpha\n\rBeta\fGamma\v", "Alpha Beta Gamma"],
  ["d-004", "Already normalized", "Already normalized"],
  ["d-005", "  Alpha\u00a0Beta  ", "Alpha\u00a0Beta"],
];

test.each(normalizationCases)("%s", (_caseId, input, expected) => {
  expect(normalizeSpacing(input)).toBe(expected);
});

const slugCases: ReadonlyArray<
  readonly [caseId: string, input: string, expected: string]
> = [
  ["d-006", "", ""],
  ["d-007", " \t\n\v\f\r ", ""],
  ["d-008", "Alpha42BETA", "alpha42beta"],
  ["d-009", "---Alpha...Beta___42---", "alpha-beta-42"],
  ["d-010", "Crème brûlée", "cr-me-br-l-e"],
  ["d-011", "\tAlpha\n\rBeta\fGamma\v", "alpha-beta-gamma"],
  ["d-012", "123 456", "123-456"],
  ["d-013", "!!!", ""],
];

test.each(slugCases)("%s", (_caseId, input, expected) => {
  expect(slugify(input)).toBe(expected);
});
