import { expect, test } from "vitest";
import { merkleTypeGraph } from "../../src/analyzer/type_graph.js";

test("recursive type SCCs terminate and stale their consumers", () => {
  const first = merkleTypeGraph([
    { id: "A", payload: { value: 1 }, references: ["B"] },
    { id: "B", payload: { value: 2 }, references: ["A"] },
    { id: "C", payload: {}, references: ["A"] },
  ]);
  const second = merkleTypeGraph([
    { id: "A", payload: { value: 9 }, references: ["B"] },
    { id: "B", payload: { value: 2 }, references: ["A"] },
    { id: "C", payload: {}, references: ["A"] },
  ]);
  expect(first).toHaveLength(2);
  expect(first.map((component) => component.digest)).not.toEqual(
    second.map((component) => component.digest),
  );
});
