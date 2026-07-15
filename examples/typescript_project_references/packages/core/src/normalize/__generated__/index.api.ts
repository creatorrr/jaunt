// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:328aced8e2d1839fb54bbcc7542d6e9d9f4640de700f25ae4061ace829c31cb5
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:1ea00d36c67c8754c1e4d3a846685f60afad599466397e157228b389c5c835e7
/**
 * Normalize spacing in a title.
 *
 * Trim leading and trailing ASCII whitespace and replace each non-empty run of
 * ASCII whitespace characters with one literal space. Return an empty string
 * when the input is empty or contains only ASCII whitespace.
 *
 * @example normalizeSpacing("  Jaunt\tTS  ") // => "Jaunt TS"
 */
export declare function normalizeSpacing(value: string): string;
