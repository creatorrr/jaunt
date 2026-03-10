Output Python code only (no markdown, no fences).

Implement `{{generated_module}}` for specs from `{{spec_module}}`.

Required top-level names (must exist): {{expected_names}}

Specs:
{{specs_block}}

How to read the specs above:
- The function/class signature is the exact API you must implement (same name, parameters, type hints, return type).
- The docstring is your specification — implement the behavior, rules, edge cases, and error handling it describes.
- If a spec includes a `# Decorator prompt` section, treat it as additional user-provided instructions that supplement the docstring.
- If a spec shows a class with method stubs (methods decorated with `@magic`), generate the entire class with those methods implemented. Preserve non-magic methods, class-level attributes, `@classmethod`, `@staticmethod`, and `@abstractmethod` decorators as shown in the spec.

Dependency APIs (callable signatures/docstrings):
{{deps_api_block}}

Decorator Dependency APIs (reference only):
{{decorator_apis_block}}

How to use dependencies:
- Each Dependency API entry key is like `<module>:<qualname>`. Import the name from `<module>`.
- Only import dependencies listed above — do not guess or fabricate module paths.
- Decorator Dependency APIs are extra typing/behavior context; do not import those keys directly.
- If a spec includes `effective_signature[...]`, treat that as the strongest signature guidance.

Previously generated dependency modules (for reference only):
{{deps_generated_block}}

Handwritten source-module symbols already available for reuse:
{{module_contract_block}}

Reference-only blueprint of the source module shape:
{{blueprint_source_block}}

Attached test specs explicitly targeting this module:
{{attached_test_specs_block}}

Local package context:
{{package_context_block}}

Extra error context (fix these issues):
{{error_context_block}}

Rules:
- Do not generate tests.
- Do not edit user files; only output generated module source code.
- Reuse handwritten symbols from `{{spec_module}}` when they already exist there; do not redefine them.
- Treat the blueprint as reference-only structure guidance; do not copy handwritten symbols from it.
- Treat attached test specs as additional behavioral guidance, not as production code to inline.
- Use the package context to prefer nearby real modules and exports over guessed import paths.
- Include type annotations on all function signatures.
- Ensure every non-Optional return type has explicit return/raise on all code paths.
- If a spec uses `async def`, the generated implementation MUST also be `async def`. Use `await` for any async calls within.
