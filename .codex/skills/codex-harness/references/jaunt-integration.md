# Embedding Codex as Jaunt's `GeneratorBackend`

Jaunt's shipped Codex engine is `src/jaunt/generate/codex_backend.py`. It drives
`codex exec` as a per-call subprocess, not `codex mcp-server`: each generation
gets a throwaway temp workspace, a seeded target file, read-only context files,
and one `codex exec --json -` run. The backend then reads the target file back
and returns its source to Jaunt.

The grounded facts below are read from:

- `src/jaunt/generate/base.py`
- `src/jaunt/generate/codex_backend.py`
- `src/jaunt/config.py`

## The interface it satisfies

A backend subclasses `GeneratorBackend` and implements **one async method**:

```python
async def generate_module(
    self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
) -> tuple[str, TokenUsage | None]:
    """Return (generated_module_source, optional_token_usage)."""
```

Everything else is provided by the base class:

- `generate_with_retry(ctx, max_attempts=...)` calls `generate_module`, runs
  `validate_generated_source(source, ctx.expected_names)`, runs any extra
  validator, and retries with appended error context. The backend produces a
  source string; Jaunt owns deterministic validation and retry.
- Override properties: `provider_name` is `"codex"`, `model_name` reads
  `self._model`, and `supports_structured_output` remains `False`.
- `complete_text(system, user)` is implemented by the Codex backend for
  `jaunt reconcile` contract derivation. It uses a read-only sandbox and
  returns Codex's final message.

`ModuleSpecContext` inputs the backend turns into files and prompt text:

| Field | What it gives you |
|-------|-------------------|
| `kind` | `"build"` or `"test"`. |
| `spec_module` | Source spec module being generated from. |
| `generated_module` | Dotted module path Codex must produce, e.g. `myapp.__generated__.foo`. |
| `expected_names` | Names the module must export; Jaunt validates these. |
| `spec_sources` | `{SpecRef: source}` decorated stubs + docstrings, written as `_context/spec_*.py`. |
| `decorator_prompts`, `decorator_apis` | Extra per-spec instructions/API summaries from decorators. |
| `dependency_apis` | `{SpecRef: api_summary}` written as `_context/dep_*.pyi`. |
| `dependency_generated_modules` | Already-generated dependency modules. |
| `skills_block`, `module_contract_block`, `base_contract_block`, `blueprint_source`, `build_instructions_block`, `attached_test_specs_block`, `package_context_block` | Pre-rendered prompt/context blocks Jaunt assembles. |
| `module_context_digest` | Digest for dependency-aware freshness. |
| `async_runner` | Configured async test runner. |
| `seed_target_content` | Initial target-file content written before Codex runs. |
| `whole_class_contract_block` | Whole-class contract written to `_context/whole_class_contract.md` when present. |
| `whole_class` | Whether this context is for whole-class generation. |

`GenerationResult(attempts, source, errors, usage)` and
`TokenUsage(prompt_tokens, completion_tokens, model, provider, cached_prompt_tokens=0)`
come from `base.py`.

## The shipped implementation

Codex is an **agent that edits files on disk**; Jaunt's backend must **return
source text**. The implementation bridges that mismatch as follows:

1. Create a clean temp workspace.
2. Create the target file at `ctx.generated_module.replace(".", "/") + ".py"`.
3. Seed the target file from `ctx.seed_target_content`.
4. Write read-only context files under `_context/`:
   `spec_*.py` from `ctx.spec_sources`, `dep_*.pyi` from
   `ctx.dependency_apis`, and `whole_class_contract.md` when
   `ctx.whole_class_contract_block` is set.
5. Build a prompt that tells Codex to edit only the target file and output a
   complete module.
6. Run `codex exec` with the prompt on stdin.
7. Parse the JSONL stream for failure state, final message, and token usage.
8. Read the target file back and return `(source, usage)`.

The subprocess shape is:

```bash
codex exec --skip-git-repo-check \
  -C "$tmp_workspace" \
  --sandbox workspace-write \
  -c 'approval_policy="never"' \
  -m gpt-5.5 \
  -c 'model_reasoning_effort="high"' \
  --json -
```

`run_codex_exec` builds that argv directly, then appends any `[codex.config]`
entries as extra repeatable `-c key=value` overrides before `--json -`. The
prompt is passed on stdin.

Important sandbox detail: Jaunt does **not** use
`--dangerously-bypass-approvals-and-sandbox`. Codex is confined by `--sandbox`
(default `workspace-write`) inside the throwaway non-git temp workspace, and
`--skip-git-repo-check` is needed so Codex will operate there.

## Failure and usage parsing

The authoritative Jaunt rule is:

`run_codex_exec` raises `JauntGenerationError` when **any** of:

- the JSONL stream contains a `turn.failed` event;
- the JSONL stream contains a top-level `error` event;
- subprocess return code is non-zero;
- the stream never emitted `turn.completed` (protocol failure).

The error includes Codex stderr, truncated, so the builder reports the real
failure. A target file that is unchanged from the seed is **not** an exec
failure: a completed turn may legitimately write identical or low-quality
content. That remains a validation concern handled by `generate_with_retry` and
`validate_generated_source`.

Token usage is parsed from the `turn.completed` event's `usage` object:

| Codex usage field | Jaunt field |
|-------------------|-------------|
| `input_tokens` | `TokenUsage.prompt_tokens` |
| `output_tokens` | `TokenUsage.completion_tokens` |
| `cached_input_tokens` | `TokenUsage.cached_prompt_tokens` |

If input/output token counts are absent, usage is returned as `None`; otherwise
the provider is `"codex"` and the model is the configured Codex model.

## Model-config retry once

`generate_module` has one narrow retry path for model-level config rejection:

1. It calls `run_codex_exec` with the configured `[codex.config]` overrides.
2. If that raises `JauntGenerationError`, the message looks like a model config
   error, and extra config was non-empty, the backend retries **once**.
3. If the offending config key can be identified, that key is removed. If not,
   all extra config overrides are cleared.
4. The second failure, if any, is returned as the real failure.

This is not a retry loop and does not replace Jaunt's validation retry in
`generate_with_retry`.

## Wiring in Jaunt

Codex is now the sole Jaunt agent engine:

```python
_VALID_AGENT_ENGINES = ("codex",)
```

There is no legacy/aider whitelist entry to add. If `jaunt.toml` asks for an
old engine, config loading raises and tells the user to remove the override.

The `[codex]` table controls the shipped backend:

```toml
[agent]
engine = "codex"

[codex]
model = "gpt-5.5"
sandbox = "workspace-write"
reasoning_effort = "high"

[codex.config]
# Extra repeatable `codex exec -c key=value` overrides.
# verbosity = "low"
```

Defaults from `CodexConfig`:

| Field | Default |
|-------|---------|
| `model` | `"gpt-5.5"` |
| `reasoning_effort` | `"high"` |
| `sandbox` | `"workspace-write"` |
| `config` | `{}` |

`[llm]` is retained as informational/fallback config, but Codex authentication
comes from `codex login` / `CODEX_API_KEY`, and `CodexBackend` prefers
`[codex].model`.

## Whole-class generation

Whole-class `@jaunt.magic` works under the Codex engine because Codex emits a
**complete file**. For whole-class contexts, the backend writes
`_context/whole_class_contract.md` and tells Codex to implement every
`# jaunt:implement` method, keep preserved methods verbatim, and design the
public API implied by the docstring.

That complete-file workflow is the shipped fix for the old whole-class gap.

## Caveats & lessons

- **Constrain the agent.** Codex can run commands and touch files. Jaunt uses a
  throwaway temp workspace, `--sandbox workspace-write`, `approval_policy =
  "never"`, and an explicit "edit only the target file" instruction. The
  `complete_text` path uses `read-only`.
- **Token usage is parsed when Codex emits it.** Read
  `turn.completed.usage.input_tokens`, `output_tokens`, and
  `cached_input_tokens`; return `None` only when the required counts are absent.
- **Verify end-to-end, not just mocked units.** Jaunt's unit tests mock
  `generate_module`, so they do not exercise a real Codex call. Validate changes
  to this backend with an actual `jaunt build` on a real example.
- **Determinism.** Set `model_reasoning_effort` and a fixed `model`; avoid
  web/search features for reproducible builds unless a spec genuinely needs
  them.

## Heavier alternatives

- **`codex mcp-server`** (see [mcp-server.md](mcp-server.md)) is a reusable
  thread-based MCP surface. Jaunt does not use it today because a per-call
  subprocess keeps each module generation isolated and avoids long-lived session
  lifecycle concerns.
- **`app-server`** (see [app-server.md](app-server.md)) is useful for IDE-grade
  JSON-RPC control, approvals, and per-item events. It is heavier and
  experimental, and is overkill for "produce one module."
