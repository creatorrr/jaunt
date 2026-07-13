import * as jaunt from "@usejaunt/ts/spec";
import { slugify } from "../packages/app/src/slug/index.jaunt.js";
import { normalizeSpacing } from "../packages/core/src/normalize/index.jaunt.js";

jaunt.magicModule();

/**
 * Cover empty and whitespace-only values, repeated spaces and tabs, punctuation,
 * already-normalized input, and an app result that proves `slugify` uses the
 * core normalization behavior.
 */
export function workspaceExamples(): void {
  jaunt.testSpec({ targets: [normalizeSpacing, slugify] });
}
