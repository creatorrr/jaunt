// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:011fba42e1a6f8d1eb9a2c6c468c9f099052f437ed9e3983a5838e48c7963db4
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:3114b139d6bde5d82beb36dae934badd781923fca5fbb0c72eae457211f3d898
// jaunt:prompt_fingerprint=sha256:264ea8cccd2cb754b5ab7f46bf018f7245195b9851026a0edb87ab956359d82d
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:5df9899790cd7d4421832fd49dac0e65c1e6a61e3dbddccf8ebbec0243067b14
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
