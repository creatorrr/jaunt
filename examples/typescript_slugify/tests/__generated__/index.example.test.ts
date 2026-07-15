// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:43d502dfad7f439e5e7e03cfc90529997c4107c4bbcf3b83b032c0d810763da7
// jaunt:vitest_fingerprint=sha256:e1d72b03464ea5f6015fff626717b40b076abf906d7c97b33815a9e27eddb28b
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:fac40e5029b8c9fbfc6fea0c4cf45c6543cfd734b55ddbd46d6cf894a3cc74bb
// jaunt:prompt_fingerprint=sha256:21a8eb1f5a71ae46d9e9bb608836d3a58713a033efe4c6d99cd0f9c42d98c607
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:a64c37885e38456b2a4e41c7c8072a984fdba2712ac38ed3bd2cf6b3da1a73d6
// jaunt:body_digest=sha256:951822419774145bde9af654a8fd4fa01c991259cdfc05ee9d40bea8b6b55ceb

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

test("slugifies the authored title example", () => {
  expect(slugify(" Hello, Jaunt TS! ")).toBe("hello-jaunt-ts");
});
