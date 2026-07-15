// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:43d502dfad7f439e5e7e03cfc90529997c4107c4bbcf3b83b032c0d810763da7
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:fac40e5029b8c9fbfc6fea0c4cf45c6543cfd734b55ddbd46d6cf894a3cc74bb
// jaunt:prompt_fingerprint=sha256:2cbbf4c5fa043c5528d29d226004a63e9024add1bf6c52df4442825a1faa0953
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:2fe980e3e6996a8b8c46cd5d20a328627780d8467cca3127eaf803fc0e1a01e3
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
