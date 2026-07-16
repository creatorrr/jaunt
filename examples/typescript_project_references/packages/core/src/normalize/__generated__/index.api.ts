// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:c4f716f19aaf96bd3672aae056580676cd62fc1fc3db2abca7ad12ac8fca6276
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:d7f87ef01e4b85c13d497d3acddc171157dde44bbc4ac12f20faefd99e926639
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
