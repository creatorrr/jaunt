import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

test("the published worker has a bounded Node host range", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ) as { engines?: { node?: unknown } };

  expect(manifest.engines?.node).toBe(">=20.0.0 <25.0.0");
});
