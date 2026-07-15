// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:1d9d84aed098a9759fed9c85b9a68c044029e7e047e33e9f765333d3e6c4cf66
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:a9f5fd967293c6097dee2884d28ff2cd1541c5f4a0e2b453abcedfca6e254bb8
import type * as __JauntApi from "./index.api.js";
function __jaunt_impl_normalizeSpacing(value: string): string {
  return value.replace(/[\t\n\v\f\r ]+/g, " ").replace(/^ | $/g, "");
}

Object.defineProperty(__jaunt_impl_normalizeSpacing, "name", { value: "normalizeSpacing", configurable: true });
/**
 * Normalize spacing in a title.
 *
 * Trim leading and trailing ASCII whitespace and replace each non-empty run of
 * ASCII whitespace characters with one literal space. Return an empty string
 * when the input is empty or contains only ASCII whitespace.
 *
 * @example normalizeSpacing("  Jaunt\tTS  ") // => "Jaunt TS"
 */
export const normalizeSpacing: typeof __JauntApi.normalizeSpacing = __jaunt_impl_normalizeSpacing;
