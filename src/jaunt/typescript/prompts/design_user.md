Design the declaration marked by the source range in `_context/design.json`.

Use the surrounding type declarations and imports in `_context/spec.ts`. Return only
the replacement TSDoc and declaration text, optionally preceded by the associated
type-only imports that declaration needs. Jaunt will build a unified diff, confine it
to the marked range, remove `@jauntDesign` after acceptance, and validate the result
without executing the module.
