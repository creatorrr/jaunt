// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:351061225b17a1886e8d1384f7f31ddff45b8cc0feaa2a26b1af415783f6bb0a
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:96d14a1b5021e381fd501aa658467f6fe43af438b04e29004d3fde17f66805d0
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
