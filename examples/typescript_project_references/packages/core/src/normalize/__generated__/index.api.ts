// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:819f62c175205856ab24edb1bb950ebeb9f8d2aeb1aadeb1f7eba25dbc25e627
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:a52511dad24073a9fc2e95259368ead606971bd859f5af8ecb222f6ec1d1fd46
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
