const DEFAULT_MESSAGE =
  "Jaunt TypeScript spec modules are static analysis inputs and must not be executed. " +
  "Run `jaunt build` and import the module's public facade instead.";

export interface MagicOptions {
  readonly deps?: readonly unknown[];
  readonly prompt?: string;
  readonly inferDeps?: boolean;
  readonly test?: boolean;
}

export interface TestSpecOptions {
  readonly targets: readonly unknown[];
  readonly prompt?: string;
}

export class JauntNotBuiltError extends Error {
  readonly code = "JAUNT_NOT_BUILT" as const;

  constructor(message = DEFAULT_MESSAGE) {
    super(message);
    this.name = "JauntNotBuiltError";
  }
}

function fail(): never {
  throw new JauntNotBuiltError();
}

/** Marks every eligible top-level declaration in this file as a Jaunt spec. */
export function magicModule(_options?: MagicOptions): void {
  fail();
}

/** Marks a governed implementation body. Jaunt spec modules are never executed. */
export function magic<T = never>(_options?: MagicOptions): T {
  return fail();
}

/** Marks authored test intent. Jaunt test-spec modules are never executed. */
export function testSpec(_options: TestSpecOptions): never {
  return fail();
}
