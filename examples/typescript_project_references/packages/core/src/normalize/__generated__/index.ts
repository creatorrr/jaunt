// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:2e97c5687938af9f6e2975de77e3a2ba6ad92f2fe21a0719674811c7f4280ddf
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:562bfc6134ba8f47ad7d1e478990cb73d1e76750e0441b4d28b4ae70c9ce0c85
import type * as __JauntApi from "./index.api.js";
const ASCII_WHITESPACE_RUN = /[\t-\r ]+/g;
const LEADING_OR_TRAILING_ASCII_WHITESPACE = /^[\t-\r ]+|[\t-\r ]+$/g;

const __jaunt_impl_normalizeSpacing = (value: string): string =>
  value
    .replace(LEADING_OR_TRAILING_ASCII_WHITESPACE, "")
    .replace(ASCII_WHITESPACE_RUN, " ");

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
