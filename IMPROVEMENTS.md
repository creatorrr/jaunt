# Jaunt: From Hackathon to Production

## What Jaunt Is (and Why It’s Worth Finishing)

Jaunt is spec-driven code generation: you write Python stubs with signatures, docstrings, and type hints, and an LLM generates the implementation. The idea is good. The architecture is surprisingly clean for a hackathon project. But there are real gaps between “impressive demo” and “tool someone would actually ship code with.”

This doc covers what needs to change, roughly in priority order. Each section states the problem, the fix, and the effort involved.

-----

## 1. Multi-Provider Backends

**Problem:** The only backend is OpenAI. The `cli.py` hard-rejects anything else (`if cfg.llm.provider != "openai": raise`). For a tool aimed at coding agents, not supporting Anthropic is a dealbreaker — Claude Code is one of the primary targets.

**Fix:**

Add an `AnthropicBackend` alongside `OpenAIBackend`. The `GeneratorBackend` ABC is already the right abstraction — the plumbing exists, it just needs a second implementation.

Config changes to `jaunt.toml`:

```toml
[llm]
provider = "anthropic"            # or "openai", "litellm"
model = "claude-sonnet-4-5-20250929"
api_key_env = "ANTHROPIC_API_KEY"
```

Consider also adding a `litellm` backend as a catch-all for local models, Ollama, Azure, etc. This removes Jaunt from the business of maintaining per-provider SDKs.

The `openai` SDK dependency should become optional (`jaunt[openai]`, `jaunt[anthropic]`). Core library shouldn’t require either.

**Effort:** Medium. The backend interface is clean; it’s mostly SDK wiring.

-----

## 2. Actually Plumb Dependency Context

**Problem:** The builder passes `dependency_apis={}` and `dependency_generated_modules={}` to every generation call. The entire dependency graph system (ordering, digests, staleness) works, but the LLM never sees the code it depends on. It’s generating in the dark.

This is the single biggest quality-of-generation issue.

**Fix:**

When building module B that depends on module A:

- `dependency_apis` should contain the spec source (signatures + docstrings) for A’s symbols
- `dependency_generated_modules` should contain the actual generated source for A (if available)

The data is already available — `extract_source_segment` exists, generated files are on disk. The `build_one` function in `builder.py` just needs to read them and populate the context fields.

**Effort:** Small-medium. The infrastructure exists; it’s wiring.

-----

## 3. CLI Ergonomics

**Problem:** No `init`, no `clean`, no `watch`. The tool has `build` and `test` and that’s it.

**Fix:**

**`jaunt init`** — Generate a starter `jaunt.toml` and an example spec file. Ask the user which source layout they use. This is the difference between “I’ll try it” and “I’ll figure it out later.”

```bash
jaunt init                    # interactive
jaunt init --layout src       # src/pkg/ layout
jaunt init --layout flat      # pkg/ at root
```

**`jaunt clean`** — Delete all `__generated__/` directories. Simple but essential for debugging stale state.

```bash
jaunt clean                   # remove all generated files
jaunt clean --dry-run         # show what would be deleted
```

**`jaunt watch`** — Watch spec files for changes and rebuild automatically. This is table stakes for iterative development, especially with agents that edit files and want immediate feedback.

```bash
jaunt watch                   # build on change
jaunt watch --test            # build + test on change
```

Use `watchfiles` (pure Python, async-friendly) rather than `watchdog`.

**`jaunt status`** — Show which modules are stale, which are up-to-date, which have errors. Machine-readable output with `--json`.

```bash
jaunt status
jaunt status --json           # for agent consumption
```

**Effort:** Medium. `init` and `clean` are straightforward. `watch` requires some care around debouncing and partial rebuilds.

-----

## 4. Machine-Readable Output (JSON Mode)

**Problem:** All output is human-formatted stderr text. Agents can’t parse it.

**Fix:**

Add `--output json` (or `--json`) to all commands. Output structured results:

```json
{
  "command": "build",
  "generated": ["my_app.specs"],
  "skipped": ["my_app.utils"],
  "failed": {},
  "duration_seconds": 2.3
}
```

For `status`:

```json
{
  "modules": {
    "my_app.specs": {"status": "stale", "reason": "spec_changed"},
    "my_app.utils": {"status": "current", "digest": "sha256:abc123"}
  }
}
```

This is critical for agent integration. Claude Code, Cursor, and similar tools need structured output to decide what to do next.

**Effort:** Small. The `BuildReport` dataclass already has the right shape.

-----

## 5. MCP Server

**Problem:** Jaunt has no programmatic interface for agents. The skill doc tells agents “run `jaunt build` in the terminal,” which works but is clunky and loses context.

**Fix:**

Ship a built-in MCP server that exposes Jaunt’s capabilities as tools:

```
jaunt mcp serve              # start MCP server (stdio transport)
```

Tools to expose:

- `jaunt_build` — build specs, return structured results
- `jaunt_test` — generate and run tests
- `jaunt_status` — check what’s stale
- `jaunt_spec_info` — given a module, return its specs and dependency graph
- `jaunt_clean` — clean generated files

This lets Claude Code (and any MCP-compatible agent) call Jaunt directly without shelling out and parsing terminal output.

**Effort:** Medium. Use `fastmcp` for the Python MCP server. The hard part is serializing the right context back.

-----

## 6. Structured Output for Generation

**Problem:** The LLM is asked to output raw Python and Jaunt strips markdown fences with a regex. There’s no schema enforcement, no structured extraction. The “retry with error context” is limited to syntax + symbol presence checks.

**Fix:**

For providers that support it, use structured output / tool use to get the generated code:

```python
# Anthropic: use tool_use with a schema
tools = [{
    "name": "write_module",
    "input_schema": {
        "type": "object",
        "properties": {
            "python_source": {"type": "string"},
            "imports_used": {"type": "array", "items": {"type": "string"}},
            "notes": {"type": "string"}
        },
        "required": ["python_source"]
    }
}]
```

This eliminates the fence-stripping hack and makes the output more reliable. Fall back to raw text for providers that don’t support structured output.

**Effort:** Small-medium per backend.

-----

## 7. Prompt Quality

**Problem:** The prompts are minimal. The system prompt is 8 lines. There’s no guidance on code style, error handling patterns, import conventions, or how to use the dependency context. The prompts don’t mention type hints, don’t ask for docstrings on generated code, and don’t constrain the output style at all.

**Fix:**

The prompts need real engineering. Key additions:

- **Import conventions:** “Import from `{spec_module}` for any symbols defined there. Import dependencies from their generated modules.”
- **Style constraints:** “Include type hints matching the spec signatures. Add brief docstrings. Follow PEP 8.”
- **Error handling:** “Preserve exception types specified in the spec docstrings. Don’t catch exceptions that should propagate.”
- **Dependency usage:** “Here is the source code for dependencies you can import and call. Use their actual module paths.”
- **Anti-patterns:** “Don’t add unused imports. Don’t import the spec module’s own symbols. Don’t generate test code.”

The prompt templates should also support user-level customization beyond just file path overrides — allow inline additions in `jaunt.toml`:

```toml
[prompts]
extra_system = """
Always use `logging` instead of `print`.
Prefer dataclasses over plain dicts.
"""
```

**Effort:** Medium. Prompt engineering is iterative.

-----

## 8. Config Cleanup

**Problem:** The config module is 268 lines of repetitive `if "key" in table: ... else: default` code. No validation beyond basic type checks. The `generated_dir` runtime hardcoding issue is documented but unfixed.

**Fix:**

Use Pydantic (already a dependency!) for config validation:

```python
class LLMConfig(BaseModel):
    provider: Literal["openai", "anthropic", "litellm"] = "openai"
    model: str = "claude-sonnet-4-5-20250929"
    api_key_env: str = "ANTHROPIC_API_KEY"
```

This cuts the config module in half and gives real validation errors for free.

Fix the `generated_dir` runtime hardcoding: pass the configured value through to `runtime.py` instead of hardcoding `"__generated__"`.

**Effort:** Small. Mostly mechanical.

-----

## 9. Error Messages and DX

**Problem:** Error messages are bare strings. When generation fails, the user sees “No source returned” or a raw traceback. No suggestion of what to do next.

**Fix:**

Every error path should include:

- What went wrong (specific)
- Why (if determinable)
- What to do about it

Examples:

- `"Generation failed for my_app.specs: OpenAI returned empty content. Check your API key and model name, or run with --force to retry."` instead of `"OpenAI returned empty content."`
- `"Spec my_app.specs:normalize_email depends on my_app.utils:parse_domain, which failed to build. Fix the dependency first."` instead of `"Dependency failed: my_app.utils"`

Add `--verbose` / `--debug` flags for full tracebacks and prompt dumps.

**Effort:** Small-medium. Mostly string work, but needs thought about what’s actually helpful.

-----

## 10. Caching and Cost Controls

**Problem:** Every `jaunt build --force` regenerates everything from scratch. No caching of LLM responses. No visibility into token usage or cost.

**Fix:**

**Response caching:** Cache LLM responses keyed on the full prompt hash. Store in `.jaunt/cache/`. This means `--force` only regenerates if the prompt actually changed (spec edits, dependency changes, prompt template changes).

```bash
jaunt build --force           # skip staleness check, but still use cache
jaunt build --no-cache        # skip cache entirely
```

**Cost tracking:** Log token counts and estimated cost per build. Show a summary:

```
build: 3 modules generated (1,247 input tokens, 892 output tokens, ~$0.003)
```

**Budget limits:** Optional `[llm] max_cost_per_build = 1.00` to prevent runaway generation.

**Effort:** Medium. Caching needs careful key construction. Cost tracking depends on provider response metadata.

-----

## 11. Agent Skill Improvements

**Problem:** The bundled SKILL.md is decent but static. The `cursorrules.md` exists but there’s no `CLAUDE.md` for Claude Code. The skill export (`jaunt skill export`) isn’t implemented in the CLI.

**Fix:**

**Ship a CLAUDE.md** tailored for Claude Code. This should include:

- How to run Jaunt commands
- When to write specs vs. implementations
- How to read and interpret generated code
- The three-layer separation (don’t bypass generation)

**Implement `jaunt skill export`:**

```bash
jaunt skill export                    # write .agents/skills/jaunt/ to project
jaunt skill export --format claude    # write CLAUDE.md to project root
jaunt skill export --format cursor    # write .cursorrules
```

**Make skills version-aware.** The exported skill should reference the installed Jaunt version and the configured LLM provider, so the agent knows what’s available.

**Effort:** Small. The content exists; it needs packaging and a CLI command.

-----

## 12. Testing Infrastructure

**Problem:** Tests exist and are thorough for the MVP, but there’s no integration test that actually calls an LLM. The test suite patches everything. There’s no way to verify that prompt changes actually improve generation quality.

**Fix:**

**Add an eval suite.** A small set of spec → expected-output pairs that can be run against a real backend:

```bash
jaunt eval                    # run evals against configured backend
jaunt eval --model gpt-4o     # compare models
```

Store results in `.jaunt/evals/` for comparison over time.

**Add snapshot tests** for the prompt rendering. When you change a prompt template, the test fails and shows you the diff in what the LLM would actually see. Approve the diff to update the snapshot.

**Effort:** Medium. The eval framework needs design thought.

-----

## 13. Package and Distribution

**Problem:** The package is `0.1.0` with a vague description (“A tiny Python library with a CLI”). `openai` is a hard dependency even if you never use it. No extras, no optional deps.

**Fix:**

```toml
[project]
name = "jaunt"
version = "0.2.0"
description = "Spec-driven code generation for Python. Write intent, generate implementations."

dependencies = []   # core has no LLM deps

[project.optional-dependencies]
openai = ["openai>=1.0.0,<2"]
anthropic = ["anthropic>=0.40.0"]
litellm = ["litellm>=1.0.0"]
all = ["jaunt[openai]", "jaunt[anthropic]"]
mcp = ["fastmcp>=2.0.0"]
dev = ["pytest>=8", "ruff>=0.9", "ty"]
```

The core library (`@jaunt.magic`, `@jaunt.test`, config, discovery, digest) should work without any LLM SDK installed. The backend is only needed at build time.

**Effort:** Small.

-----

## Priority Order

If I had to sequence these for maximum impact with minimum effort:

1. **Plumb dependency context** (#2) — Biggest generation quality win. Small effort.
1. **Anthropic backend** (#1) — Unlocks Claude Code users. Medium effort.
1. **JSON output** (#4) — Prerequisite for agent integration. Small effort.
1. **`init` / `clean` / `status`** (#3) — Basic DX. Small-medium effort.
1. **Prompt quality** (#7) — Improves every generation. Iterative.
1. **Config cleanup** (#8) — Reduces maintenance burden. Small effort.
1. **Optional deps** (#13) — Clean packaging. Small effort.
1. **MCP server** (#5) — Full agent integration. Medium effort.
1. **Agent skills** (#11) — Better agent guidance. Small effort.
1. **Error messages** (#9) — Polish. Small-medium effort.
1. **Watch mode** (#3) — Nice to have. Medium effort.
1. **Structured output** (#6) — Reliability improvement. Medium effort.
1. **Caching** (#10) — Cost optimization. Medium effort.
1. **Eval suite** (#12) — Quality assurance. Medium effort.

Items 1-4 shipped in v0.2.0. Items 5-9 are the next tier for production-readiness. Items 10-14 add polish.

-----

## What to Keep

The hackathon code gets several things right:

- **The three-layer separation** (backend / CLI orchestration / agent guidance) is a good architecture. Don’t collapse it.
- **The `GeneratorBackend` ABC** is the right abstraction. Adding backends is plug-and-play.
- **Atomic file writes** with `os.replace` — correct from day one.
- **Incremental rebuild via content-addressed digests** — this is exactly the right approach. No timestamps, no file watchers, just hash the spec graph.
- **The dependency DAG with critical-path scheduling** — this is more sophisticated than most hackathon code. Keep it.
- **`__test__ = False` on test stubs** — small detail, but it shows someone thought about the pytest interaction carefully.

The foundation is solid. The work ahead is mostly about filling gaps and adding polish, not rearchitecting.