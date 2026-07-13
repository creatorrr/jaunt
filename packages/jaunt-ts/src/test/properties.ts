import { createHash } from "node:crypto";

export function propertySeed(caseDigest: string): number {
  const bytes = createHash("sha256").update(caseDigest).digest();
  return bytes.readUInt32BE(0) & 0x7fff_ffff;
}

export function renderTypedProperty(options: {
  readonly name: string;
  readonly expectedType: string;
  readonly arbitrary: string;
  readonly predicate: string;
  readonly caseDigest: string;
  readonly numRuns: number;
  readonly async?: boolean;
}): string {
  const property = options.async ? "asyncProperty" : "property";
  return [
    `const ${options.name}Arbitrary: fc.Arbitrary<${options.expectedType}> = ${options.arbitrary};`,
    `fc.assert(fc.${property}(${options.name}Arbitrary, ${options.predicate}), {`,
    `  seed: ${propertySeed(options.caseDigest)},`,
    `  numRuns: ${options.numRuns},`,
    "});",
  ].join("\n");
}
