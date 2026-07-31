import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/**
 * Normalize spacing in a title.
 *
 * Trim leading and trailing ASCII whitespace and replace each non-empty run of
 * ASCII whitespace characters with one literal space. Return an empty string
 * when the input is empty or contains only ASCII whitespace.
 *
 * @example normalizeSpacing("  Jaunt\tTS  ") // => "Jaunt TS"
 */
export function normalizeSpacing(value: string): string {
  return jaunt.magic();
}
