/**
 * `jaunt/register` — the Node resolution adapter (preview).
 *
 * Redirects imports of governed spec modules to their __generated__
 * siblings, which is how the port replaces Python jaunt's live module
 * rebinding (ESM namespaces are sealed, so substitution moves from
 * attribute-rebinding time to resolve time).
 *
 * Importer-aware exception: a spec's own generated module imports the *raw*
 * spec file (for handwritten context), so the redirect skips exactly that
 * importer — the TS analog of Python's `__jaunt_original_stubs__` snapshot.
 *
 * Usage: node --import ./src/jaunt/register.mjs src/app.ts
 * Requires node >= 22.15 (module.registerHooks — synchronous, same-thread).
 */
import { existsSync, readFileSync } from "node:fs";
import { registerHooks } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

function findManifest(startDir) {
  let dir = startDir;
  for (;;) {
    const candidate = join(dir, ".jaunt", "ts-manifest.json");
    if (existsSync(candidate)) return candidate;
    const parent = dirname(dir);
    if (parent === dir) return null;
    dir = parent;
  }
}

const redirects = new Map();
const manifestPath = findManifest(process.cwd());
if (manifestPath !== null) {
  const projectRoot = dirname(dirname(manifestPath));
  const manifest = JSON.parse(readFileSync(manifestPath, "utf8"));
  for (const [spec, generated] of Object.entries(manifest.modules ?? {})) {
    redirects.set(resolve(projectRoot, spec), resolve(projectRoot, generated));
  }
}

registerHooks({
  resolve(specifier, context, nextResolve) {
    const resolved = nextResolve(specifier, context);
    if (!resolved.url?.startsWith("file:")) return resolved;
    const generated = redirects.get(fileURLToPath(resolved.url));
    if (generated === undefined) return resolved;
    const importer = context.parentURL?.startsWith("file:")
      ? fileURLToPath(context.parentURL)
      : "";
    if (importer === generated) return resolved; // generated → raw spec
    return { ...resolved, url: pathToFileURL(generated).href };
  },
});
