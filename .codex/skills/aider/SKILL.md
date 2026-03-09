---
name: aider
description: Using and building atop aider-chat pypi lib
---

# Aider Internals -- Comprehensive Skill Reference

## Metadata

- **Skill name**: Aider Coding Agent Harness
- **Description**: Deep reference for Aider's internals, programmatic API, edit formats, repo map, architect mode, and how to embed Aider as a code generation engine within Jaunt.
- **Trigger conditions**: Activate when working on Aider integration, building custom agents on top of Aider, configuring LLM-driven code editing, or implementing Jaunt's `GeneratorBackend` using Aider.
- **Source**: Aider v0.86+ (https://github.com/Aider-AI/aider)
- **License**: Apache 2.0

---

## 1. How Aider Works Internally

### The Agent Loop

Aider's core loop in `BaseCoder.run()`:

1. **Get user input** (or accept programmatic message)
2. **Build context**: system prompt + repo map + file contents + read-only files + chat history
3. **Send to LLM** via litellm
4. **Parse response** to extract edits (format-specific: SEARCH/REPLACE, whole-file, unified diff, etc.)
5. **Apply edits** to files on disk
6. **Auto-lint** edited files (if configured)
7. **Auto-test** (if configured)
8. **Auto-commit** changes to git (if configured)
9. **Reflect on errors**: if lint/test fails, feed errors back to LLM and retry

### Key Classes

| Class | Module | Role |
|-------|--------|------|
| `Coder` (BaseCoder) | `aider.coders.base_coder` | Core orchestrator: manages context, LLM calls, edit application |
| `Model` | `aider.models` | Model configuration, litellm integration, token counting |
| `InputOutput` | `aider.io` | All user I/O, confirmations, file writing |
| `GitRepo` | `aider.repo` | Git operations, auto-commits, diffs |
| `RepoMap` | `aider.repomap` | Tree-sitter indexing, graph-ranked code map |

---

## 2. The Coder Class

### Factory Pattern: `Coder.create()`

Always use the factory -- it selects the correct subclass based on edit format:

```python
from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput

model = Model("claude-sonnet-4-20250514")
io = InputOutput(yes=True, pretty=False, fancy_input=False)

coder = Coder.create(
    main_model=model,
    edit_format="diff",       # or "whole", "udiff", "architect", etc.
    io=io,
    fnames=["src/myfile.py"],
    read_only_fnames=["src/types.py", "src/utils.py"],
    auto_commits=False,
    auto_lint=False,
    auto_test=False,
    use_git=True,
    stream=False,
    map_tokens=1024,
    verbose=False,
)
```

**Factory signature:**

```python
@classmethod
def create(
    main_model=None,       # Model instance
    edit_format=None,      # str: "diff", "whole", "udiff", "diff-fenced",
                           #       "architect", "editor-diff", "editor-whole"
    io=None,               # InputOutput instance
    from_coder=None,       # Existing Coder to clone context from
    summarize_from_coder=True,  # Summarize chat history when cloning
    **kwargs               # Passed to Coder.__init__()
)
```

When `from_coder` is provided, the new coder inherits:
- `fnames`, `read_only_fnames`
- `done_messages` (chat history, optionally summarized)
- `repo`, `root`
- Cost/token tracking state

### Edit Format to Coder Class Mapping

| Edit Format | Coder Class | Best For |
|-------------|-------------|----------|
| `"diff"` | `EditBlockCoder` | Most models; SEARCH/REPLACE blocks |
| `"whole"` | `WholeFileCoder` | Weaker models; returns entire file |
| `"udiff"` | `UnifiedDiffCoder` | GPT-4 Turbo; unified diff format |
| `"diff-fenced"` | `EditBlockFencedCoder` | Gemini models |
| `"architect"` | `ArchitectCoder` | Two-tier: architect plans, editor implements |
| `"editor-diff"` | `EditorEditBlockCoder` | Editor-only mode with SEARCH/REPLACE |
| `"editor-whole"` | `EditorWholeFileCoder` | Editor-only mode with whole files |

### Constructor Parameters (Important Subset)

```python
Coder.__init__(
    main_model,                    # Model instance
    io,                            # InputOutput instance
    repo=None,                     # GitRepo (auto-created if use_git=True)
    fnames=None,                   # List[str]: editable files
    read_only_fnames=None,         # List[str]: context-only files
    map_tokens=1024,               # Token budget for repo map (0 = disable)
    auto_commits=True,             # Git auto-commit after edits
    dirty_commits=True,            # Commit even when repo is dirty
    auto_lint=True,                # Run linter after edits
    auto_test=False,               # Run tests after edits
    lint_cmds=None,                # Dict[str, str]: language -> lint command
    test_cmd=None,                 # str: test command
    stream=True,                   # Stream LLM responses
    use_git=True,                  # Enable git integration
    edit_format=None,              # Override edit format
    suggest_shell_commands=True,   # Suggest shell commands in responses
    chat_language=None,            # Natural language for responses
    restore_chat_history=False,    # Resume prior chat
    auto_accept_architect=True,    # Skip confirmation in architect mode
    detect_urls=True,              # Auto-detect URLs in messages
    verbose=False,                 # Debug output
    cache_prompts=False,           # Enable prompt caching
)
```

### Running Instructions

```python
# Single instruction -- returns response content string
response = coder.run("implement the fibonacci function")

# Multiple sequential instructions
coder.run("add type hints to all functions")
coder.run("write docstrings for the public API")

# In-chat commands also work
coder.run("/tokens")       # Show token usage
coder.run("/add newfile.py")  # Add file to context
coder.run("/drop oldfile.py") # Remove file from context
```

**`coder.run()` signature:**

```python
def run(self, with_message=None, preproc=True):
    """
    Main entry point. If with_message is provided, processes that single
    instruction and returns. Otherwise enters interactive loop.

    Returns: partial_response_content (str) -- the LLM's response text
    """
```

### Streaming

```python
# For streaming responses (yields chunks)
for chunk in coder.run_stream("implement the function"):
    print(chunk, end="", flush=True)
```

### Accessing Results After run()

```python
coder.run("implement fibonacci")

# The LLM's full response text
print(coder.partial_response_content)

# Files that were edited
print(coder.aider_edited_files)  # set of relative paths

# Cost tracking
print(coder.total_cost)
print(coder.message_cost)
print(coder.total_tokens_sent)
print(coder.total_tokens_received)
```

---

## 3. The Model Class

### Basic Usage

```python
from aider.models import Model

# Simple -- uses litellm to route to correct provider
model = Model("gpt-4o")
model = Model("claude-sonnet-4-20250514")
model = Model("deepseek/deepseek-chat")

# With weak model for commit messages / summarization
model = Model("claude-sonnet-4-20250514", weak_model="claude-3-5-haiku-20241022")

# With editor model for architect mode
model = Model(
    "claude-sonnet-4-20250514",
    editor_model="claude-sonnet-4-20250514",
    editor_edit_format="editor-diff",
)
```

### How Model Resolution Works

1. **Alias resolution**: "sonnet" -> "claude-sonnet-4-5", "gpt4" -> "gpt-4o", etc.
2. **Model info fetch**: Queries litellm's model registry for token limits, costs
3. **API key validation**: Checks environment for required keys (OPENAI_API_KEY, ANTHROPIC_API_KEY, etc.)
4. **Settings configuration**: Applies model-specific settings (edit format, repo map, streaming, etc.)

### Model Aliases (Built-in)

| Alias | Resolves To |
|-------|-------------|
| `"sonnet"` | `"claude-sonnet-4-5"` |
| `"opus"` | `"claude-3-opus-20240229"` |
| `"haiku"` | `"claude-3-5-haiku-20241022"` |
| `"4o"` | `"gpt-4o"` |
| `"deepseek"` | `"deepseek/deepseek-chat"` |

### Key Model Attributes

```python
model.name                    # Resolved model name string
model.edit_format             # Default edit format for this model
model.use_repo_map            # Whether repo map is recommended
model.use_temperature         # Whether to send temperature param
model.streaming               # Whether streaming is supported
model.max_chat_history_tokens # Soft limit before summarization
model.info                    # Dict of litellm model metadata
model.extra_params            # Dict passed to litellm.completion()
```

### LiteLLM Integration

All LLM calls go through litellm under the hood:

```python
# Model.send_completion() wraps litellm.completion()
hash_obj, response = model.send_completion(
    messages=[{"role": "user", "content": "hello"}],
    functions=None,
    stream=False,
    temperature=0,
    extra_params={},
)
```

API keys are read from environment variables:
- `OPENAI_API_KEY` for OpenAI models
- `ANTHROPIC_API_KEY` for Anthropic/Claude models
- `CEREBRAS_API_KEY` for Cerebras models
- `DEEPSEEK_API_KEY` for DeepSeek
- `OPENROUTER_API_KEY` for OpenRouter
- Provider-prefixed models auto-detect: `openrouter/model-name` -> `OPENROUTER_API_KEY`

---

## 4. The InputOutput Class

### Headless/Embedded Mode

For programmatic use, suppress all interactive behavior:

```python
from aider.io import InputOutput
import io as stdlib_io

# Minimal headless setup
io = InputOutput(
    yes=True,              # Auto-confirm all prompts (critical for headless)
    pretty=False,          # No colors/formatting
    fancy_input=False,     # No prompt-toolkit UI
)

# Full suppression with output capture
output_buffer = stdlib_io.StringIO()
io = InputOutput(
    yes=True,
    pretty=False,
    fancy_input=False,
    input=stdlib_io.StringIO(),   # No stdin
    output=output_buffer,          # Capture stdout
)
```

### Constructor Parameters

```python
InputOutput(
    yes=None,                    # True=auto-yes, False=auto-no, None=ask user
    pretty=True,                 # Colorized output
    fancy_input=True,            # Prompt toolkit features
    input=None,                  # Custom input stream
    output=None,                 # Custom output stream
    encoding="utf-8",            # File encoding
    dry_run=False,               # Skip file writes
    input_history_file=None,     # Path for input history
    chat_history_file=None,      # Path for chat history
    llm_history_file=None,       # Path for LLM conversation log
    user_input_color="#00cc00",
    tool_output_color=None,
    tool_error_color="#FF2222",
    tool_warning_color="#FFA500",
    assistant_output_color="#0088ff",
)
```

### Key Methods

```python
io.tool_output("message")              # Standard info output
io.tool_output("msg", log_only=True)   # Log only, don't display
io.tool_error("error message")         # Error output
io.tool_warning("warning message")     # Warning output
io.write_text(fname, content)          # Write file (respects dry_run)
io.read_text(fname)                    # Read file with encoding
io.confirm_ask("Proceed?")            # Ask yes/no (auto-answered if yes=True)
```

---

## 5. The GitRepo Class

### Constructor

```python
from aider.repo import GitRepo

repo = GitRepo(
    io=io,                          # InputOutput instance
    fnames=["src/myfile.py"],       # Files to track
    git_dname=None,                 # Git dir (auto-detected)
    aider_ignore_file=".aiderignore",
    attribute_author=True,          # Set author to "aider"
    attribute_committer=True,       # Set committer to "aider"
    attribute_co_authored_by=True,  # Add co-authored-by trailer
    commit_prompt=None,             # Custom commit message prompt
    subtree_only=False,             # Restrict to current subtree
    git_commit_verify=False,        # Skip pre-commit hooks
)
```

### Key Methods

```python
repo.commit(fnames, context, message, aider_edits=True)  # Create commit
repo.get_diffs(fnames)                                     # Get unified diffs
repo.get_tracked_files()                                   # All tracked file paths
repo.get_dirty_files()                                     # Modified files
repo.ignored_file(fname)                                   # Check .aiderignore
```

---

## 6. Edit Formats Deep Dive

### SEARCH/REPLACE (diff format) -- Recommended for Most Models

The LLM returns blocks like:

```
path/to/file.py
<<<<<<< SEARCH
def old_function():
    return 42
=======
def old_function():
    return 43
>>>>>>> REPLACE
```

- Delimiter patterns: 5-9 `<` for SEARCH, 5-9 `=` for divider, 5-9 `>` for REPLACE
- Empty SEARCH block = create new file or append
- Filename appears on line before the opening `<<<<<<< SEARCH`
- Multiple blocks per file are supported
- Fallback matching: if exact match fails, searches other chat files

### Whole File Format

```
path/to/file.py
```python
entire file content here
```
```

- Simplest format; LLM returns complete file content
- Filename on line before code fence
- Good for small files or weaker models
- Wasteful for large files with small changes

### Unified Diff Format (udiff)

```
--- path/to/file.py
+++ path/to/file.py
@@ -10,7 +10,7 @@
 context line
-old line
+new line
 context line
```

- Standard unified diff notation
- Best for GPT-4 Turbo (reduces "lazy coding" tendencies)

### Diff-Fenced Format

Like SEARCH/REPLACE but with filename inside the fence. Designed for Gemini models that struggle with the standard fencing approach.

### Architect Mode (Two-Tier)

1. **Architect model** receives the user's request and produces natural-language editing instructions
2. **Editor model** receives those instructions + file context and produces actual code edits

```python
# Configure architect mode
model = Model(
    "claude-sonnet-4-20250514",
    editor_model="claude-sonnet-4-20250514",
    editor_edit_format="editor-diff",
)
coder = Coder.create(
    main_model=model,
    edit_format="architect",
    auto_accept_architect=True,  # Skip "Edit the files?" prompt
    io=io,
    fnames=fnames,
)
```

The `ArchitectCoder` flow:
1. Inherits from `AskCoder` (no direct editing)
2. In `reply_completed()`, spawns a separate editor `Coder` instance
3. The editor coder gets: isolated message queues, disabled shell suggestions, zero map tokens, no cache
4. Cost tracking flows back from editor to architect

---

## 7. Repo Map System

### How It Works

1. **Tree-sitter parsing**: Parses all source files into ASTs
2. **Symbol extraction**: Identifies definitions (functions, classes, methods) and references
3. **Graph construction**: Files are nodes, edges connect files with dependencies
4. **Graph ranking**: PageRank-like algorithm identifies most important symbols
5. **Token-budget fitting**: Condenses to fit `--map-tokens` budget (default 1024)

### Programmatic Control

```python
coder = Coder.create(
    main_model=model,
    fnames=fnames,
    map_tokens=2048,            # Increase repo map budget
    map_refresh="auto",         # "auto", "always", "files", "manual"
    map_multiplier_no_files=2,  # Multiplier when no files in context
)

# Access repo map directly
repo_map_text = coder.get_repo_map()

# The repo map is included automatically in context via format_messages()
```

### Using Repo Map for Context Gathering Only

```python
# Create an "ask" coder that doesn't edit files
coder = Coder.create(
    main_model=model,
    edit_format="ask",   # AskCoder -- no edits, just conversation
    fnames=[],
    read_only_fnames=list_of_all_files,
    map_tokens=4096,     # Generous map budget
)
response = coder.run("What files implement the authentication flow?")
# response contains the LLM's analysis using the repo map
```

---

## 8. Building Custom Agents on Aider

### Pattern 1: Wrapper Function

```python
from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput

def generate_code(
    instruction: str,
    editable_files: list[str],
    readonly_files: list[str] | None = None,
    model_name: str = "claude-sonnet-4-20250514",
    edit_format: str = "diff",
) -> dict:
    """Run a single code generation task via Aider."""

    model = Model(model_name)
    io = InputOutput(yes=True, pretty=False, fancy_input=False)

    coder = Coder.create(
        main_model=model,
        edit_format=edit_format,
        io=io,
        fnames=editable_files,
        read_only_fnames=readonly_files or [],
        auto_commits=False,
        auto_lint=False,
        auto_test=False,
        stream=False,
        map_tokens=0,  # Disable repo map if not needed
    )

    response = coder.run(instruction)

    return {
        "response": response,
        "edited_files": list(coder.aider_edited_files),
        "cost": coder.total_cost,
        "tokens_sent": coder.total_tokens_sent,
        "tokens_received": coder.total_tokens_received,
    }
```

### Pattern 2: Multi-Step Agent with Context Accumulation

```python
def multi_step_generate(steps: list[str], files: list[str]):
    model = Model("claude-sonnet-4-20250514")
    io = InputOutput(yes=True, pretty=False, fancy_input=False)

    coder = Coder.create(
        main_model=model,
        edit_format="diff",
        io=io,
        fnames=files,
        auto_commits=False,
        stream=False,
    )

    results = []
    for step in steps:
        response = coder.run(step)
        results.append({
            "step": step,
            "response": response,
            "edited": list(coder.aider_edited_files),
        })

    return results
```

### Pattern 3: Switching Edit Formats Mid-Session

```python
# Start with architect for planning
architect_coder = Coder.create(
    main_model=Model("claude-sonnet-4-20250514"),
    edit_format="architect",
    io=io,
    fnames=files,
)
architect_coder.run("Refactor the auth module into separate concerns")

# Switch to diff mode for targeted fixes
diff_coder = Coder.create(
    main_model=Model("claude-sonnet-4-20250514"),
    edit_format="diff",
    io=io,
    from_coder=architect_coder,  # Inherit context
)
diff_coder.run("Fix the import errors in auth/tokens.py")
```

### Pattern 4: Custom Prompt Injection

Aider's system prompt is built from template attributes on the coder's `gpt_prompts` object. You can modify prompts after creation:

```python
coder = Coder.create(main_model=model, edit_format="diff", io=io, fnames=files)

# Prepend custom context to the system prompt
# The model's system_prompt_prefix setting is the cleanest way:
model = Model("claude-sonnet-4-20250514")
# In .aider.model.settings.yml or programmatically:
# model.system_prompt_prefix = "You are generating code for the Jaunt framework..."

# Or inject context via a read-only file containing instructions
coder = Coder.create(
    main_model=model,
    io=io,
    fnames=["target.py"],
    read_only_fnames=["CONVENTIONS.md", "API_SPEC.md"],  # Context injection
)
```

### Pattern 5: Controlling File Context

```python
coder = Coder.create(main_model=model, io=io, fnames=["main.py"])

# Add files programmatically
coder.add_rel_fname("src/utils.py")

# Remove files
coder.drop_rel_fname("src/utils.py")

# Check current files
print(coder.get_inchat_relative_files())   # Editable files
print(coder.abs_read_only_fnames)          # Read-only files

# Get all repo files (for discovery)
print(coder.get_all_relative_files())
```

---

## 9. Jaunt-Specific Integration Patterns

### Mapping Jaunt Concepts to Aider

| Jaunt Concept | Aider Equivalent |
|---------------|------------------|
| Spec file (with `@jaunt.magic` stubs) | `fnames` -- editable files |
| Dependency context (transitive deps) | `read_only_fnames` -- context files |
| Generated `__generated__/` output | Files edited by Aider on disk |
| `jaunt.toml` LLM provider | `Model()` configuration |
| `ModuleSpecContext.spec_sources` | File content in chat context |
| `ModuleSpecContext.dependency_apis` | Read-only file content |
| Build prompt templates | System prompt / instruction to `coder.run()` |

### Recommended Architecture for an Aider-Based GeneratorBackend

```python
from aider.coders import Coder
from aider.models import Model
from aider.io import InputOutput
import io as stdlib_io
import tempfile
import shutil
from pathlib import Path

from jaunt.generate.base import GeneratorBackend, GenerationResult, ModuleSpecContext, TokenUsage


class AiderBackend(GeneratorBackend):
    """GeneratorBackend implementation using Aider as the coding agent."""

    def __init__(self, model_name: str, edit_format: str = "whole"):
        self.model_name = model_name
        self.edit_format = edit_format

    def generate(
        self,
        ctx: ModuleSpecContext,
        on_progress=None,
    ) -> GenerationResult:
        # 1. Set up a temp workspace with spec files
        with tempfile.TemporaryDirectory() as tmpdir:
            tmpdir = Path(tmpdir)

            # Write the target file (empty or with stubs)
            target = tmpdir / f"{ctx.generated_module.replace('.', '/')}.py"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("")

            # Write dependency context as read-only files
            readonly = []
            for ref, source in ctx.dependency_apis.items():
                dep_path = tmpdir / f"_deps/{ref}.py"
                dep_path.parent.mkdir(parents=True, exist_ok=True)
                dep_path.write_text(source)
                readonly.append(str(dep_path))

            # Write spec sources as read-only context
            for ref, source in ctx.spec_sources.items():
                spec_path = tmpdir / f"_specs/{ref}.py"
                spec_path.parent.mkdir(parents=True, exist_ok=True)
                spec_path.write_text(source)
                readonly.append(str(spec_path))

            # 2. Configure Aider
            model = Model(self.model_name)
            io = InputOutput(yes=True, pretty=False, fancy_input=False)

            coder = Coder.create(
                main_model=model,
                edit_format=self.edit_format,
                io=io,
                fnames=[str(target)],
                read_only_fnames=readonly,
                auto_commits=False,
                auto_lint=False,
                auto_test=False,
                stream=False,
                use_git=False,       # No git in temp workspace
                map_tokens=0,        # No repo map needed
            )

            # 3. Build the instruction from Jaunt's prompt templates
            instruction = self._build_instruction(ctx)

            # 4. Run generation
            try:
                response = coder.run(instruction)
                source = target.read_text()

                usage = TokenUsage(
                    prompt_tokens=coder.total_tokens_sent,
                    completion_tokens=coder.total_tokens_received,
                    model=self.model_name,
                    provider="aider",
                )

                return GenerationResult(
                    attempts=1,
                    source=source if source.strip() else None,
                    errors=[],
                    usage=usage,
                )
            except Exception as e:
                return GenerationResult(
                    attempts=1,
                    source=None,
                    errors=[str(e)],
                )

    def _build_instruction(self, ctx: ModuleSpecContext) -> str:
        names = ", ".join(ctx.expected_names)
        return (
            f"Implement the following Python module that exports: {names}.\n\n"
            f"The spec stubs and their docstrings define the expected behavior. "
            f"Read the spec files in _specs/ for the interface contracts. "
            f"Read the dependency files in _deps/ for available APIs.\n\n"
            f"Write the complete implementation to {ctx.generated_module}.py."
        )
```

### Recommended Edit Format for Jaunt

Follow Aider's model-default guidance first. Aider is generally configured to
pick an appropriate default edit format per model, so do not assume `"whole"`
is always best.

For **Jaunt build modules**:
- First attempt: use the configured task mode.
- If build mode is `"architect"`, start with architect planning plus
  `"editor-diff"` editing.
- If an architect retry fails because SEARCH/REPLACE style edits do not apply
  cleanly, keep architect planning but switch the editor to `"editor-whole"`.
- If the failure is a narrow type-check or small contract repair, prefer a
  whole-file repair pass against the previous candidate rather than another full
  architect cycle.

For **incremental updates** where preserving most of the file matters and the
model is already good at exact edits, `"diff"` is still efficient.

For **reliability fallbacks**, `"whole"` or `"editor-whole"` is the preferred
escape hatch when diff-style edits keep failing to apply.

### Multi-Provider Configuration

```python
# OpenAI
model = Model("gpt-4o")
# Requires: OPENAI_API_KEY

# Anthropic
model = Model("claude-sonnet-4-20250514")
# Requires: ANTHROPIC_API_KEY

# Cerebras (via OpenAI-compatible endpoint)
import os
os.environ["OPENAI_API_BASE"] = "https://api.cerebras.ai/v1"
os.environ["OPENAI_API_KEY"] = os.environ["CEREBRAS_API_KEY"]
model = Model("openai/llama-4-scout-17b-16e-instruct")

# DeepSeek
model = Model("deepseek/deepseek-chat")
# Requires: DEEPSEEK_API_KEY

# OpenRouter (access to many models)
model = Model("openrouter/anthropic/claude-sonnet-4-20250514")
# Requires: OPENROUTER_API_KEY

# Any litellm-supported provider
model = Model("provider/model-name")
```

### Parsing Aider's Output

After `coder.run()`, the generated code is already on disk (Aider writes files directly). To extract it:

```python
# Read the generated file after coder.run()
generated_source = Path(target_file).read_text()

# Check what was edited
edited_files = coder.aider_edited_files  # set of relative paths

# The LLM's response text (includes explanations, not just code)
llm_response = coder.partial_response_content
```

### Error Handling Strategies

```python
from aider.exceptions import LiteLLMExceptions

try:
    response = coder.run(instruction)
except KeyboardInterrupt:
    pass  # User cancelled
except Exception as e:
    # Check for common failure modes
    error_msg = str(e)

    if "context_length_exceeded" in error_msg:
        # Reduce context: fewer read-only files, smaller map_tokens
        pass
    elif "rate_limit" in error_msg:
        # Retry with backoff
        pass
    elif "invalid_api_key" in error_msg:
        # Check API key configuration
        pass

# Check for malformed responses (LLM didn't follow edit format)
if coder.num_malformed_responses > 0:
    # The LLM failed to produce valid edits
    # Consider: retry, switch edit format, or use a stronger model
    pass

# Check for exhausted context windows
if coder.num_exhausted_context_windows > 0:
    # Context was too large for the model
    pass
```

---

## 10. Configuration Reference

### Key Coder.create() Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `main_model` | `Model` | required | The LLM to use |
| `edit_format` | `str` | model default | Edit format: diff, whole, udiff, architect, etc. |
| `io` | `InputOutput` | auto | I/O handler |
| `fnames` | `list[str]` | `[]` | Editable files |
| `read_only_fnames` | `list[str]` | `[]` | Read-only context files |
| `auto_commits` | `bool` | `True` | Auto-commit edits to git |
| `dirty_commits` | `bool` | `True` | Commit when repo is dirty |
| `auto_lint` | `bool` | `True` | Run linter after edits |
| `auto_test` | `bool` | `False` | Run tests after edits |
| `test_cmd` | `str` | `None` | Test command to run |
| `lint_cmds` | `dict` | `None` | Language-specific lint commands |
| `stream` | `bool` | `True` | Stream LLM responses |
| `use_git` | `bool` | `True` | Enable git integration |
| `map_tokens` | `int` | `1024` | Repo map token budget (0 = disable) |
| `map_refresh` | `str` | `"auto"` | Map refresh strategy |
| `verbose` | `bool` | `False` | Debug output |
| `cache_prompts` | `bool` | `False` | Enable prompt caching |
| `suggest_shell_commands` | `bool` | `True` | Suggest shell commands |
| `detect_urls` | `bool` | `True` | Auto-detect URLs |
| `chat_language` | `str` | `None` | Response language |
| `auto_accept_architect` | `bool` | `True` | Skip architect confirmation |
| `restore_chat_history` | `bool` | `False` | Resume prior chat |

### Model Configuration via .aider.model.settings.yml

```yaml
- name: custom/my-model
  edit_format: diff
  weak_model_name: gpt-4o-mini
  editor_model_name: gpt-4o
  editor_edit_format: editor-diff
  use_repo_map: true
  use_system_prompt: true
  streaming: true
  lazy: false
  overeager: false
  use_temperature: true
  cache_control: false
  extra_params:
    max_tokens: 8192
    extra_headers:
      X-Custom: value
  accepts_settings:
    - thinking_tokens
    - reasoning_effort
```

### Environment Variables

| Variable | Purpose |
|----------|---------|
| `OPENAI_API_KEY` | OpenAI API authentication |
| `ANTHROPIC_API_KEY` | Anthropic API authentication |
| `DEEPSEEK_API_KEY` | DeepSeek API authentication |
| `OPENROUTER_API_KEY` | OpenRouter API authentication |
| `OPENAI_API_BASE` | Custom API endpoint URL |
| `AIDER_MODEL` | Default model name |
| `AIDER_WEAK_MODEL` | Default weak model |
| `AIDER_EDITOR_MODEL` | Default editor model |
| `AIDER_EDIT_FORMAT` | Default edit format |
| `NO_COLOR` | Disable colored output |

---

## 11. Stability Notes and Gotchas

### API Stability Warning

From the official docs:

> "The python scripting API is not officially supported or documented, and could change in future releases without providing backwards compatibility."

This means:
- Pin to a specific Aider version (e.g., `aider-chat==0.86.1`)
- Write integration tests that exercise your Aider usage patterns
- Monitor Aider's HISTORY.md for breaking changes on upgrade

### Important Defaults and Gotchas

1. **`auto_commits=True` by default** -- Always set `auto_commits=False` for programmatic use unless you want Aider managing your git history.

2. **`auto_lint=True` by default** -- Set to `False` if you handle linting separately (as Jaunt does).

3. **`use_git=True` by default** -- If running in a temp directory without git, set `use_git=False` or initialize a git repo first.

4. **`stream=True` by default** -- Set to `False` for simpler programmatic consumption (get complete response at once).

5. **`yes=True` is critical** for headless mode -- Without it, Aider will try to prompt the user interactively and hang.

6. **`suggest_shell_commands=True` by default** -- Set to `False` to prevent Aider from suggesting shell commands in its output, which can confuse programmatic parsing.

7. **`detect_urls=True` by default** -- Set to `False` to prevent Aider from trying to fetch URLs mentioned in prompts.

8. **Coder.create() vs Coder()** -- Always use `Coder.create()`, never instantiate `Coder()` directly. The factory selects the correct subclass.

9. **File paths must exist** -- `fnames` files should exist on disk (can be empty). Aider may refuse to work with non-existent files.

10. **Token counting** -- `coder.total_tokens_sent` and `coder.total_tokens_received` are cumulative across all `run()` calls. Use `coder.message_tokens_sent` for per-call metrics.

### Known Breaking Change Patterns

Based on HISTORY analysis:
- Edit format names occasionally change or new ones are added
- Model alias mappings update frequently as new models release
- The `ModelSettings` dataclass gains new fields regularly
- Coder subclass names may change (e.g., new coder types added)

### Version Pinning Strategy

```toml
# In pyproject.toml
dependencies = [
    "aider-chat>=0.86,<0.87",  # Pin to minor version
]
```

Or for maximum stability:
```toml
dependencies = [
    "aider-chat==0.86.1",  # Exact pin
]
```

---

## 12. Advanced Topics

### Custom Model Registration

For models not in litellm's registry:

```python
# Via .aider.model.metadata.json
{
    "custom/my-model": {
        "max_tokens": 4096,
        "max_input_tokens": 128000,
        "max_output_tokens": 4096,
        "input_cost_per_token": 0.00000050,
        "output_cost_per_token": 0.00000150,
        "litellm_provider": "openai",
        "mode": "chat"
    }
}
```

### Prompt Caching (Anthropic)

```python
model = Model("claude-sonnet-4-20250514")
coder = Coder.create(
    main_model=model,
    io=io,
    fnames=files,
    cache_prompts=True,            # Enable prompt caching
    # cache_keepalive_pings=5,     # Keep cache warm (5 pings at 5-min intervals)
)
```

### Reasoning/Thinking Tokens

```python
model = Model("claude-sonnet-4-20250514")
model.set_thinking_tokens("8k")  # Budget for extended thinking

# Or via environment
# AIDER_THINKING_TOKENS=8k
```

### The `from_coder` Pattern for Context Continuity

```python
# First pass: broad analysis
analyst = Coder.create(
    main_model=model, edit_format="ask", io=io,
    fnames=[], read_only_fnames=all_files, map_tokens=4096,
)
analysis = analyst.run("Which files need changes for feature X?")

# Second pass: targeted edits, inheriting context
editor = Coder.create(
    main_model=model, edit_format="diff", io=io,
    fnames=identified_files,
    from_coder=analyst,           # Inherit chat history + context
    summarize_from_coder=True,    # Compress history to save tokens
)
editor.run("Implement feature X based on our analysis")
```

### Disabling Git Entirely

```python
coder = Coder.create(
    main_model=model,
    io=io,
    fnames=files,
    use_git=False,         # No git repo detection
    auto_commits=False,    # No auto-commits
    dirty_commits=False,   # No dirty-state commits
)
```
