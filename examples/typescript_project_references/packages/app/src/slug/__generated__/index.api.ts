// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:a20c485c1a8ce4aa263bb88d898e88df8c64b38cd78767b1cb9c65c0e71c4f08
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:25dc8ed149b1baade404b433db359d1b7c0503d43bc21ba25c670e75fdaa9708
import type { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";

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
export declare function slugify(value: string): string;
