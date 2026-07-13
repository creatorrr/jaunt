/**
 * OPTIONAL dev convenience — NOT part of the correctness path.
 *
 * The facade architecture needs no runtime hooks: consumers import the
 * ordinary public module (src/tokens/index.ts). This hook exists only for
 * scratch scripts that import a `*.jaunt.ts` spec path directly — it
 * resolves such imports to the sibling public facade (`./index.ts`) by
 * filename convention, so a quick `node --import ./src/jaunt/register.mjs
 * scratch.ts` behaves as if you had imported the facade.
 *
 * Requires node >= 22.15 (module.registerHooks). If you never import spec
 * paths directly, you never need this file.
 */
import { existsSync } from "node:fs";
import { registerHooks } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

registerHooks({
  resolve(specifier, context, nextResolve) {
    const resolved = nextResolve(specifier, context);
    if (!resolved.url?.startsWith("file:")) return resolved;
    const target = fileURLToPath(resolved.url);
    if (!target.endsWith(".jaunt.ts")) return resolved;
    const facade = join(dirname(target), "index.ts");
    if (!existsSync(facade)) return resolved;
    return { ...resolved, url: pathToFileURL(facade).href };
  },
});
