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
model = "gpt-5.5"
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
model = "gpt-5.5"
reasoning_effort = "high"
sandbox = "workspace-write"
# Include `codex --version` in build/test freshness fingerprints.
# fingerprint_cli_version = true
# features = []
# Raw passthrough to `codex` (advanced):
# [codex.config]
"""


INIT_SPEC_TEMPLATE = '''\
# Starter spec: `jaunt build` implements this module into `__generated__/`.
import jaunt


@jaunt.magic()
def slugify(text: str) -> str:
    """
    Convert a string to a URL-safe slug: lowercase, spaces and runs of
    non-alphanumeric chars collapsed to single hyphens, leading/trailing
    hyphens stripped.
    """
    ...


@jaunt.test(targets=slugify)
def test_slugify() -> str:
    """Generate pytest coverage for words, punctuation runs, and surrounding spaces."""
    ...
'''
