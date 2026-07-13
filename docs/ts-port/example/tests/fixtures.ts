/**
 * Shared vitest fixtures — the `conftest.py` analog. A derived case
 * declaring `@fixtures clock` in its contract renders against this extended
 * `test`, and a missing or misspelled fixture is a *compile* error in the
 * battery — pytest resolves fixtures by name at collection time, so the TS
 * port catches this class of mistake strictly earlier.
 */
import { test as base } from "vitest";

export interface FakeClock {
  now(): number;
  advance(seconds: number): void;
}

export const test = base.extend<{ clock: FakeClock }>({
  clock: async ({}, use) => {
    let t = 1_700_000_000;
    await use({
      now: () => t,
      advance: (seconds: number) => {
        t += seconds;
      },
    });
  },
});
