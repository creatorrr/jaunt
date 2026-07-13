// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=derived
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:d107708114b6616c3f8fb36c796b6d6f5a133fc4e710cc6e78cfede8f9e1fb4c
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:d0b6c4b28232f78e164e83f2306c5a794fc0a04bf55390d4781f9b235a2faeec
// jaunt:prompt_fingerprint=sha256:2cbbf4c5fa043c5528d29d226004a63e9024add1bf6c52df4442825a1faa0953
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:57d9f4600763c41d7c158c58ebb9ced71c9529cca2a1331f2159336ad7f3ef3c
// jaunt:body_digest=sha256:9f6581add1a6c0c4413a1f88b029accddf3aa2cb7acbbd11b0c3d1ad73373a1e

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

test.each([
  ["D001", "ABCxyz019", "abcxyz019"],
  ["D002", "alpha...___---beta", "alpha-beta"],
  ["D003", "\t /Alpha Beta/ \n", "alpha-beta"],
  ["D004", "one@#$%^&*()two", "one-two"],
  ["D005", "already-normalized", "already-normalized"],
  ["D006", "42 Meaning OF Life 7", "42-meaning-of-life-7"],
] as const)("%s", (_caseId, title, expected) => {
  expect(slugify(title)).toBe(expected);
});

test("D007", () => {
  expect(slugify(" \t!@#$%^&*()_+-=[]{};':\",./<>?\n")).toBe("");
});

test("D008", () => {
  expect(slugify("")).toBe("");
});
