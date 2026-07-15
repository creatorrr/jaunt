// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:9f541571619f901cb091cc092c1d18d9ce863e90276e3fd70c7a95d760fc804f
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:3114b139d6bde5d82beb36dae934badd781923fca5fbb0c72eae457211f3d898
// jaunt:prompt_fingerprint=sha256:2cbbf4c5fa043c5528d29d226004a63e9024add1bf6c52df4442825a1faa0953
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:e1284e1748dabaebcc4d0a1477b70405a1b0d9d138a6e86e267d1a9672393ddf
// jaunt:body_digest=sha256:15908a2922828697edebefe49a25c807ea53adb70f2e35f6f42d02d3f234e42c

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

const cases: ReadonlyArray<readonly [string, string, string]> = [
  ["d-001", "Alpha BETA gamma", "alpha-beta-gamma"],
  ["d-002", "one___two...three", "one-two-three"],
  ["d-003", "---Edge Boundaries---", "edge-boundaries"],
  ["d-004", "version 2 release 10", "version-2-release-10"],
  ["d-005", "A\té\nB—C", "a-b-c"],
  ["d-006", " \t—_!?\n ", ""],
  ["d-007", "ABC123xyz", "abc123xyz"],
];

for (const [caseId, input, expected] of cases) {
  test(caseId, () => {
    expect(slugify(input)).toBe(expected);
  });
}
