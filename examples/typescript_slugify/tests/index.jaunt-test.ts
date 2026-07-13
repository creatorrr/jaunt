import * as jaunt from "@usejaunt/ts/spec";
import { slugify } from "../src/index.jaunt.js";

jaunt.magicModule();

/**
 * Explicit examples include spaces, punctuation, repeated separators, an
 * already-normalized value, and input with no ASCII letters or digits.
 */
export function slugExamples(): void {
  jaunt.testSpec({ targets: [slugify] });
}
