import { describe, expect, test } from "vitest";
import { cleanTSDoc } from "../../src/analyzer/docs.js";

describe("TSDoc cleaning", () => {
  test("normalizes CRLF, stars, indentation, and edges", () => {
    expect(cleanTSDoc("/**\r\n * Hello.\r\n *\r\n *   Detail.\r\n */")).toBe(
      "Hello.\n\n  Detail.",
    );
  });

  test("preserves Unicode", () => {
    expect(cleanTSDoc("/** Café 🚀 */")).toBe("Café 🚀");
  });
});
