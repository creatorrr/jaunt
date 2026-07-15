// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:e09ed22c84edd2b32fbd6562e1d487c141738b6f934a8eb7e31cf9aed10663ad
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:1dce47932190ced05d07c75a85e8eb91df18f6e1abc57eed34d32b8cc64b35d8
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
