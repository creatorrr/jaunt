import { relative, sep } from "node:path";
import type ts from "@typescript/typescript6";
import type { DiagnosticRecord, Severity } from "./types.js";

export function posixRelative(root: string, path: string): string {
  return relative(root, path).split(sep).join("/") || ".";
}

function severity(category: ts.DiagnosticCategory): Severity {
  if (category === 1) return "error";
  if (category === 0) return "warning";
  return "info";
}

export function fromTypeScriptDiagnostic(
  compiler: typeof import("@typescript/typescript6"),
  root: string,
  diagnostic: ts.Diagnostic,
): DiagnosticRecord {
  const message = compiler.flattenDiagnosticMessageText(
    diagnostic.messageText,
    "\n",
  );
  if (!diagnostic.file || diagnostic.start === undefined) {
    return {
      code: `TS${diagnostic.code}`,
      severity: severity(diagnostic.category),
      message,
    };
  }
  const start = diagnostic.start;
  const length = diagnostic.length ?? 0;
  const location = diagnostic.file.getLineAndCharacterOfPosition(start);
  return {
    code: `TS${diagnostic.code}`,
    severity: severity(diagnostic.category),
    message,
    path: posixRelative(root, diagnostic.file.fileName),
    start,
    end: start + length,
    line: location.line + 1,
    column: location.character + 1,
  };
}

export function diagnosticAt(
  root: string,
  sourceFile: ts.SourceFile,
  node: ts.Node,
  code: string,
  message: string,
): DiagnosticRecord {
  const start = node.getStart(sourceFile);
  const end = node.getEnd();
  const location = sourceFile.getLineAndCharacterOfPosition(start);
  return {
    code,
    severity: "error",
    message,
    path: posixRelative(root, sourceFile.fileName),
    start,
    end,
    line: location.line + 1,
    column: location.character + 1,
  };
}

export function sortDiagnostics(
  records: readonly DiagnosticRecord[],
): DiagnosticRecord[] {
  return [...records].sort((left, right) =>
    [left.path ?? "", left.start ?? -1, left.code, left.message]
      .join("\0")
      .localeCompare(
        [right.path ?? "", right.start ?? -1, right.code, right.message].join(
          "\0",
        ),
      ),
  );
}
