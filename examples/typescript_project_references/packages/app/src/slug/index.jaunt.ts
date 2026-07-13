import * as jaunt from "@usejaunt/ts/spec";
import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.jaunt.js";

jaunt.magicModule();

/**
 * Convert a title to a lowercase ASCII URL slug.
 *
 * First normalize spacing with `normalizeSpacing`. Lowercase ASCII letters,
 * replace each non-empty run of characters other than ASCII letters and digits
 * with one hyphen, and remove leading or trailing hyphens. Return an empty
 * string when no ASCII letters or digits remain.
 *
 * @example slugify("  Project\tReferences!  ") // => "project-references"
 */
export function slugify(value: string): string {
  return jaunt.magic({ deps: [normalizeSpacing] });
}
