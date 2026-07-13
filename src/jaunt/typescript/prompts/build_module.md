Implement the TypeScript module described by the context files.

Read `_context/contract.json` first. It contains canonical contract IR, routes,
required reserved binding names, and dependency IDs. Read `_context/spec.ts` for the
authored TSDoc and declarations, `_context/api.ts` for the deterministic public type
surface, and any `_context/context.ts` or dependency API files for allowed imports.
When `_context/dependencies.json` exists, it is the allowlist of public Jaunt facades
the candidate may import; use each listed runtime `facadeSpecifier` and never its
private spec, API mirror, or generated implementation path.

Write the complete candidate to `{{target_path}}`. Define every binding listed in
`{{reserved_bindings}}` and no exports. Match the owning project's module convention:
`{{module_kind}}` with `{{module_resolution}}` resolution. Source import specifiers
must use the runtime form expected by that project, normally `.js` for NodeNext.

The model is not responsible for the public boundary. Jaunt appends typed exports
only after the candidate passes semantic conformance.
