// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:fbcbc4d22cae2b3450e3af658bed40addf8d49a96a2016bb0b8ab3bdbbdd41aa
// jaunt:fixture_fingerprint=sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b
// jaunt:vitest_fingerprint=sha256:4962cb1ba597e743b68e1e47c54890379c7e8a12cf4e208aa7aa6108cdbfb423
// jaunt:fast_check_fingerprint=sha256:83e236e47ebc48763b1b0308bc0beb28122a39d4d2a15d15dce3949110ae79fa
// jaunt:runner_fingerprint=sha256:52602d0c1edc81cb7e6da304e5fbf595cf43146144afa8a8aaf7acf0f1e7880c
// jaunt:prompt_fingerprint=sha256:2cbbf4c5fa043c5528d29d226004a63e9024add1bf6c52df4442825a1faa0953
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:6f05636f92f284296490b10e1e4cc03570ccf27165a77939195499927890b929
// jaunt:body_digest=sha256:bf5343caac0c74a367f1ee05071ae64c6253620a6d2c78178b911eaa7b9ed9af

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

const cases: ReadonlyArray<readonly [string, string, string]> = [
  ["d-001", "JAUNT", "jaunt"],
  ["d-002", "Version 2 Build 17", "version-2-build-17"],
  ["d-003", "alpha___... beta", "alpha-beta"],
  ["d-004", "---Alpha---", "alpha"],
  ["d-005", "12345", "12345"],
  ["d-006", " \t!@#$%^&*()\n", ""],
  ["d-007", "你好é", ""],
  ["d-008", "a/B\\C:D", "a-b-c-d"],
];

test.each(cases)("%s", (_caseId, title, expected) => {
  expect(slugify(title)).toBe(expected);
});
