Output Python code only (no markdown, no fences).

You are generating the pytest test module `{{generated_module}}` from test specs in `{{spec_module}}`.

The generated module MUST define these top-level pytest test functions (do not import them): {{expected_names}}

Specs:
{{specs_block}}

Dependency APIs (callable signatures/docstrings):
{{deps_api_block}}

Previously generated dependency modules (reference only):
{{deps_generated_block}}

Extra error context (fix these issues):
{{error_context_block}}

Rules:
- Generate tests only (no production implementation).
- Do not import from `{{generated_module}}` (that would be a circular import).
- Do not edit user files; only output test module source code.
