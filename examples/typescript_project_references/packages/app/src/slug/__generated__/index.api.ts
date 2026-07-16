// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:392e8fba907b408843de2fb13706b7184a5b0f3c8509b51278e3314c81236f92
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:15ecdfef5f177d6039458def6e6acc867218807d8eb9c1767b00a6e282e486bd
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
