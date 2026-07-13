// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:src/index
// jaunt:structural=sha256:23a67a585a57b6b12f7fed9afe069f6d85c316423e96ec6c197972d70329da27
// jaunt:prose=sha256:8f49f368d319309f41bb7828c151e17873012884a4a4f6241f0c8a555cac0e43
// jaunt:api=sha256:e3f04853e092ff31480725d9d75e53f7c9c5c6ea46f23347990fa791d42969cb
import type * as __JauntApi from "./index.api.js";
function __jaunt_impl_slugify(title: string): string {
  return title.match(/[A-Za-z0-9]+/g)?.map((part) => part.toLowerCase()).join("-") ?? "";
}

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
export const slugify: typeof __JauntApi.slugify = __jaunt_impl_slugify;
