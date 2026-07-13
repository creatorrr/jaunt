Write the requested Vitest battery to `{{target_path}}`.

Read `_context/contract.json`, `_context/spec.ts`, and `_context/api.ts`. Import the
module under test only from `{{facade_specifier}}`. If `_context/fixtures.ts` exists,
import its extended `test` value; otherwise import `test` from `vitest`.

The requested tier is `{{tier}}`. Use only that tier in this file. The file must be
self-contained, compile under the configured test project, and terminate under the
configured timeout.
