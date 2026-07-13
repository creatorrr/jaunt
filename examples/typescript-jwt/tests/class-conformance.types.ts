/**
 * Negative compile sentinel for the synthetic class-adapter strategy.
 *
 * Whole-class assignment accepts narrowed TypeScript methods because methods
 * are bivariant. Reconstructing each member as a function type must reject it.
 */
import type { TokenStore as TokenStoreContract } from "../src/tokens/index.js";

type JauntFunction = (...args: never[]) => unknown;
type MethodAdapter<T, K extends keyof T> = T[K] extends JauntFunction
  ? (...args: Parameters<T[K]>) => ReturnType<T[K]>
  : never;
type AssertAssignable<Expected, Actual extends Expected> = Actual;

class NarrowTokenStore {
  put(subject: "admin", _token: string, _exp: number): void {
    void subject;
  }

  get(_subject: string): string | null {
    return null;
  }

  sweep(): number {
    return 0;
  }

  get size(): number {
    return 0;
  }
}

type ContractPutAdapter = MethodAdapter<TokenStoreContract, "put">;
type NarrowPutAdapter = MethodAdapter<NarrowTokenStore, "put">;

// This directive becomes an error if the adapter ever regresses to bivariant
// whole-class comparison and starts accepting the narrowed `subject` input.
// @ts-expect-error narrowed implementation method must be rejected
type _NarrowPutMustFail = AssertAssignable<ContractPutAdapter, NarrowPutAdapter>;
