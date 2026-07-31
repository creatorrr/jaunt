// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example
// jaunt:source=tests/index.jaunt-test.ts
// jaunt:test_spec_digest=sha256:5bbbbbde8f55a8a7b2f3d0e59080f7306f81191cda70f24469227fb6014b44c9
// jaunt:target_api_digest=sha256:fbcbc4d22cae2b3450e3af658bed40addf8d49a96a2016bb0b8ab3bdbbdd41aa
// jaunt:fixture_fingerprint=sha256:74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b
// jaunt:vitest_fingerprint=sha256:4962cb1ba597e743b68e1e47c54890379c7e8a12cf4e208aa7aa6108cdbfb423
// jaunt:fast_check_fingerprint=sha256:83e236e47ebc48763b1b0308bc0beb28122a39d4d2a15d15dce3949110ae79fa
// jaunt:runner_fingerprint=sha256:52602d0c1edc81cb7e6da304e5fbf595cf43146144afa8a8aaf7acf0f1e7880c
// jaunt:prompt_fingerprint=sha256:21a8eb1f5a71ae46d9e9bb608836d3a58713a033efe4c6d99cd0f9c42d98c607
// jaunt:policy_fingerprint=sha256:babe1406e8e4cc1024536374f7e50070a88000c5e80db5f17d2914c1e7752693
// jaunt:battery_fingerprint=sha256:fa70a241a6d1e724d6a535ef372fbc6633c85ade372e17d4bc88b2e851938b3d
// jaunt:body_digest=sha256:5cf5d6fe29d7d7cd4d4b4d40bfae8610d5463bb8546965dca88f9153b9a07aaf

import { expect, test } from "vitest";

import { slugify } from "../../src/index.js";

test("converts the authored title example to a lowercase URL slug", () => {
  expect(slugify(" Hello, Jaunt TS! ")).toBe("hello-jaunt-ts");
});
