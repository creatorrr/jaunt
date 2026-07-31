// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:3ef0a949d9f3433815690375fd12857d03c44e071d80f52e2c30cbc2793d9c58
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:d52dd510560a400c78bbe28c3fa4b03f57ba013134ae9156ece5e54dccca87a0
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
