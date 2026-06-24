# Embedding Codex as a Jaunt `GeneratorBackend`

This is the aider parallel: a Codex-backed code-generation engine for Jaunt,
built on `codex mcp-server`. It's a design reference (forward-looking — not yet
implemented). The grounded facts below are read from
`src/jaunt/generate/base.py` and `src/jaunt/config.py`.

## The interface you must satisfy

A backend subclasses `GeneratorBackend` and implements **one async method**:

```python
async def generate_module(
    self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
) -> tuple[str, TokenUsage | None]:
    """Return (generated_module_source, optional_token_usage)."""
```

Everything else is provided by the base class:

- `generate_with_retry(ctx, max_attempts=...)` calls your `generate_module`,
  runs `validate_generated_source(source, ctx.expected_names)`, and retries with
  appended error context. **You only produce a source string; Jaunt owns
  validation and retry.**
- Override properties: `model_name` (reads `self._model`), `provider_name`
  (e.g. `"codex"`), and optionally `supports_structured_output`.
- `complete_text(system, user)` — implement if you want this engine usable by
  `jaunt reconcile` for contract derivation; otherwise it raises.

`ModuleSpecContext` (the inputs you turn into a prompt):

| Field | What it gives you |
|-------|-------------------|
| `kind` | `"build"` or `"test"`. |
| `generated_module` | Dotted module path Codex must produce (e.g. `myapp.__generated__.foo`). |
| `expected_names` | Names the module must export — Jaunt validates these. |
| `spec_sources` | `{SpecRef: source}` — the decorated stubs + docstrings (the contract). |
| `decorator_prompts` | Extra per-spec instructions from decorator kwargs. |
| `dependency_apis` | `{SpecRef: api_summary}` — read-only APIs of deps. |
| `dependency_generated_modules` | Already-generated dep modules (importable context). |
| `module_contract_block`, `base_contract_block`, `blueprint_source`, `build_instructions_block`, `attached_test_specs_block`, `package_context_block`, `skills_block` | Pre-rendered prompt blocks Jaunt already assembles. |
| `async_runner` | `"asyncio"` / `"trio"` / … for async specs. |

`GenerationResult(attempts, source, errors, usage)` and
`TokenUsage(prompt_tokens, completion_tokens, model, provider, cached_prompt_tokens=0)`.

## The key impedance match

Codex is an **agent that edits files on disk**; `generate_module` must **return
source text**. So the backend:

1. Creates a clean temp workspace with the **target file path** Codex should fill.
2. Writes the spec/deps as read-only context files in that workspace.
3. Runs Codex (`sandbox=workspace-write`, `approval-policy=never`, `cwd`=temp)
   with a prompt: *"write the implementation to `<target>` and nothing else."*
4. **Reads the target file back** and returns its contents as `source`.

This is why Codex *fixes the whole-class gap*: it emits a **complete file**, so a
docstring-only / whole-class `@jaunt.magic` produces a full class body — the
exact thing aider's SEARCH/REPLACE flow couldn't do (see the
`aider-whole-class-gap` note). A Codex engine is the natural path to making
whole-class generation work outside `engine = "legacy"`.

## Worked sketch (mcp-server path)

Drive `codex mcp-server` as an MCP client (async fits `generate_module`
directly). Start the server **once** and reuse it across modules.

```python
from __future__ import annotations
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from jaunt.generate.base import (
    GeneratorBackend, ModuleSpecContext, TokenUsage,
)


class CodexBackend(GeneratorBackend):
    """GeneratorBackend that drives `codex mcp-server` to emit whole modules."""

    def __init__(self, model: str, *, sandbox: str = "workspace-write",
                 reasoning_effort: str = "high"):
        self._model = model
        self._sandbox = sandbox
        self._effort = reasoning_effort
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    @property
    def provider_name(self) -> str:
        return "codex"

    async def _ensure_session(self) -> ClientSession:
        if self._session is not None:
            return self._session
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command="codex", args=["mcp-server"])
        read, write = await self._stack.enter_async_context(stdio_client(params))
        session = await self._stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        self._session = session
        return session

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
            self._stack = self._session = None

    async def generate_module(
        self, ctx: ModuleSpecContext, *, extra_error_context: list[str] | None = None
    ) -> tuple[str, TokenUsage | None]:
        session = await self._ensure_session()

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / (ctx.generated_module.replace(".", "/") + ".py")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")  # exist so Codex edits in place

            # Seed read-only context (specs, dep APIs) as files Codex can read.
            ctx_dir = root / "_context"
            ctx_dir.mkdir()
            # SpecRef is a str ("pkg.mod:Qualname"); index filenames to avoid
            # collisions and keep the ref in a leading comment for traceability.
            for i, (ref, src) in enumerate(ctx.spec_sources.items()):
                (ctx_dir / f"spec_{i}.py").write_text(f"# {ref}\n{src}")
            for i, (ref, api) in enumerate(ctx.dependency_apis.items()):
                (ctx_dir / f"dep_{i}.pyi").write_text(f"# {ref}\n{api}")

            prompt = self._build_prompt(ctx, target.relative_to(root),
                                        extra_error_context)

            res = await session.call_tool("codex", {
                "prompt": prompt,
                "cwd": str(root),
                "sandbox": self._sandbox,
                "approval-policy": "never",
                "model": self._model,
                "config": {"model_reasoning_effort": self._effort},
            })
            # (Follow-ups would use res.structuredContent["threadId"] + codex-reply,
            #  but Jaunt's generate_with_retry re-prompts via a fresh call instead.)

            source = target.read_text()
            usage = self._extract_usage(res)   # may be None; see caveat below
            return source, usage

    def _build_prompt(self, ctx, target_rel, extra_error_context) -> str:
        blocks = [
            f"Write a complete Python module to `{target_rel}` that exports: "
            f"{', '.join(ctx.expected_names)}.",
            "The spec stubs and their docstrings in `_context/spec_*.py` are the "
            "behavioral contract. Read `_context/dep_*.pyi` for available APIs.",
            ctx.build_instructions_block,
            ctx.module_contract_block,
            "Edit ONLY the target file. Do not create other files, run tests, "
            "or modify anything else. Output the full module — no placeholders.",
        ]
        if extra_error_context:
            blocks.append("Previous attempt problems:\n" +
                          "\n".join(extra_error_context))
        return "\n\n".join(b for b in blocks if b)
```

`generate_with_retry` will call `generate_module`, validate the returned source
against `ctx.expected_names`, and re-invoke with `extra_error_context` on
failure — no Codex-side retry loop needed.

## Wiring it into Jaunt

1. **Engine whitelist** — `config.py` has
   `_VALID_AGENT_ENGINES = ("legacy", "aider")`. Add `"codex"`.
2. **Engine factory** — wherever the backend is constructed from
   `AgentConfig.engine`, branch `"codex"` → `CodexBackend(...)`.
3. **Config block** — a `[codex]` table (parallel to `[aider]`): `model`,
   `sandbox`, `reasoning_effort`, maybe `features`. Plus `[agent] engine =
   "codex"`.
4. **Lifecycle** — the backend holds a live `mcp-server` subprocess; call
   `aclose()` when the build finishes (the builder already owns backend
   lifetime).

```toml
[agent]
engine = "codex"

[codex]
model = "gpt-5.2-codex"
sandbox = "workspace-write"
reasoning_effort = "high"
```

## Caveats & lessons

- **Token usage may be approximate or absent.** The mcp tool result might not
  surface exact prompt/completion counts; return `None` rather than fabricating,
  or parse usage from streamed events if available. Costs/usage are reported per
  account in the OpenAI dashboard regardless.
- **Constrain the agent.** Codex *can* run commands and touch many files. For a
  pure code-gen backend, use a throwaway temp workspace, `workspace-write`
  scoped to it, and an explicit "edit only the target file" instruction.
  Otherwise it may "helpfully" run pytest, create scaffolding, or refactor deps.
- **Verify end-to-end, not just mocked units.** Jaunt's unit tests mock
  `generate_module`, so they never exercise a real Codex call — the
  `aider-whole-class-gap` only surfaced on a real build. Validate any Codex
  engine with an actual `jaunt build` on a real example.
- **Determinism.** Set `model_reasoning_effort` and a fixed `model`; avoid
  `--search` for reproducible builds unless a spec genuinely needs the web.

## Alternative paths (trade-offs)

- **`codex exec` subprocess** (see [exec.md](exec.md)) — instead of an MCP
  client, run `codex exec --json --sandbox workspace-write -a never -C <tmp> -`
  per module and read the target file back. Simpler (no long-lived process,
  matches the user's existing `cdx`/workflow execution model), but spawns a
  process per generation and gives a JSONL stream to parse instead of structured
  tool results. A reasonable first implementation.
- **`app-server`** (see [app-server.md](app-server.md)) — only if Jaunt later
  wants per-item events, approvals, or fs control during generation. Heavier and
  experimental; overkill for "produce one module."
