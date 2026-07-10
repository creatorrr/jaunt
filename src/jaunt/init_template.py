"""Shared templates for `jaunt init` and schema display."""

from __future__ import annotations

INIT_TEMPLATE = """\
version = 1

[paths]
source_roots = ["src"]
test_roots = ["tests"]
generated_dir = "__generated__"

[llm]
# Informational under the Codex engine: the model is set in [codex] below, and
# Codex authenticates via `codex login` / CODEX_API_KEY (not an api_key_env here).
provider = "openai"
model = "gpt-5.6-sol"
# max_cost_per_build = 5.0

[build]
jobs = 8
infer_deps = true
ty_retry_attempts = 1
async_runner = "asyncio"
# Keep target test source out of build prompts by default.
include_target_tests = false
# Deterministically reject generated imports that are not stdlib, declared
# dependencies, first-party modules, or explicitly allowed extras.
check_generated_imports = true
# generated_import_allowlist = ["intentional_extra"]
# Emit provenance-headed .pyi stubs next to each spec module (opt-out).
emit_stubs = true
# Add persistent extra instructions that apply to build generation.
# instructions = [
#   "Prefer small composable helpers over monolithic functions.",
# ]

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]

[prompts]
# Override packaged prompt templates with project-local files if needed.
# build_system = ""
# build_module = ""
# test_system = ""
# test_module = ""

[agent]
engine = "codex"

[codex]
model = "gpt-5.6-sol"
reasoning_effort = "medium"
sandbox = "workspace-write"
# Opt-in: include `codex --version` in freshness fingerprints. Couples
# `jaunt check` to environments that have the codex binary installed.
# fingerprint_cli_version = false
# features = []
# Raw passthrough to `codex` (advanced):
# [codex.config]
"""


# A fully annotated reference covering every section and key the config loader
# accepts. Shown by `jaunt instructions` when no jaunt.toml exists yet — the moment
# a user needs the schema — and kept in lock-step with the config allowlists by
# `tests/test_config.py::test_full_schema_template_covers_all_allowlists`. Not used
# by `jaunt init` (that scaffolds the smaller, opinionated INIT_TEMPLATE above).
FULL_SCHEMA_TEMPLATE = """\
version = 1

[paths]
source_roots = ["src"]        # dirs scanned for specs (package *parent*, e.g. "src")
test_roots = ["tests"]        # dirs scanned for @jaunt.test specs
generated_dir = "__generated__"  # output dir for generated code

[llm]
# Informational under the Codex engine (the model is set in [codex]); Codex
# authenticates via `codex login` / CODEX_API_KEY, not api_key_env.
provider = "openai"
model = "gpt-5.2"
api_key_env = "OPENAI_API_KEY"
max_cost_per_build = 5.0
reasoning_effort = "high"      # informational under Codex
anthropic_thinking_budget_tokens = 2048
prompt_cache = false
prompt_cache_key = ""

[build]
jobs = 8                       # parallel build workers
infer_deps = true              # AST-infer dependency edges in addition to deps=
ty_retry_attempts = 1          # ty-driven regeneration retries
async_runner = "asyncio"       # asyncio | anyio
include_target_tests = false   # keep target test source out of build prompts
check_generated_imports = true # reject undeclared imports in generated code
generated_import_allowlist = [] # extra top-level imports to permit in generated code
instructions = []              # persistent extra build-generation instructions
emit_stubs = true              # emit provenance-headed .pyi stubs (opt-out)

[test]
jobs = 4
infer_deps = true
pytest_args = ["-q"]
auto_class_tests = false       # auto-generate pytest coverage for class specs

[prompts]
# Override packaged prompt templates with project-local files (empty = default).
build_system = ""
build_preamble = ""
build_module = ""
test_system = ""
test_module = ""
project_overview_system = ""
project_overview_user = ""

[agent]
engine = "codex"               # the only supported engine

[codex]
model = "gpt-5.6-sol"
reasoning_effort = "medium"    # low | medium | high
sandbox = "workspace-write"
# Opt-in: embed `codex --version` in freshness fingerprints. Couples `jaunt
# check` to environments that have the codex binary installed.
fingerprint_cli_version = false
features = []
# Raw passthrough to the `codex` CLI (advanced); keys here are not validated.
[codex.config]

[daemon]
poll_interval = 2.0            # seconds between HEAD polls
max_jobs = 0                   # 0 -> build.jobs
notify_command = ""            # optional shell command run on job completion
auto_commit = false            # false parks green jobs as proposals (land later)

[skills]
auto = true                    # auto-generate PyPI helper skills for imports
max_chars_per_skill = 8000     # retained for back-compat (unused by Codex builder)
inject_user_skills = []        # retained for back-compat (unused by Codex builder)
builtin = true                 # seed Jaunt's bundled builtin skills
builtin_skills = ["pytest", "ruff", "ty", "uv"]  # override to trim/extend the default set

[contract]
battery_dir = "tests/contract" # where derived contract batteries are written
derive = ["examples", "errors"] # case kinds derived from docstring prose ("properties" is opt-in)
strength = true                # run mutation-based strength scoring at reconcile
property_max_examples = 50     # Hypothesis budget per derived property case

[semantic_gate]
enabled = true                 # gate behaviorally-equivalent edits before a rebuild
model = "gpt-5.4-mini"         # small model that judges contract equivalence
reasoning_effort = "high"      # low | medium | high

[context]
repo_map = true                # maintain treedocs.yaml + inject a repo map
repo_map_file = "treedocs.yaml"
enrich = false                 # LLM-enrich descriptions (else AST-only, offline)
max_chars = 6000               # cap the injected repo-map block
overview = false               # inject a model-written architecture overview

[context.search]               # colgrep semantic retrieval (opt-in)
enabled = false                # requires the `colgrep` binary on PATH
internal_retrieval = true      # seed _context/relevant_*.py from colgrep hits
max_hits = 8
"""


INIT_SPEC_TEMPLATE = '''\
# Starter spec: `jaunt build` implements this module into `__generated__/`.
import jaunt

jaunt.magic_module(__name__)


def greet(name: str) -> str:
    """Return a friendly greeting for `name`.

    Includes the name verbatim and ends with an exclamation mark.
    """
    ...
'''
