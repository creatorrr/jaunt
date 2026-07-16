// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:cb66e910d604ca980572706bb20e63cf24abc777336d395bab54ca392d08d317
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:8a1a6238f267ca29ed114b0b0d1556951f8e8c7833997518e4fe774ec7ebda44
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
