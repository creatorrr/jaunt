// ⛓️ jaunt:api-mirror — generated; do not edit.
// jaunt:module=ts:packages/core/src/normalize/index
// jaunt:structural=sha256:c8e068a9a510223d9ccbf630be83649f7e335d44b7d90583ebccd4461501f5f8
// jaunt:prose=sha256:fe36c3763b8865671942e43121b369178cedb891ef3436c7263ba68da757b456
// jaunt:api=sha256:e0a660b90f61ef74fc8b3a2e94b8908815cf45c05b737c763524f04d50b75e0b
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
