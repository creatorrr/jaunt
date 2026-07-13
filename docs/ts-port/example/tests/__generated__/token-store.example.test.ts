// ⚙️ jaunt:generated — DO NOT EDIT. Regenerate with `jaunt test`.
// jaunt:tier=example (designed-API coverage for TokenStore)
//
// Designed APIs are imported through the package *barrel* — their types
// flow from the generated module (see src/tokens/index.ts). The `clock`
// fixture comes from tests/fixtures.ts, the conftest.py analog; the
// contract's "injectable clock" clause is what makes expiry testable
// deterministically.
import { expect } from "vitest";

import { TokenStore } from "../../src/tokens/index.ts";
import { test } from "../fixtures.ts";

test("stores and returns live tokens", ({ clock }) => {
  const store = new TokenStore(() => clock.now());
  store.put("user-1", "tok-1", clock.now() + 60);
  expect(store.get("user-1")).toBe("tok-1");
  expect(store.size).toBe(1);
});

test("expired entries are invisible and pruned on read", ({ clock }) => {
  const store = new TokenStore(() => clock.now());
  store.put("user-1", "tok-1", clock.now() + 60);
  clock.advance(61);
  expect(store.get("user-1")).toBeNull();
  expect(store.size).toBe(0);
});

test("sweep drops every expired entry and reports the count", ({ clock }) => {
  const store = new TokenStore(() => clock.now());
  store.put("a", "tok-a", clock.now() + 10);
  store.put("b", "tok-b", clock.now() + 100);
  clock.advance(50);
  expect(store.sweep()).toBe(1);
  expect(store.get("b")).toBe("tok-b");
  expect(store.size).toBe(1);
});
