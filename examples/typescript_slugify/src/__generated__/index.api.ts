// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:src/index
// jaunt:structural=sha256:3d86c64d77e229f0a6d18d5e0aeaf26226176dfc147b681e4e4f85a91a2b3bf6
// jaunt:prose=sha256:8f49f368d319309f41bb7828c151e17873012884a4a4f6241f0c8a555cac0e43
// jaunt:api=sha256:01799e333c4e039315b0250de926552bcea2e8fef2ac1e85f933fd63d1be736f
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
