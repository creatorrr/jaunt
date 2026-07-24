// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:dfbc3fb7ce87d84eac3129a976e634a1d0c925bd551a9102032628b9a23df013
// jaunt:fixture_fingerprint=sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b
// jaunt:vitest_fingerprint=sha256:4962cb1ba597e743b68e1e47c54890379c7e8a12cf4e208aa7aa6108cdbfb423
// jaunt:fast_check_fingerprint=sha256:fdf6617d9d3f4359cc6c80ae721209fdd9056c36296e9d7af50b17effd1cd819
// jaunt:runner_fingerprint=sha256:50d27e7718852bf96ddfee00be5ccf80c718da6d98af5326aabc1a952dfaf8db
// jaunt:prompt_fingerprint=sha256:21a8eb1f5a71ae46d9e9bb608836d3a58713a033efe4c6d99cd0f9c42d98c607
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:skills_fingerprint=462d7ee5b605e739480d217bc7874e1490ce7a1a8d700cb2a516c776f04fbcaf
// jaunt:battery_fingerprint=sha256:254365c84155fe96c5cbc243c60f05f83d7f057bc6ca43cec6252a7c7ebdb4e5
// jaunt:body_digest=sha256:951822419774145bde9af654a8fd4fa01c991259cdfc05ee9d40bea8b6b55ceb

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

test("slugifies the authored title example", () => {
  expect(slugify(" Hello, Jaunt TS! ")).toBe("hello-jaunt-ts");
});
