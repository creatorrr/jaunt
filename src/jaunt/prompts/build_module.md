Output Python code only (no markdown, no fences).

Implement `{{generated_module}}` for specs from `{{spec_module}}`.

Required top-level names (must exist): {{expected_names}}

Specs:
{{specs_block}}

How to read the specs above:
- The function/class signature is the exact API you must implement (same name, parameters, type hints, return type).
- The docstring is your specification — implement the behavior, rules, edge cases, and error handling it describes.
- If a spec includes a `# Decorator prompt` section, treat it as additional user-provided instructions that supplement the docstring.
- If a spec shows a class decorated with `@magic` (whole-class mode), generate the COMPLETE class:
  - Implement every method whose body is a stub (only a docstring, `...`, `pass`, or `raise NotImplementedError`).
  - Keep every other method, class attribute, base class, and class decorator EXACTLY as shown (verbatim) — including any method marked `@jaunt.preserve`, which you must emit WITHOUT the `@jaunt.preserve` decorator.
  - You may add private helper methods and shared state as needed.
  - Retain the class docstring's content (you may append notes).
  - Honor the inheritance contract in "Base class contract" below: implement all inherited abstractmethods and make overrides consistent with their base signatures.
  - If the class body is only a docstring (docstring-only mode), design the full public API the docstring implies.
- If a spec shows a class with per-method `@magic` stubs, generate the entire class with those methods implemented (legacy per-method mode), preserving non-magic members and decorators.

Dependency APIs (callable signatures/docstrings):
{{deps_api_block}}

Decorator Dependency APIs (reference only):
{{decorator_apis_block}}

Base class contract (inherited/overridable methods and required abstractmethods):
{{base_contract_block}}

How to use dependencies:
- Each Dependency API entry key is like `<module>:<qualname>`. Import the name from `<module>`.
- Import spec-registry dependencies ONLY from the declared paths listed above — do not guess or fabricate module paths.
- The Python stdlib, and installed third-party distributions that the spec module itself imports or the owning package declares, are fair game: import them plainly from their real modules (no duck-typed stand-ins, no dynamic-import contortions).
- For anything else, inline the logic and mark it with JAUNT-NEEDS-DEP as instructed.
- Decorator Dependency APIs are extra typing/behavior context; do not import those keys directly.
- If a spec includes `effective_signature[...]`, treat that as the strongest signature guidance.
- If the contract implies behavior from a module NOT listed in Dependency APIs, do not invent an import. Inline the minimal logic and mark the site with a comment: `# JAUNT-NEEDS-DEP: <module>:<name> — <one-line reason>`.

Previously generated dependency modules (for reference only):
{{deps_generated_block}}

Handwritten source-module symbols already available for reuse:
{{module_contract_block}}

Reference-only blueprint of the source module shape:
{{blueprint_source_block}}

Attached test specs explicitly targeting this module:
{{attached_test_specs_block}}

Additional build instructions from the user/project:
{{build_instructions_block}}

Local package context:
{{package_context_block}}

Extra error context (fix these issues):
{{error_context_block}}

Rules:
- Do not generate tests.
- Do not edit user files; only output generated module source code.
- Reuse handwritten symbols from `{{spec_module}}` when they already exist there; do not redefine them.
- The generated module must define every spec symbol itself; never import a spec symbol back from `{{spec_module}}`. Call same-module sibling spec symbols by bare name — never via a module-level import of `{{spec_module}}` (it is mid-import at load time).
- Never wrap imports in try/except to provide fallbacks — import failures must raise, and there must never be a second, divergent implementation of a contract symbol.
- Treat the blueprint as reference-only structure guidance; do not copy handwritten symbols from it.
- Treat attached test specs as additional behavioral guidance, not as production code to inline.
- Treat additional build instructions as extra user intent layered on top of the spec docstrings.
- Use the package context to prefer nearby real modules and exports over guessed import paths.
- Include type annotations on all function signatures.
- Ensure every non-Optional return type has explicit return/raise on all code paths.
- If a spec uses `async def`, the generated implementation MUST also be `async def`. Use `await` for any async calls within.
