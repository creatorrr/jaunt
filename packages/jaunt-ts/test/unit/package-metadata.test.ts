import { readFileSync } from "node:fs";
import { expect, test } from "vitest";

test("the published worker has a bounded Node host range", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ) as { engines?: { node?: unknown } };

  expect(manifest.engines?.node).toBe(">=20.0.0 <25.0.0");
});

test("the stable package publishes through npm latest", () => {
  const manifest = JSON.parse(
    readFileSync(new URL("../../package.json", import.meta.url), "utf8"),
  ) as {
    version?: unknown;
    homepage?: unknown;
    publishConfig?: { access?: unknown; tag?: unknown };
  };

  expect(manifest.version).toBe("0.1.2");
  expect(manifest.homepage).toBe("https://jaunt.ing/docs/guides/typescript");
  expect(manifest.publishConfig).toEqual({ access: "public", tag: "latest" });
});
