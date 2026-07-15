// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:src/index
// jaunt:structural=sha256:9dfeab9f8ce2a96993adac3c1f94cbd528583f695f26d5bbbebb57cf8c523c27
// jaunt:prose=sha256:8f49f368d319309f41bb7828c151e17873012884a4a4f6241f0c8a555cac0e43
// jaunt:api=sha256:a64bc2183e8218fc85f7f9e38e970cf5335001f612fb2cda1e18ece78d27cce0
/**
 * Convert a title to a lowercase URL slug.
 *
 * Trim leading and trailing whitespace, treat every non-empty run of
 * non-alphanumeric ASCII characters as one hyphen, and omit leading/trailing
 * hyphens. An input containing no ASCII letters or digits returns an empty
 * string.
 *
 * @example slugify(" Hello, Jaunt TS! ") // => "hello-jaunt-ts"
 */
export declare function slugify(title: string): string;
