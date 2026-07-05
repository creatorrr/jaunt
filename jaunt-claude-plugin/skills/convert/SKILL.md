---
name: convert
disable-model-invocation: true
argument-hint: "[module-or-file]"
description: Convert an existing handwritten Python module into a jaunt spec (docstring contract + generated body). Walks churn triage, characterization tests first, contract distillation, stub conversion, build, gate, and first-build line review. Costs real money at the build step.
---

# /jaunt:convert — module → spec conversion

Converting bills real money at the build step. Make the safety net come first
so the conversion is reversible and reviewable.

## 1. Triage — is this a good target?

Good: pure logic, self-contained, real churn, existing tests.
Bad: heavy I/O orchestration, import-time side effects, module-level consumers
of would-be specs, <50 LOC trivia. If bad, stop and say why. Do not convert.

## 2. Safety net first

Existing tests are the gate. If coverage is thin, write characterization tests
against CURRENT behavior before touching the module. They must pass unchanged
pre- and post-conversion.

## 3. Resolve the project

```bash
bash "${CLAUDE_PLUGIN_ROOT}/scripts/resolve-project.sh" <file>
```

No `jaunt.toml`: create one project per package, with `source_roots = ["."]`
at the package's import root. Specs spanning multiple source roots are a hard
exit-2 error since 1.5.1. Keep `[codex]` and `[build].instructions`
byte-identical with sibling projects: drift restales every module
(fingerprint, free re-stamp).

## 4. Distill the contract

The docstring IS the full behavioral contract and must be self-contained.
Generation cannot see sibling docstrings, so inline cross-module invariants or
put them in `magic_module(prompt=...)`. Pin every behavior the tests assert.
State mutable-state timing: read at call time vs import time.

## 5. Convert to stub

Add `jaunt.magic_module(__name__)` at the top. Each converted symbol's body
becomes `raise NotImplementedError`, not `...`; ty rejects empty bodies under
concrete return annotations, and the forms are digest-equal. Handwritten
symbols keep real bodies untouched and coexist. Specs consumed at import time
get a per-symbol `@jaunt.magic`.

```bash
uv run jaunt specs
```

Review `newly_governed`; every entry must be intentional.

## 6. Preview the spend

```bash
uv run jaunt status --json
```

New modules classify structural (paid). Use the taxonomy exactly: none, prose
(~$0 refreeze), structural (paid), fingerprint (free re-stamp). State the
expected bill before building.

## 7. Build + gate

Use the `/jaunt:build` protocol. Surface advisories verbatim. `jaunt check`
exits 0. Pre-existing tests pass unchanged. Ruff and ty are clean.

## 8. First-build line review

Dispatch the `jaunt` plugin's `first-build-reviewer` agent on the generated
diff vs the contract. The failure class no gate catches is behavior the spec
does not pin: contract-silence divergence.

## 9. Commit

Commit the spec, `__generated__/`, and `.pyi` together. The message names the
module converted and the bill.

## When it goes wrong

| Symptom | Likely cause | Fix |
|---|---|---|
| Generated import rejected as undeclared | Dep missing from the owning package's pyproject, or hidden in handwritten context | Declare it in the owning package or import it in the spec module; rebuild through the spec, never patch the generated body |
| Exit 2, multi-root error | Governed specs span source roots | Split to one jaunt project per package with `source_roots = ["."]`; rebuild through the spec, never patch the generated body |
| ty errors in generated code | Contract is not self-contained: missing types, invariants, or mutable-state timing | Tighten the docstring or `magic_module(prompt=...)`; rebuild through the spec, never patch the generated body |
| Existing tests fail | Docstring did not pin current behavior the tests expect | Add the missing rule or example to the docstring; rebuild through the spec, never patch the generated body |
