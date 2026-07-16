// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:b2e1c32db9c05c81fa1fc19bf958f70e88420158b4245509b0d856463abe167e
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:3a61ccfd55851b5f8792223a127f903093cfb7ff0caba25bb2a90ec485e83081
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
