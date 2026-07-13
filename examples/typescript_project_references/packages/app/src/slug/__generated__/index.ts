// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:7cefbcc49060158324f2118122b0c1ee22fb7d94c3b810ecd83f343faaccefda
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:ea467c7c9aa5a62e78a124793f4c646ab17c67796aca42e2f9a281688aa01570
import type * as __JauntApi from "./index.api.js";
import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";

function __jaunt_impl_slugify(value: string): string {
  return normalizeSpacing(value)
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();
}

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
