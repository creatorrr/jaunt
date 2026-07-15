// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:c928b5532fb470e586878858f889fb357268f42cd311798eeab678a99393bf97
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:983831719868a4868d24a5fad83043deeade789aa0da72424188bb445f318553
import type * as __JauntApi from "./index.api.js";
import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";

function __jaunt_impl_slugify(value: string): string {
  return normalizeSpacing(value)
    .replace(/[A-Z]/g, (character: string): string => character.toLowerCase())
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

Object.defineProperty(__jaunt_impl_slugify, "name", { value: "slugify", configurable: true });
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
export const slugify: typeof __JauntApi.slugify = __jaunt_impl_slugify;
