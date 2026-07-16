// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:src/index
// jaunt:structural=sha256:cd6e82f7c269ff5b6a06c9518b0b378a0ab273f8f0371997d82bbfce267cf6e4
// jaunt:prose=sha256:8f49f368d319309f41bb7828c151e17873012884a4a4f6241f0c8a555cac0e43
// jaunt:api=sha256:b219688ca20499ed7326f4e83ce6a5da06900819e838794651e66cd20714b1b0
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
