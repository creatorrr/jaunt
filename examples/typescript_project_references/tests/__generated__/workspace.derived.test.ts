// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:d993da6ae5cc96c03cf70ec87458f53e00fa96f7626129e8a23a587c02dff9fa
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:d18e1385cd1c7e9204ff14ece768846b47144325b3177f549074baf7038a08d8
// jaunt:prompt_fingerprint=sha256:264ea8cccd2cb754b5ab7f46bf018f7245195b9851026a0edb87ab956359d82d
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:8d04959508e35d1f9e5aa18d61aca68460cd65d180e160f754a487753a9fa60c
// jaunt:body_digest=sha256:8e73f72f24bd9e4cdac001f999dfe9a33959a298ba3222eb5de5b86a7890e0c1

import { describe, expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

describe("normalizeSpacing", () => {
  test.each([
    ["D-N-001", "", ""],
    ["D-N-002", " \t\n\v\f\r ", ""],
    ["D-N-003", "alpha\t\n\v\f\rbeta", "alpha beta"],
    ["D-N-004", "\r\nalpha   beta\t", "alpha beta"],
    ["D-N-005", "alpha\u00a0beta", "alpha\u00a0beta"],
    ["D-N-006", "alpha-beta", "alpha-beta"],
  ] as const)("%s", (_caseId, value, expected) => {
    expect(normalizeSpacing(value)).toBe(expected);
  });
});

describe("slugify", () => {
  test.each([
    ["D-S-001", "", ""],
    ["D-S-002", " \t\n\v\f\r ", ""],
    ["D-S-003", "API Version 2", "api-version-2"],
    ["D-S-004", "---Alpha___Beta...", "alpha-beta"],
    ["D-S-005", "99 Bottles", "99-bottles"],
    ["D-S-006", "caf\u00e9 au lait", "caf-au-lait"],
    ["D-S-007", "\u4f60\u597d", ""],
    ["D-S-008", "a!@#$%^&*()b", "a-b"],
    ["D-S-009", "Already-Normalized", "already-normalized"],
  ] as const)("%s", (_caseId, value, expected) => {
    expect(slugify(value)).toBe(expected);
  });
});
