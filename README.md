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

Jaunt is a CLI for **spec-driven code generation**. You write intent as typed
contracts and Jaunt writes implementations under `__generated__/` using the OpenAI
Codex CLI (`codex exec`). Python is stable; the TypeScript target is an alpha behind
version-2 configuration.

Call `jaunt.magic_module(__name__)` once at the top of a file and every top-level
stub below it becomes a spec, with no per-symbol decorators:

```python
import re
import jaunt

jaunt.magic_module(__name__, prompt="All parsers are RFC 5322 strict.")

EMAIL_RE = re.compile(r"...")           # real body → handwritten, kept as-is

class Email:
    """Email with from_, to, subject, body. Validates on construction."""
    # docstring-only class → Jaunt designs and writes the whole class

def parse_email(raw: str) -> Email:
    """Parse an RFC 5322 payload into an Email. Raise ValueError on malformed
    input, naming the first offending header."""
    ...

def _debug(email: Email) -> str:        # real body → handwritten helper
    return f"<{email.from_} -> {email.to}>"
```

`jaunt build` fills in `Email` and `parse_email` and leaves `EMAIL_RE` and
`_debug` alone. The scan governs only top-level stubs — a `def` or `class` whose
body is `...`, a bare docstring, `pass`, or `raise NotImplementedError`. A spec
body never runs; an unbuilt one raises a clear error on first use. If your type
checker flags `...` under a concrete return annotation, either relax that rule on
your spec roots or write `raise NotImplementedError` instead — the two forms
digest identically, so switching between them never restales. Anything with a
real body, or carrying a non-jaunt decorator like `@property` or `@dataclass`, is
handwritten context the model reads but never regenerates.

### The precision layer: `@jaunt.magic`

Reach for the decorator when you want per-symbol control. `@jaunt.magic(deps=...,
prompt=...)` overrides the module defaults for one symbol — the module defaults
still merge in key by key, and the per-symbol value wins. The decorator is also
how you opt a symbol in against the scan: a stub carrying `@property`, or an
intentionally-empty function marked `@jaunt.preserve`, stays handwritten until
you add `@jaunt.magic`.

```python
@jaunt.magic(deps=[parse_email], prompt="Reuse parse_email per line.")
def parse_mbox(raw: str) -> list[Email]:
    """Split an mbox payload on `From ` lines and parse each message."""
    ...
```

## What you get

- **Module-level magic** — `jaunt.magic_module(__name__)` turns every top-level
  stub in a file into a spec. Mixed files (specs plus handwritten helpers) are
  first-class. Decorate individual symbols with `@jaunt.magic` / `@jaunt.test`
  when you want per-symbol overrides.
- **Whole-class specs** — a class-level spec can be docstring-only (Jaunt designs
  the API), stub methods only, or a mix. Each method sits in one of three tiers:
  `@jaunt.preserve` keeps it verbatim, `@jaunt.sig` locks its signature while
  Jaunt writes the body, and an unmarked guidepost stub lets the model adapt the
  signature.
- **Parallel, DAG-scheduled builds** — modules build over the dependency graph
  with a critical-path-first ready queue. A module starts generating the instant
  its dependencies finish, with no wave barriers, up to `[build] jobs` workers at
  once. A failed module skips only its dependents; the rest of the graph keeps
  building.
- **Smart change detection** — freshness is a SHA-256 digest over the
  AST-normalized contract, so reformatting, comment edits, and quote-style churn
  never trigger a rebuild. Staleness is dependency-aware: changing a module's
  exported API restales its dependents, while a body-only rebuild does not. A
  behaviorally-equivalent docstring edit gets re-frozen by the semantic gate
  instead of paying for a full rebuild.
- **Async, tests, and contracts** — `async def` specs build and test through
  `build.async_runner`, `@jaunt.test` specs generate pytest batteries, and
  `@jaunt.contract` pins hand-written code with a derived, committed battery.

### TypeScript alpha

TypeScript specs are private static inputs. The project-local `@usejaunt/ts` worker
parses them without executing application code, renders a deterministic API mirror,
and validates generated candidates in a compiler overlay before Jaunt writes anything:

```ts
import * as jaunt from "@usejaunt/ts/spec";

jaunt.magicModule();

/** Convert a title to a stable URL slug. */
export function slugify(title: string): string {
  return jaunt.magic();
}
```

```bash
uvx jaunt init --language ts
npm init -y && npm pkg set type=module
npm install -D @usejaunt/ts@next 'typescript@^5.9' vitest fast-check @types/node
uvx jaunt sync
uvx jaunt migrate --language ts       # upgrade preview; plan-only and model-free
uvx jaunt build --language ts
uvx jaunt test --language ts
uvx jaunt check --language ts
```

`jaunt init` leaves `package.json` untouched and prints the remaining package setup
command. Existing packages without a `type` get `npm pkg set type=module`; explicit
CommonJS packages keep CommonJS and receive a compatible NodeNext config.

The Jaunt worker runs on Node `>=20 <25`; generated JavaScript may target a different
deployment runtime according to the owning `tsconfig.json`.

Generated programs use ordinary imports and keep running without Jaunt installed. See
the [TypeScript guide](https://jaunt.ing/docs/guides/typescript) for the facade layout,
supported compiler range, and version-2 config.

## Two Modes

Jaunt has two authoring modes that coexist in the same project:

- **Magic mode** (`jaunt.magic_module` / `@jaunt.magic` / `@jaunt.test`): the
  docstring is canonical and Jaunt generates implementations under
  `__generated__/`.
- **Contract mode** (`@jaunt.contract`): committed code is canonical and Jaunt
  derives a committed pytest battery under `tests/contract/`. Covers top-level
  functions (sync or async) and whole classes; derived cases may use pytest
  fixtures resolved from `tests/contract/conftest.py`. Opt-in `"properties"`
  derives Hypothesis property tests from a `Properties:` docstring section —
  deterministic `given <bindings> :: <invariant>` bullets, plus prose bullets
  the model transcribes at `reconcile`.

See `examples/contract_slugify/` for a Contract-mode walkthrough and
`examples/contract_properties/` for property idioms (conservation, round-trips,
bounds) — including a truncation bug the pinned examples can't catch.

## Jaunt builds itself

Since 1.5.2, Jaunt uses both modes on its own source. Seven framework modules
are magic-mode specs — `jaunt.guard`, `jaunt.heldout`, `jaunt.migrate`, and the
four `jaunt.contract` helpers (`strength`, `cases`, `drift`, `edits`) — with
their generated bodies and `.pyi` stubs committed and shipped in the wheel.
Fifteen more core modules are in contract mode, with committed pytest batteries
under `tests/contract/jaunt/`. `jaunt check` runs in CI and gates Jaunt's own
spec-vs-generated drift, deterministically and without an API key. Modules that
run during `import jaunt` (the runtime, registry, and decorator internals) stay
plain handwritten Python — the builder needs them to build anything.

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

`jaunt init` scaffolds a starter `src/specs.py` in module-magic style — one stub
Jaunt implements:

```python
import jaunt

jaunt.magic_module(__name__)


def greet(name: str) -> str:
    """Return a friendly greeting for `name`.

    Includes the name verbatim and ends with an exclamation mark.
    """
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

### Codex and Claude Code plugins

The first-party plugins package generated-file guards, session freshness, and
workspace-aware build/convert/doctor workflows:

```bash
jaunt install-codex-plugin
jaunt install-claude-plugin
```

The underlying marketplace commands are:

```bash
codex plugin marketplace add creatorrr/jaunt
codex plugin add jaunt@jaunt-codex-plugins
claude plugin marketplace add creatorrr/jaunt
claude plugin install jaunt@jaunt-plugins
```

Docs: https://jaunt.ing/docs/guides/codex-plugin and
https://jaunt.ing/docs/guides/claude-code-plugin.

## Background Daemon

`jaunt daemon start` runs background codegen with commit-triggered isolated jobs
and parks green jobs as proposals by default. Land them with `jaunt jobs land`,
discard them with `jaunt jobs discard`, or opt into auto-commit in `jaunt.toml`.
`jaunt daemon stop|status` stops or inspects it.
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

## Release

Publishing runs through the repository's **Coordinated release** GitHub Actions
workflow. It builds the Python and npm candidates once, tests those exact artifacts,
publishes through PyPI and npm trusted publishing (OIDC), verifies the registry bytes,
then creates the matching Git tags and GitHub releases. Do not upload a locally built
wheel or tarball.

After the version, lockfiles, changelog, and generated artifacts are committed on
`main`, run `.github/workflows/release.yml` from the Actions UI. Choose `python`,
`typescript`, or `both`; leave `publish` off for a candidate-only rehearsal, or enable
it to publish. TypeScript alpha releases use the `next` npm dist-tag (`beta` is also an
explicit workflow choice).

The equivalent GitHub CLI invocation is:

```bash
gh workflow run release.yml --ref main \
  -f component=both \
  -f publish=true \
  -f npm_dist_tag=next
```

The repository must have trusted-publisher entries for the `pypi` and `npm` GitHub
environments. No long-lived PyPI or npm token is stored in Actions.

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
