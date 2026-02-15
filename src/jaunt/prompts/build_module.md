Output Python code only (no markdown, no fences).

Implement `{{generated_module}}` for specs from `{{spec_module}}`.

Required top-level names (must exist): {{expected_names}}

Specs:
{{specs_block}}

How to read the specs above:
- The function/class signature is the exact API you must implement (same name, parameters, type hints, return type).
- The docstring is your specification — implement the behavior, rules, edge cases, and error handling it describes.
- If a spec includes a `# Decorator prompt` section, treat it as additional user-provided instructions that supplement the docstring.

Dependency APIs (callable signatures/docstrings):
{{deps_api_block}}

How to use dependencies:
- Each Dependency API entry key is like `<module>:<qualname>`. Import the name from `<module>`.
- Only import dependencies listed above — do not guess or fabricate module paths.

Previously generated dependency modules (for reference only):
{{deps_generated_block}}

Extra error context (fix these issues):
{{error_context_block}}

Rules:
- Do not generate tests.
- Do not edit user files; only output generated module source code.
- Include type annotations on all function signatures.
- Ensure every non-Optional return type has explicit return/raise on all code paths.
