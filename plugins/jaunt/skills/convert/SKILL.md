---
name: convert
description: Use only when the user explicitly asks to convert handwritten Python into Jaunt specs. Characterizes current behavior, distills contracts, converts stubs, previews model work, builds, and reviews the first result.
---

# Convert handwritten Python to Jaunt

Conversion reaches a model call at the build step. Keep the safety net ahead of
that call.

1. Choose a suitable target: stable pure logic with meaningful tests is a good
   candidate; import-time side effects and thin I/O orchestration are not.
2. Add characterization tests for current behavior before changing the module.
   They must pass unchanged before and after conversion.
3. Locate this installed `SKILL.md`, walk up two directories to the plugin
   root, and run `scripts/resolve-workspace.sh` by absolute path. A Jaunt 1.6.2+
   root config may cover several packages through literal or globbed roots. For
   old nested configs, preview `jaunt migrate --merge-projects`.
4. Distill a self-contained contract. Pin errors, ordering, mutation, state
   timing, and boundaries that callers rely on.
5. Add `jaunt.magic_module(__name__)` or a per-symbol `@jaunt.magic`, then
   replace implementation bodies with valid strict stubs. Review
   `uv run jaunt specs --json` and every `newly_governed` symbol.
6. Run `uv run jaunt status --json --progress none`. New implementations are
   structural model work. Describe the likely calls without inventing a price.
7. Follow `$jaunt:build`. Surface advisories, report actual cost, run check and
   the unchanged tests, then perform the first-build review.
8. Commit the spec, generated implementation, and generated `.pyi` together.

Never patch `__generated__/**` or provenance-headed stubs.
