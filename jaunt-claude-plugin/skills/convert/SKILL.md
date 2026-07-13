---
name: convert
disable-model-invocation: true
argument-hint: "[module-or-file]"
description: Use only when the user explicitly asks to convert handwritten Python or TypeScript into Jaunt specs. Characterizes behavior, distills Python docstrings or TypeScript TSDoc contracts, creates strict stubs, previews model work, builds, and reviews the first result.
---

# Convert handwritten Python or TypeScript to Jaunt

Conversion reaches a model call at the build step. Keep the safety net ahead of
that call.

1. Choose a suitable target: stable pure logic with meaningful tests is a good
   candidate; import-time side effects and thin I/O orchestration are not.
2. Add characterization tests for current behavior before changing the module:
   pytest for Python or Vitest against the public facade for TypeScript. They
   must pass unchanged before and after conversion.
3. Resolve the nearest workspace with `resolve-workspace.sh`. A Jaunt 1.6.2+
   root config may cover several packages through literal or globbed roots.
   For old nested configs, preview `jaunt migrate --merge-projects`.
4. Distill a self-contained contract. Pin errors, ordering, mutation, state
   timing, and boundaries that callers rely on.
5. For Python, add `jaunt.magic_module(__name__)` or per-symbol
   `@jaunt.magic`, then replace bodies with strict stubs. For TypeScript, move
   the contract into a private `*.jaunt.ts[x]`, call `jaunt.magicModule()`,
   replace governed bodies with `jaunt.magic()`, keep consumers on the public
   facade, and run model-free `sync --language ts` first.
6. Use `resolve-workspace.sh --run "$PWD"` for all Jaunt commands. It selects
   a compatible installed Jaunt, a uv project environment, or `uvx jaunt` for a
   JavaScript-only project. Run `specs --json` and review every
   `newly_governed` symbol, then run `status --json --progress none`. New
   implementations and TypeScript `unbuilt` entries are model work. Describe
   the likely calls without inventing a price.
7. Follow `$jaunt:build`. Surface advisories, report actual cost, run check and
   the unchanged tests, then perform the first-build review.
8. Commit the spec and its complete artifact set together: generated
   implementation and `.pyi` for Python; implementation, API mirror, sidecar,
   facade, and generated batteries for TypeScript.

Never patch `__generated__/**`, provenance-headed stubs, API mirrors, sidecars,
or generated batteries.
