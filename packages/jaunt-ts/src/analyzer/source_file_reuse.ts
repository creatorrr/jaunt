import type ts from "@typescript/typescript6";

interface RedirectSourceFile extends ts.SourceFile {
  readonly redirectInfo?: {
    readonly unredirected: ts.SourceFile;
  };
}

/** Return the physical source a compiler host may safely provide to a new Program. */
export function reusableSourceFile(
  sourceFile: ts.SourceFile | undefined,
): ts.SourceFile | undefined {
  return (
    (sourceFile as RedirectSourceFile | undefined)?.redirectInfo
      ?.unredirected ?? sourceFile
  );
}
