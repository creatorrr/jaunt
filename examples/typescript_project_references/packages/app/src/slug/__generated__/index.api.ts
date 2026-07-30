// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:b114e3532732110f83e7563905f6699c3c6164686324477ef272a837c680ad69
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:9eee56ccbbc8d65de49f715f0bf39e76f532c1f526027dea55323cd68d99d5e6
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
