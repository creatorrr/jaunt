# Jaunt

<div align="center">
  <img src="docs-site/public/images/tyger-blake-1794.jpg" alt="William Blake, 'The Tyger' from Songs of Experience (1794). The Metropolitan Museum of Art, Open Access." width="280" />
  <br/>
  <sub>William Blake, <em>The Tyger</em>, plate 42 from <em>Songs of Experience</em> (1794).
  <a href="https://www.metmuseum.org/art/collection/search/347983">The Metropolitan Museum of Art, Open Access.</a></sub>
</div>

<br/>

> *Tyger Tyger, burning bright,*
> *In the forests of the night;*
> *What immortal hand or eye,*
> *Could frame thy fearful symmetry?*
>
> -- William Blake, via Alfred Bester's *The Stars My Destination*

Jaunt is a small Python library + CLI for **spec-driven code generation**:

- Write implementation intent as normal Python stubs decorated with `@jaunt.magic(...)`.
- Optionally write test intent as stubs decorated with `@jaunt.test(...)`.
- Jaunt generates real modules under `__generated__/` using the OpenAI Codex CLI (`codex exec`) as its code-generation engine.
- Async support is available for both implementation and test specs through `async def` plus the `build.async_runner` setting.
- `@magic` works on individual class methods too — decorate instance methods, `@classmethod`, `@staticmethod`, or `@abstractmethod` stubs and Jaunt generates only those methods while preserving the rest of the class.
- Incremental freshness tracks both module digests and exported dependency APIs, so signature changes, whole-class member changes, and contract docstring edits can invalidate dependents.

## Two Modes

Jaunt supports two first-class authoring modes that coexist and are selected by
decorator:

- **Magic mode** (`@jaunt.magic` / `@jaunt.test`): the docstring is canonical and
  Jaunt generates implementations under `__generated__/`.
- **Contract mode** (`@jaunt.contract`): committed code is canonical and Jaunt
  derives a committed pytest battery under `tests/contract/`.

See `examples/contract_slugify/` for a Contract-mode walkthrough.

## Installation

```bash
pip install jaunt

# The base install is batteries-included (rich, watchfiles, pytest,
# pytest-asyncio, anyio) — no optional extras.

# Jaunt drives the external OpenAI Codex CLI, which you install and
# authenticate separately:
#   1. Install the `codex` CLI (see the Codex docs).
#   2. Authenticate it: `codex login`.
```

## Codex Engine

Codex is the sole code-generation engine: Jaunt drives `codex exec` for
all build/test/skill workflows. It requires the external `codex` binary on your
PATH, authenticated via `codex login`. Multi-provider routing is deferred.

## Quickstart (This Repo)

Prereqs: `uv` installed.

```bash
uv sync
codex login                 # authenticate the Codex engine
uv run jaunt --version
```

For your own project, run tests with the source root importable, e.g. `PYTHONPATH=src`.

See `docs-site/` for rendered docs, or `DOCS.md` for a plain-text walkthrough.

All examples live under `examples/`. See `examples/README.md` for the full list.

### Your First Spec

`jaunt init` creates a starter `src/specs.py` like this:

```python
import jaunt

@jaunt.magic()
def slugify(text: str) -> str:
    """Convert a string to a URL-safe slug: lowercase, collapse non-alphanumeric runs."""
    ...

@jaunt.test(targets=slugify)
def test_slugify() -> str:
    """Check words, punctuation, and surrounding spaces."""
    ...
```

```bash
uv run jaunt build
PYTHONPATH=src uv run jaunt test
```

### Hackathon Demo (JWT Auth)

Headline demo: **JWT auth** (the "wow gap" example: short spec, real generated glue + tests).

```bash
# Generate implementations for @jaunt.magic specs.
uv run jaunt build --root examples/jwt_auth

# Generate pytest tests for @jaunt.test specs and run them.
PYTHONPATH=examples/jwt_auth/src uv run jaunt test --root examples/jwt_auth
```

## For Coding Agents

Point any coding agent (Claude Code, Codex, Cursor, a bare shell, CI) at Jaunt
with one command:

```bash
jaunt instructions          # a tight, project-aware primer to load into context
jaunt instructions --json   # {command, ok, text, project} for tooling/MCP
```

It prints the framework rules (the two modes, the build/test loop, how to write a
good spec, the command + exit-code reference) followed by a live snapshot of the
current project (resolved paths, engine/model, and which modules are stale). It
ships with the package, so the briefing always matches the installed version. Run
it before you start working.

## Background Daemon

`jaunt daemon start` runs background codegen with commit-triggered isolated jobs
and auto-commit on green. `jaunt daemon stop|status` stops or inspects it.
Use `jaunt jobs` for job records, would-rebuild previews, `show <id> [--full]`,
and `retry <id>`. `jaunt log` tails `JAUNT_LOG` (`-n N`, `--module X`), and
`jaunt guard` warns when agents touch `__generated__` via the PreToolUse hook.

## Freshness Model

- The full cleaned docstring is part of the spec contract, not just the first summary line.
- For whole-class `@jaunt.magic` specs, Jaunt treats the class signature plus declared members and method signatures as exported API.
- Jaunt's freshness model uses that dependency API too, so an upstream contract change can mark downstream modules stale even if their own source file did not change.

## Eval Suite

`jaunt eval` is deferred under the Codex engine (rework pending).

Prompt snapshots:

```bash
uv run pytest tests/test_prompt_snapshots.py --snapshot-update
```

## Auto-Generate PyPI Skills (Build)

`jaunt build` includes a best-effort pre-build step that auto-generates “skills” for external libraries your project imports and injects them into the build prompt.

What happens:

- Scan `paths.source_roots` for `import ...` / `from ... import ...` (ignores stdlib, internal modules, and relative imports).
- Resolve imports to installed PyPI distributions + versions from the current environment.
- Ensure a skill exists per distribution at:
  - `<project_root>/.agents/skills/<dist-normalized>/SKILL.md`
- If missing/outdated, fetch the exact PyPI README for `<dist>==<version>` and generate `SKILL.md` using the Codex engine.
- Inject the concatenated skills text into the build LLM prompt.

Overwrite rules:

- Jaunt only overwrites a skill if it was previously Jaunt-generated (it has a `<!-- jaunt:skill=pypi ... -->` header) and the installed version changed.
- If the header is missing, the file is treated as user-managed and will never be overwritten.

Failure mode: warnings to stderr, and the build continues without missing skills.

## Docs Site (Fumadocs)

The repository includes a Fumadocs (Next.js) documentation site under `docs-site/`.

```bash
cd docs-site
npm run dev
```

## Publish to PyPI

If you keep your token in `.env` as `UV_PUBLISH_TOKEN=...`, load it into your shell first:

```bash
set -a
source .env
set +a
```

Build and validate artifacts:

```bash
uv build
uvx twine check dist/*
```

Upload to PyPI:

```bash
uv publish --check-url https://pypi.org/simple/
```

## Dev

```bash
uv run ruff check --fix .
uv run ruff format .
uv run ty check
uv run pytest
```

Final verification before pushing:

```bash
uv run ruff check .
uv run ruff format --check .
uv run ty check
uv run pytest
```

## Why "Jaunt"?

Named after *jaunting* -- teleportation by thought alone -- from Alfred
Bester's 1956 novel [*The Stars My Destination*](https://en.wikipedia.org/wiki/The_Stars_My_Destination)
(originally published as *Tiger! Tiger!*). You think about where you want to
be, and you're there.

Jaunt works the same way: describe your intent, and arrive at working code.

The forge-and-furnace imagery you'll find scattered through the codebase
comes from William Blake's poem "The Tyger," which Bester used as the
novel's epigraph and alternate title. The poem's vision of creation --
hammer, chain, furnace, anvil -- mirrors the act of forging code from pure
specification.
