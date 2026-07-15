// ⛓️ jaunt:generated — generated; do not edit.
// jaunt:state=built
// jaunt:module=ts:packages/app/src/slug/index
// jaunt:structural=sha256:a6dab9640a9f915a8389b155763e50b6504a003bbc1e5cb62a3d68f59ac1637b
// jaunt:prose=sha256:e9fa557be73e969f3cb26837fff759c7cccd3f8a5ca90099486dae5df986e4ee
// jaunt:api=sha256:02e22aaff07391d5f95d1ad63c0d30d065836e8a76bd03e6e47b704833b01993
import type * as __JauntApi from "./index.api.js";
import { normalizeSpacing } from "@jaunt-examples/core/normalize/index.js";

const __jaunt_impl_slugify = (value: string): string => {
  const normalized = normalizeSpacing(value);
  const lowercaseAscii = normalized.replace(/[A-Z]/g, (letter: string): string =>
    letter.toLowerCase(),
  );

  return lowercaseAscii.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
};

Object.defineProperty(__jaunt_impl_slugify, "name", { value: "slugify", configurable: true });
/**
 * Convert a title to a lowercase ASCII URL slug.
 *
 * First normalize spacing with `normalizeSpacing`. Lowercase ASCII letters,
 * replace each non-empty run of characters other than ASCII letters and digits
 * with one hyphen, and remove leading or trailing hyphens. Return an empty
 * string when no ASCII letters or digits remain.
 *
 * @example slugify("  Project\tReferences!  ") // => "project-references"
 */
export const slugify: typeof __JauntApi.slugify = __jaunt_impl_slugify;
