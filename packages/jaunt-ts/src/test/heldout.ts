/**
 * Defense-in-depth guard for the held-out test boundary.
 *
 * The reporter constructs a small allowlisted DTO. This guard independently
 * remembers detail that was present on raw errors/output and verifies that none
 * of it survived into the DTO returned to Python.
 */

export class HeldOutLeakError extends Error {
  constructor() {
    super("Protected test output failed the held-out leak assertion");
    this.name = "HeldOutLeakError";
  }
}

function collectStrings(
  value: unknown,
  output: Set<string>,
  seen: Set<object>,
): void {
  if (typeof value === "string") {
    if (value.length > 0) output.add(value);
    const trimmed = value.trim();
    if (trimmed.length > 0) output.add(trimmed);
    return;
  }
  if (value === null || value === undefined || typeof value !== "object")
    return;
  if (seen.has(value)) return;
  seen.add(value);

  if (value instanceof Error) {
    collectStrings(value.message, output, seen);
    collectStrings(value.stack, output, seen);
    collectStrings(value.cause, output, seen);
    if (value instanceof AggregateError) {
      collectStrings(value.errors, output, seen);
    }
  }

  if (Array.isArray(value)) {
    for (const item of value) collectStrings(item, output, seen);
    return;
  }
  for (const item of Object.values(value as Record<string, unknown>)) {
    collectStrings(item, output, seen);
  }
}

function stringsIn(value: unknown): Set<string> {
  const output = new Set<string>();
  collectStrings(value, output, new Set<object>());
  return output;
}

export class HeldOutLeakGuard {
  readonly #sensitive = new Set<string>();
  readonly #allowed = new Set<string>();

  observe(value: unknown): void {
    for (const item of stringsIn(value)) this.#sensitive.add(item);
  }

  allow(value: unknown): void {
    for (const item of stringsIn(value)) this.#allowed.add(item);
  }

  assertSafe(value: unknown): void {
    const rendered = stringsIn(value);
    for (const secret of this.#sensitive) {
      if (secret.length < 4 || this.#allowed.has(secret)) continue;
      if ([...rendered].some((item) => item.includes(secret))) {
        throw new HeldOutLeakError();
      }
    }
  }
}
