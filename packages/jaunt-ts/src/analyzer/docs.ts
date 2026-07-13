import type ts from "@typescript/typescript6";

export interface ParsedDocs {
  readonly text: string;
  readonly tags: Readonly<Record<string, readonly string[]>>;
}

export function cleanTSDoc(raw: string): string {
  const withoutDelimiters = raw
    .replace(/^\s*\/\*\*?/, "")
    .replace(/\*\/\s*$/, "");
  const lines = withoutDelimiters
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*\* ?/, ""));
  while (lines[0]?.trim() === "") lines.shift();
  while (lines.at(-1)?.trim() === "") lines.pop();
  const nonEmpty = lines.filter((line) => line.trim() !== "");
  const indent =
    nonEmpty.length === 0
      ? 0
      : Math.min(...nonEmpty.map((line) => /^\s*/.exec(line)?.[0].length ?? 0));
  return lines
    .map((line) => line.slice(indent).trimEnd())
    .join("\n")
    .trim();
}

export function docsForNode(
  compiler: typeof import("@typescript/typescript6"),
  sourceFile: ts.SourceFile,
  node: ts.Node,
): ParsedDocs {
  const comments = compiler.getJSDocCommentsAndTags(node);
  const blocks = comments
    .filter(compiler.isJSDoc)
    .map((item) => cleanTSDoc(item.getText(sourceFile)));
  const tags = new Map<string, string[]>();
  for (const tag of compiler.getJSDocTags(node)) {
    const name = tag.tagName.text;
    const comment =
      typeof tag.comment === "string"
        ? tag.comment
        : (tag.comment?.map((part) => part.text).join("") ?? "");
    const values = tags.get(name) ?? [];
    values.push(comment.trim());
    tags.set(name, values);
  }
  return {
    text: blocks.join("\n\n"),
    tags: Object.fromEntries(
      [...tags.entries()].sort(([left], [right]) => left.localeCompare(right)),
    ),
  };
}
