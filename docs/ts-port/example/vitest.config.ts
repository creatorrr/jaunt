import { readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const ROOT = fileURLToPath(new URL(".", import.meta.url));

/**
 * The `@jaunt/vite` plugin in miniature — the Vite-side twin of
 * src/jaunt/register.mjs, built on the same manifest. One core rule, thin
 * adapters per resolver: imports of a governed spec module are redirected to
 * its __generated__ sibling, except when the importer *is* that generated
 * module (it imports the raw spec file for handwritten context).
 */
function jauntRedirect() {
  const manifest = JSON.parse(
    readFileSync(path.join(ROOT, ".jaunt", "ts-manifest.json"), "utf8"),
  ) as { modules: Record<string, string> };
  const redirects = new Map(
    Object.entries(manifest.modules).map(([spec, generated]) => [
      path.resolve(ROOT, spec),
      path.resolve(ROOT, generated),
    ]),
  );
  return {
    name: "jaunt-redirect",
    enforce: "pre" as const,
    resolveId(source: string, importer: string | undefined) {
      if (importer === undefined || !source.startsWith(".")) return null;
      const importerPath = importer.split("?")[0];
      const target = path.resolve(path.dirname(importerPath), source);
      const generated = redirects.get(target);
      if (generated === undefined || importerPath === generated) return null;
      return generated;
    },
  };
}

export default defineConfig({
  plugins: [jauntRedirect()],
  test: {
    include: ["tests/**/*.test.ts"],
  },
});
