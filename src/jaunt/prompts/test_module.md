Output Python code only (no markdown, no fences).

You are generating the pytest test module `{{generated_module}}` from test specs in `{{spec_module}}`.

The generated module MUST define these top-level pytest test functions (do not import them): {{expected_names}}

Specs:
{{specs_block}}

How to read the test specs above:
- The docstring describes the test scenario — what to set up, what to call, and what to assert.
- If a spec includes a `# Decorator prompt` section, treat it as additional user-provided instructions for the test.
- The function signature (parameters, type hints) indicates whether the test needs fixtures.

Dependency APIs (callable signatures/docstrings):
{{deps_api_block}}

Previously generated dependency modules (reference only):
{{deps_generated_block}}

Extra error context (fix these issues):
{{error_context_block}}

Test quality:
- Cover the happy path (normal usage) and edge cases (boundary values, error conditions, empty inputs).
- Write specific assertions that check concrete values — avoid bare `assert result`.
- Use `pytest.raises` for expected exceptions.

Rules:
- Generate tests only (no production implementation).
- Do not import from `{{generated_module}}` (that would be a circular import).
- Do not edit user files; only output test module source code.
- Do not guess or search for application modules like `app`, `main`, `token`, etc.
- Import the production APIs under test from the modules listed in Dependency APIs above.
  - Each Dependency API entry key is like `<module>:<qualname>`; import from `<module>`.
- Do not import production APIs from the test spec module (`{{spec_module}}`); it contains only `@jaunt.test` stubs.
{{async_test_info}}
