// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/workspace.jaunt-test.ts
// jaunt:test_spec_digest=sha256:9c467f35f3e59eaadb19aa958d10a76b439ce6edafa34db4cb8985f3a3979149
// jaunt:target_api_digest=sha256:5e82905133ea007e2411af78b1c42783d341d40a7d4d553e110974d40e0d7e09
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:68ea27c23927294821104fb6e5ff72601122f29737a2d94654138edf4836f6d0
// jaunt:runner_fingerprint=sha256:886498db38c2728b4b792da111cfb2780c9000391fc5a4b2a01eb590952858d5
// jaunt:prompt_fingerprint=sha256:264ea8cccd2cb754b5ab7f46bf018f7245195b9851026a0edb87ab956359d82d
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:1a54a0b80fa2a7e9bee8c00c81fb54f9f73eb67d1208770e239a139f231607bd
// jaunt:body_digest=sha256:dacccb284874639f058ffc29d22044b215727aa7f90021a430a8b8395d799804

import { expect, test } from "vitest";

import { slugify } from "../../packages/app/src/slug/index.js";
import { normalizeSpacing } from "../../packages/core/src/normalize/index.js";

const normalizationCases: ReadonlyArray<readonly [string, string, string]> = [
  ["d001", "\t\r\n Alpha\v\fBeta \tGamma\r\n", "Alpha Beta Gamma"],
  ["d002", " \t\n\v\f\r ", ""],
  ["d003", "", ""],
  ["d004", "\u00a0  Alpha\tBeta  \u2003", "\u00a0 Alpha Beta \u2003"],
  ["d005", "Already compact", "Already compact"],
];

for (const [caseId, input, expected] of normalizationCases) {
  test(caseId, () => {
    expect(normalizeSpacing(input)).toBe(expected);
  });
}

const slugCases: ReadonlyArray<readonly [string, string, string]> = [
  ["d006", "API42___Ready...NOW", "api42-ready-now"],
  ["d007", "---Café déjà vu---", "caf-d-j-vu"],
  ["d008", "123ABCxyz789", "123abcxyz789"],
  ["d009", "中文🙂", ""],
  ["d010", " A--B__ C!!!D ", "a-b-c-d"],
  ["d011", "İstanbul AKB", "stanbul-a-b"],
  ["d012", "9", "9"],
];

for (const [caseId, input, expected] of slugCases) {
  test(caseId, () => {
    expect(slugify(input)).toBe(expected);
  });
}
