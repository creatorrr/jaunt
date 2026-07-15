// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:src/index
// jaunt:structural=sha256:d8a1f0a2cc5817e5a16eadb561f837307e5cea57af8a7027252d6aff59556c47
// jaunt:prose=sha256:8f49f368d319309f41bb7828c151e17873012884a4a4f6241f0c8a555cac0e43
// jaunt:api=sha256:e86e0a408e9f000d7677a2ca5e6398274c8ac27e8dc1cd89331e4dca4b872932
import type * as __JauntApi from "./index.api.js";
const __jaunt_impl_slugify = (title: string): string =>
  title
    .replace(/[^A-Za-z0-9]+/g, "-")
    .replace(/^-|-$/g, "")
    .toLowerCase();

Object.defineProperty(__jaunt_impl_slugify, "name", { value: "slugify", configurable: true });
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
