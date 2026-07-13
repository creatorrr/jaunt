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

export declare class JauntNotBuiltError extends Error {
  readonly name: "JauntNotBuiltError";
  readonly code: "JAUNT_NOT_BUILT";
  constructor(message?: string);
}

/** Marks every eligible top-level declaration in this file as a Jaunt spec. */
export declare function magicModule(options?: MagicOptions): void;

/** Marks a governed implementation body. Jaunt spec modules are never executed. */
export declare function magic<T = never>(options?: MagicOptions): T;

/** Marks authored test intent. Jaunt test-spec modules are never executed. */
export declare function testSpec(options: TestSpecOptions): never;
